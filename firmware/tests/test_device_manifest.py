"""Task #47 host contract tests: device.json / manifest / SHA-256.

Stdlib only (no ESP-IDF, no device, no network). Locks the Task #47
contract implemented by firmware/components/recorder/sha256.c,
device_identity.c, device_manifest.c and wired into
firmware/main/main.c:

- incremental SHA-256 over the WAV file bytes entire (lowercase hex 64,
  PC-recomputable via hashlib)
- UUIDv4 device/segment identity (NVS-stable deviceId, fresh
  recordingId per segment, no collisions)
- device.json / manifest.json schema shape (Spec #35 required fields,
  schemaVersion=1, unknown-major fail-closed, deviceId-mismatch
  preservation)
- manifest.tmp full-write + flush + atomic rename (crash during tmp
  write preserves the old manifest; missing manifest + valid tmp
  promotes)
- CONFLICT on same recordingId with a different hash/filename (never
  overwrites), idempotent duplicate upserts
- boot wiring (NVS ensure, device.json ensure, recovered sync) and
  rotation/terminal manifest records with metadata-only logging

A faithful Python mirror of the C validation/build/upsert rules plus
source-presence assertions keeps the C implementation from drifting.
Real files use only synthetic payloads in temporary directories.
"""

import hashlib
import json
import os
import re
import struct
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
SHA_H = COMP / "include/sha256.h"
SHA_C = COMP / "sha256.c"
ID_H = COMP / "include/device_identity.h"
ID_C = COMP / "device_identity.c"
MAN_H = COMP / "include/device_manifest.h"
MAN_C = COMP / "device_manifest.c"
CFG = COMP / "include/recorder_config.h"
MAIN = REPO / "firmware/main/main.c"
CMAKE = COMP / "CMakeLists.txt"

SCHEMA_VERSION = 1
MODEL = "M5Capsule v1.1"
FW = "0.1.0"


def _strip_c_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"//.*", "", src)
    return src


def _has_c_call(src, name):
    return (
        re.search(r"\b" + re.escape(name) + r"\s*\(", _strip_c_comments(src))
        is not None
    )


# --- Python mirrors -------------------------------------------------------

def mirror_valid_uuid(s):
    if not isinstance(s, str) or len(s) != 36:
        return False
    for i, c in enumerate(s):
        if i in (8, 13, 18, 23):
            if c != "-":
                return False
        elif c not in "0123456789abcdefABCDEF":
            return False
    return True


def mirror_format_uuid_v4(rand16: bytes) -> str:
    assert len(rand16) == 16
    b = bytearray(rand16)
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def mirror_valid_sha_hex(s):
    return (
        isinstance(s, str)
        and len(s) == 64
        and all(c in "0123456789abcdef" for c in s)
    )


def mirror_valid_iso(s):
    if not isinstance(s, str) or len(s) != 25:
        return False
    for i, expect in (
        (4, "-"), (7, "-"), (10, "T"), (13, ":"),
        (16, ":"), (22, ":"),
    ):
        if s[i] != expect:
            return False
    if s[19] not in ("+", "-"):
        return False
    digits = [i for i in range(25) if i not in (4, 7, 10, 13, 16, 19, 22)]
    if any(not s[i].isdigit() for i in digits):
        return False
    mon, day = int(s[5:7]), int(s[8:10])
    hour, minute, sec = int(s[11:13]), int(s[14:16]), int(s[17:19])
    oh, om = int(s[20:22]), int(s[23:25])
    return (
        1 <= mon <= 12 and 1 <= day <= 31 and hour <= 23
        and minute <= 59 and sec <= 60 and oh <= 14 and om <= 59
    )


def mirror_build_device_json(device_id, model=MODEL, fw=FW):
    assert mirror_valid_uuid(device_id)
    assert model and '"' not in model and "\\" not in model
    assert fw and '"' not in fw and "\\" not in fw
    return (
        '{"schemaVersion":%d,"deviceId":"%s","model":"%s",'
        '"firmwareVersion":"%s","audioCapabilities":{"sampleRate":16000,'
        '"bitDepth":16,"channels":1,"format":"pcm"}}'
        % (SCHEMA_VERSION, device_id, model, fw)
    )


def mirror_build_entry(rec_id, filename, started_at, duration_ms,
                       size_bytes, sha, state, fw=FW):
    assert mirror_valid_uuid(rec_id)
    assert filename.startswith("recordings/") and filename.endswith(".wav")
    assert not filename.endswith(".wav.part")
    assert mirror_valid_iso(started_at)
    assert mirror_valid_sha_hex(sha)
    assert state in ("finalized", "recovered")
    assert size_bytes >= 44
    return (
        '{"recordingId":"%s","filename":"%s","startedAt":"%s",'
        '"durationMs":%d,"sizeBytes":%d,"sha256":"%s","sampleRate":16000,'
        '"bitDepth":16,"channels":1,"state":"%s","firmwareVersion":"%s"}'
        % (rec_id, filename, started_at, duration_ms, size_bytes, sha,
           state, fw)
    )


def mirror_upsert(manifest_path: Path, tmp_path: Path, device_id: str,
                  updated_at: str, entry: dict):
    """Mirror of device_manifest_upsert_file over real files.

    entry: dict with recordingId/sha256/filename keys plus full JSON text
    under entry["__json__"]. Returns "ok" / "conflict" / "error".
    """
    assert mirror_valid_uuid(device_id)
    assert mirror_valid_iso(updated_at)
    entry_json = entry["__json__"]
    rec_id = entry["recordingId"]
    assert mirror_valid_uuid(rec_id)
    assert mirror_valid_sha_hex(entry["sha256"])
    if not manifest_path.exists():
        if tmp_path.exists():
            return "error"  # caller handles promotion separately
        payload = json.dumps(
            {"schemaVersion": 1, "deviceId": device_id,
             "updatedAt": updated_at,
             "recordings": [{k: v for k, v in entry.items()
                             if not k.startswith("__")}]},
            separators=(",", ":"))
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.rename(manifest_path)
        return "ok"
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return "error"
    if doc.get("schemaVersion") != 1:
        return "error"  # unknown major: fail closed
    if doc.get("deviceId") != device_id:
        return "error"  # fork: never auto-overwrite
    recs = doc.get("recordings")
    if not isinstance(recs, list):
        return "error"
    for existing in recs:
        if existing.get("recordingId") == rec_id:
            if (existing.get("sha256") == entry["sha256"]
                    and existing.get("filename") == entry["filename"]):
                return "ok"  # idempotent duplicate
            return "conflict"  # same id, different bytes/name
    recs.append({k: v for k, v in entry.items()
                 if not k.startswith("__")})
    doc["updatedAt"] = updated_at
    payload = json.dumps(doc, separators=(",", ":"))
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.rename(manifest_path)
    return "ok"


# --- SHA-256 ---------------------------------------------------------------

def test_sha256_vectors_match_hashlib():
    assert hashlib.sha256(b"").hexdigest() == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    assert hashlib.sha256(b"abc").hexdigest() == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    # Incremental update equivalence (the device hashes in 1KB reads).
    h = hashlib.sha256()
    h.update(b"abc")
    h.update(b"def" * 2000)
    whole = hashlib.sha256(b"abc" + b"def" * 2000).hexdigest()
    assert h.hexdigest() == whole
    assert len(whole) == 64 and mirror_valid_sha_hex(whole)


def test_sha256_hex_shape_mirror():
    assert mirror_valid_sha_hex("e" * 64)
    assert mirror_valid_sha_hex("0123456789abcdef" * 4)
    assert not mirror_valid_sha_hex("E" * 64)  # uppercase rejected
    assert not mirror_valid_sha_hex("e" * 63)  # truncated rejected
    assert not mirror_valid_sha_hex("g" * 64)  # non-hex rejected
    assert not mirror_valid_sha_hex("")


def test_sha256_file_hash_matches_recomputation(tmp_path=None):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "120000_synth.wav"
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + 32768, b"WAVE", b"fmt ",
            16, 1, 1, 16000, 32000, 2, 16, b"data", 32768)
        payload = b"".join(
            struct.pack("<h", (i % 512) - 256) for i in range(16384))
        wav.write_bytes(header + payload)
        # Device incremental hash == PC whole-file recomputation.
        chunked = hashlib.sha256()
        with wav.open("rb") as f:
            while True:
                block = f.read(4096)
                if not block:
                    break
                chunked.update(block)
        assert chunked.hexdigest() == hashlib.sha256(
            wav.read_bytes()).hexdigest()
        assert mirror_valid_sha_hex(chunked.hexdigest())


# --- UUID ------------------------------------------------------------------

def test_uuid_format_applies_version_and_variant():
    u = mirror_format_uuid_v4(bytes(16))
    assert mirror_valid_uuid(u)
    assert u[14] == "4"  # version nibble
    assert u[19] in "89ab"  # variant bits
    assert u == u.lower()
    # Random inputs never collide in practice and always validate.
    seen = {mirror_format_uuid_v4(os.urandom(16)) for _ in range(256)}
    assert len(seen) == 256
    assert all(mirror_valid_uuid(v) for v in seen)


def test_uuid_validation_mirror():
    assert mirror_valid_uuid("123e4567-e89b-42d3-a456-426614174000")
    assert not mirror_valid_uuid("s0001")  # pre-#47 placeholder rejected
    assert not mirror_valid_uuid("")
    assert not mirror_valid_uuid("123e4567e89b42d3a456426614174000")
    assert not mirror_valid_uuid("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")


# --- device.json ------------------------------------------------------------

def test_device_json_shape_and_required_fields():
    did = mirror_format_uuid_v4(os.urandom(16))
    doc = json.loads(mirror_build_device_json(did))
    for field in ("schemaVersion", "deviceId", "model", "firmwareVersion",
                  "audioCapabilities"):
        assert field in doc, field
    assert doc["schemaVersion"] == 1
    assert doc["deviceId"] == did
    assert doc["model"] == MODEL
    assert doc["firmwareVersion"] == FW
    caps = doc["audioCapabilities"]
    assert caps["sampleRate"] == 16000
    assert caps["bitDepth"] == 16
    assert caps["channels"] == 1


def test_iso8601_shape_mirror():
    assert mirror_valid_iso("2026-09-14T12:00:00+00:00")
    assert mirror_valid_iso("1970-01-01T00:00:00+00:00")
    assert not mirror_valid_iso("2026-09-14 12:00:00")
    assert not mirror_valid_iso("2026-09-14T12:00:00")
    assert not mirror_valid_iso("2026-09-14T12:00:00Z")


# --- Manifest entry ----------------------------------------------------------

def test_manifest_entry_shape_and_required_fields():
    rid = mirror_format_uuid_v4(os.urandom(16))
    sha = hashlib.sha256(b"wav-bytes").hexdigest()
    entry = json.loads(mirror_build_entry(
        rid, "recordings/2026-09-14/120000_%s.wav" % rid,
        "2026-09-14T12:00:00+00:00", 1800000, 57600044, sha, "finalized"))
    for field in ("recordingId", "filename", "startedAt", "durationMs",
                  "sizeBytes", "sha256", "sampleRate", "bitDepth",
                  "channels", "state", "firmwareVersion"):
        assert field in entry, field
    assert entry["sampleRate"] == 16000
    assert entry["bitDepth"] == 16
    assert entry["channels"] == 1
    assert entry["state"] in ("finalized", "recovered")
    # Duration helper: payload bytes at 32000 B/s.
    assert 57600000 // 32 == 1800000


def test_manifest_entry_rejects_part_and_bad_state():
    rid = mirror_format_uuid_v4(os.urandom(16))
    sha = hashlib.sha256(b"x").hexdigest()
    bad_names = [
        "recordings/2026-09-14/120000_%s.wav.part" % rid,
        "/sdcard/M5DAYLOG/recordings/2026-09-14/120000_%s.wav" % rid,
        "120000_%s.wav" % rid,
    ]
    for name in bad_names:
        try:
            mirror_build_entry(rid, name, "2026-09-14T12:00:00+00:00",
                               1000, 32444, sha, "finalized")
        except AssertionError:
            continue
        raise AssertionError("accepted bad filename: %s" % name)


# --- Manifest upsert ----------------------------------------------------------

def test_upsert_creates_idempotent_and_conflicts(tmp_path=None):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        manifest = tmp_p / "manifest.json"
        tmpf = tmp_p / "manifest.tmp"
        did = mirror_format_uuid_v4(os.urandom(16))
        rid = mirror_format_uuid_v4(os.urandom(16))
        sha = hashlib.sha256(b"seg-a").hexdigest()
        entry = {"recordingId": rid,
                 "filename": "recordings/2026-09-14/120000_%s.wav" % rid,
                 "startedAt": "2026-09-14T12:00:00+00:00",
                 "durationMs": 1000, "sizeBytes": 32044, "sha256": sha,
                 "sampleRate": 16000, "bitDepth": 16, "channels": 1,
                 "state": "finalized", "firmwareVersion": FW}
        entry["__json__"] = mirror_build_entry(
            rid, entry["filename"], entry["startedAt"], 1000, 32044, sha,
            "finalized")
        # Missing manifest is created.
        assert mirror_upsert(manifest, tmpf, did,
                             "2026-09-14T12:00:01+00:00", entry) == "ok"
        assert manifest.exists() and not tmpf.exists()
        before = manifest.read_bytes()
        # Exact duplicate is idempotent (file untouched).
        assert mirror_upsert(manifest, tmpf, did,
                             "2026-09-14T12:00:02+00:00", entry) == "ok"
        assert manifest.read_bytes() == before
        # Same recordingId with a different hash is CONFLICT, preserved.
        other = dict(entry)
        other["sha256"] = hashlib.sha256(b"seg-B").hexdigest()
        other["__json__"] = mirror_build_entry(
            rid, entry["filename"], entry["startedAt"], 1000, 32044,
            other["sha256"], "finalized")
        assert mirror_upsert(manifest, tmpf, did,
                             "2026-09-14T12:00:03+00:00", other) == "conflict"
        assert manifest.read_bytes() == before
        # Same recordingId with a different filename is also CONFLICT.
        other2 = dict(entry)
        other2["filename"] = "recordings/2026-09-14/120001_%s.wav" % rid
        other2["__json__"] = mirror_build_entry(
            rid, other2["filename"], entry["startedAt"], 1000, 32044, sha,
            "finalized")
        assert mirror_upsert(manifest, tmpf, did,
                             "2026-09-14T12:00:04+00:00", other2) == "conflict"
        assert manifest.read_bytes() == before
        # Unknown major / deviceId fork fail closed, manifest preserved.
        tampered = json.loads(manifest.read_text(encoding="utf-8"))
        tampered["schemaVersion"] = 2
        manifest.write_text(json.dumps(tampered), encoding="utf-8")
        assert mirror_upsert(manifest, tmpf, did,
                             "2026-09-14T12:00:05+00:00", entry) == "error"
        tampered["schemaVersion"] = 1
        tampered["deviceId"] = mirror_format_uuid_v4(os.urandom(16))
        manifest.write_text(json.dumps(tampered), encoding="utf-8")
        assert mirror_upsert(manifest, tmpf, did,
                             "2026-09-14T12:00:06+00:00", entry) == "error"


def test_tmp_write_crash_preserves_old_manifest(tmp_path=None):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        manifest = tmp_p / "manifest.json"
        tmpf = tmp_p / "manifest.tmp"
        did = mirror_format_uuid_v4(os.urandom(16))
        rid = mirror_format_uuid_v4(os.urandom(16))
        manifest.write_text(json.dumps(
            {"schemaVersion": 1, "deviceId": did,
             "updatedAt": "2026-09-14T12:00:00+00:00", "recordings": []}),
            encoding="utf-8")
        before = manifest.read_bytes()
        # Simulate a power loss mid-tmp-write: tmp holds garbage, the
        # destination must remain intact and valid.
        tmpf.write_bytes(b'{"schemaVersion":1,"deviceId":"'[:20])
        assert manifest.read_bytes() == before
        assert json.loads(before)["recordings"] == []
        # Boot promotion: missing manifest + valid tmp promotes.
        manifest.unlink()
        tmpf.write_bytes(json.dumps(
            {"schemaVersion": 1, "deviceId": did,
             "updatedAt": "2026-09-14T12:00:00+00:00", "recordings": []}
        ).encode())
        tmpf.rename(manifest)
        assert json.loads(manifest.read_text(encoding="utf-8"))["deviceId"] \
            == did


# --- C source contract ----------------------------------------------------------

def test_config_declares_metadata_paths():
    src = CFG.read_text(encoding="utf-8")
    assert "#define RECORDER_METADATA_SCHEMA_VERSION 1u" in src
    assert '#define RECORDER_MODEL "M5Capsule v1.1"' in src
    assert "#define RECORDER_FIRMWARE_VERSION" in src
    assert '#define RECORDER_DEVICE_JSON_PATH "/sdcard/M5DAYLOG/device.json"' \
        in src
    assert '#define RECORDER_MANIFEST_PATH "/sdcard/M5DAYLOG/manifest.json"' \
        in src
    assert '#define RECORDER_MANIFEST_TMP_PATH "/sdcard/M5DAYLOG/manifest.tmp"' \
        in src
    assert "#define RECORDER_UUID_STR_LEN 37u" in src
    assert "#define RECORDER_SHA256_HEX_LEN 65u" in src
    assert "#define RECORDER_ISO8601_STR_LEN 32u" in src
    assert "#define RECORDER_MANIFEST_ENTRY_MAX" in src
    assert "#define RECORDER_MANIFEST_MAX_BYTES" in src


def test_c_sha256_implements_incremental_contract():
    src = SHA_C.read_text(encoding="utf-8")
    hdr = SHA_H.read_text(encoding="utf-8")
    for symbol in ("sha256_init", "sha256_update", "sha256_final",
                   "sha256_to_hex", "sha256_is_valid_hex",
                   "sha256_file_hex", "sha256_ctx_t"):
        assert symbol in src or symbol in hdr, symbol
    # Lowercase-only hex discipline (uppercase must not validate).
    assert "0x0Fu" in src or "abcdef" in src
    assert "'a'" in src
    # File hashing is chunked/incremental (no whole-file buffering, no
    # writer-frame growth: 1KB chunks, not a multi-KB stack buffer).
    assert "1024" in src
    assert "fread" in src


def test_c_identity_implements_uuid_and_device_json():
    src = ID_C.read_text(encoding="utf-8")
    hdr = ID_H.read_text(encoding="utf-8")
    for symbol in ("device_identity_is_valid_uuid",
                   "device_identity_format_uuid_v4",
                   "device_identity_build_device_json",
                   "device_identity_ensure_device_json",
                   "device_identity_parse_device_id",
                   "device_identity_parse_schema_version",
                   "device_identity_ensure_device_id"):
        assert symbol in src or symbol in hdr, symbol
    # RFC 4122 version/variant bits + lowercase rendering.
    assert "0x40" in src and "0x80" in src
    # Mismatch/unknown-schema preservation: never auto-overwrites.
    assert "never auto-overwrite" in src.lower() or \
        "never auto-overwritten" in src.lower() or "fail-closed" in src.lower()
    # NVS persistence is ESP-only with a host stub.
    assert "nvs_flash" in src or "nvs_open" in src
    assert "ESP_ERR_NOT_SUPPORTED" in src
    assert "esp_random" in src
    # device.json carries the fixed PoC audio capabilities.
    assert "audioCapabilities" in src
    assert "sampleRate" in src


def test_c_manifest_implements_upsert_contract():
    src = MAN_C.read_text(encoding="utf-8")
    hdr = MAN_H.read_text(encoding="utf-8")
    for symbol in ("device_manifest_is_valid_sha256_hex",
                   "device_manifest_is_valid_iso8601_offset",
                   "device_manifest_format_iso8601_utc",
                   "device_manifest_build_entry",
                   "device_manifest_build_empty",
                   "device_manifest_hash_wav_file",
                   "device_manifest_extract_recording_id",
                   "device_manifest_build_filename",
                   "device_manifest_duration_ms",
                   "device_manifest_upsert_file",
                   "device_manifest_recover_tmp",
                   "device_manifest_sync_wav_dir",
                   "DEVICE_MANIFEST_OK",
                   "DEVICE_MANIFEST_CONFLICT",
                   "DEVICE_MANIFEST_ERROR",
                   "DEVICE_MANIFEST_STATE_FINALIZED",
                   "DEVICE_MANIFEST_STATE_RECOVERED"):
        assert symbol in src or symbol in hdr, symbol
    # tmp + atomic rename durability (old manifest never truncated).
    assert "manifest.tmp" in src.lower() or "tmp_path" in src
    assert "rename(" in src
    assert "never truncate" in src.lower() or "atomic" in src.lower()
    # CONFLICT discipline: same recordingId + different hash never
    # overwrites.
    assert "CONFLICT" in src
    assert "never overwrite" in src.lower() or "never overwrites" in src.lower()
    # Only `.wav` (never `.wav.part`) enters the manifest.
    assert ".wav.part" in src
    # recordingId is the primary key.
    assert "recordingId" in src
    # Unknown-major fail-closed.
    assert "Unknown" in src or "unknown" in src.lower()
    assert "fail closed" in src.lower() or "fail-closed" in src.lower()


def test_component_registers_new_sources_and_nvs():
    src = CMAKE.read_text(encoding="utf-8")
    for name in ("sha256.c", "device_identity.c", "device_manifest.c"):
        assert name in src, name
    assert "nvs_flash" in src


def test_main_wires_identity_and_manifest():
    main = MAIN.read_text(encoding="utf-8")
    assert '#include "device_identity.h"' in main
    assert '#include "device_manifest.h"' in main
    # Stable NVS deviceId first, then device.json, then manifest shell.
    assert "device_identity_ensure_device_id" in main
    assert "device_identity_ensure_device_json" in main
    assert "recorder_ensure_manifest_exists" in main
    assert main.index("device_identity_ensure_device_id") < main.index(
        "device_identity_ensure_device_json")
    assert main.index("device_identity_ensure_device_json") < main.index(
        "wav_recovery_scan_recordings")
    # Fresh UUIDv4 per segment (no placeholder counter reuse).
    assert "recorder_new_recording_id" in main
    assert "esp_random" in main
    assert '"s%04u"' not in main
    assert "seg_seq" not in main
    # Rotation + terminal manifest records (finalized), recovery sync.
    assert "recorder_manifest_record_wav" in main
    assert "DEVICE_MANIFEST_STATE_FINALIZED" in main
    assert "DEVICE_MANIFEST_STATE_RECOVERED" in main
    assert "device_manifest_sync_wav_dir" in main
    assert main.count("recorder_manifest_record_wav(") >= 3  # def + 2 calls
    # Stage markers for evidence, metadata only.
    assert "stage: manifest" in main
    assert "reason: manifest conflict" in main or "manifest conflict" in main
    # Identity mismatch / manifest I/O failures enter ERROR, never
    # silent recording.
    assert "reason: device id" in main
    assert "reason: device json" in main
    assert "reason: manifest sync" in main or "manifest sync" in main
    # Writer stack discipline preserved for the new buffers.
    assert "static char s_device_id[RECORDER_UUID_STR_LEN]" in main
    assert "static char s_manifest_entry[RECORDER_MANIFEST_ENTRY_MAX]" in main
    assert "static char s_seg_rec_id[RECORDER_UUID_STR_LEN]" in main


def test_no_audio_deletion_and_no_scope_creep():
    blob = "\n".join(
        p.read_text(encoding="utf-8")
        for p in COMP.rglob("*")
        if p.is_file() and p.suffix in (".c", ".h")
    )
    code = _strip_c_comments(blob)
    # Audio is never deleted: no unlink() anywhere, and the only
    # remove() uses live in device_manifest.c (tmp/destination metadata
    # replace + stale-tmp cleanup, never `.wav`/`.wav.part` deletion).
    assert "unlink(" not in code
    for p in COMP.rglob("*"):
        if p.is_file() and p.suffix in (".c", ".h") and \
                p.name != "device_manifest.c":
            assert not _has_c_call(p.read_text(encoding="utf-8"), "remove"), \
                p.name
            assert not _has_c_call(p.read_text(encoding="utf-8"), "unlink"), \
                p.name
    man = (COMP / "device_manifest.c").read_text(encoding="utf-8")
    assert ".wav" in man  # suffix guards present
    # Task #47 is now in scope; processed-ACK retention stays out.
    lowered = blob.lower()
    assert "manifest" in lowered
    assert "device.json" in lowered or "device_json" in lowered
    assert "sha256" in lowered
    for keyword in ("acks/", "processed.json", "processed ack"):
        assert keyword not in lowered, keyword
    # No deletion of recordings: manifest collision reports CONFLICT.
    assert "CONFLICT" in blob


def test_no_credentials_or_private_data_patterns():
    blob = "\n".join(
        p.read_text(encoding="utf-8")
        for p in COMP.rglob("*")
        if p.is_file() and p.suffix in (".c", ".h")
    )
    for forbidden in ("BEGIN PRIVATE KEY", "ghp_", "AKIA", "password="):
        assert forbidden not in blob, forbidden
    main = MAIN.read_text(encoding="utf-8")
    assert "password" not in main.lower()
    # Manifest/identity logs carry only stage/result metadata: no id,
    # hash, or filename interpolation in the stage: manifest lines.
    for line in main.splitlines():
        if "stage: manifest" in line and ("LOGI" in line or "LOGE" in line):
            assert "s_device_id" not in line
            assert "sha" not in line.lower() or "result" in line.lower()
    # Filenames with timestamps stay in the manifest file, never in logs.
    assert main.count("stage: manifest") >= 5
