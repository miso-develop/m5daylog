"""Task #46 host contract tests: power-loss recovery / quarantine.

Stdlib only (no ESP-IDF, no device, no network). Locks the Task #46
contract implemented by firmware/components/recorder/wav_recovery.c and
wired into firmware/main/main.c + sd_mount.c:

- residual `.wav.part` scan at boot, header rebuilt from payload length
- odd-tail truncation to the 16bit sample boundary (tail only)
- unrecoverable files moved to `quarantine/` with rename() only, never
  auto-deleted (no remove/unlink anywhere)
- destination collision never overwrites (quarantine instead)
- recovery results appended to `events.jsonl` (counts/sizes only)
- fail-loud: fatal scan/dir failures enter ERROR, never silent recording

A faithful Python mirror of the C header/path/recovery rules plus
source-presence assertions keeps the C implementation from drifting.
Real files use only synthetic payloads in temporary directories.
"""

import json
import re
import struct
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
REC_H = COMP / "include/wav_recovery.h"
REC_C = COMP / "wav_recovery.c"
CFG = COMP / "include/recorder_config.h"
MAIN = REPO / "firmware/main/main.c"
MOUNT_H = COMP / "include/sd_mount.h"
MOUNT_C = COMP / "sd_mount.c"

SAMPLE_RATE = 16000
CHANNELS = 1
BITS = 16
HEADER = 44


def _strip_c_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"//.*", "", src)
    return src


def _has_c_call(src, name):
    return (
        re.search(r"\b" + re.escape(name) + r"\s*\(", _strip_c_comments(src))
        is not None
    )


def build_header(pcm_bytes):
    byte_rate = SAMPLE_RATE * CHANNELS * (BITS // 8)
    block_align = CHANNELS * (BITS // 8)
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + pcm_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        CHANNELS,
        SAMPLE_RATE,
        byte_rate,
        block_align,
        BITS,
        b"data",
        pcm_bytes,
    )


def synth_payload(num_samples, seed=0):
    return b"".join(
        struct.pack("<h", ((i + seed) % 512) - 256)
        for i in range(num_samples)
    )


def mirror_validate_header(header: bytes) -> bool:
    if len(header) < 44:
        return False
    if header[0:4] != b"RIFF":
        return False
    if header[8:12] != b"WAVE":
        return False
    if header[12:16] != b"fmt ":
        return False
    if struct.unpack("<I", header[16:20])[0] != 16:
        return False
    if struct.unpack("<H", header[20:22])[0] != 1:
        return False
    if struct.unpack("<H", header[22:24])[0] != 1:
        return False
    if struct.unpack("<I", header[24:28])[0] != 16000:
        return False
    if struct.unpack("<I", header[28:32])[0] != 32000:
        return False
    if struct.unpack("<H", header[32:34])[0] != 2:
        return False
    if struct.unpack("<H", header[34:36])[0] != 16:
        return False
    if header[36:40] != b"data":
        return False
    return True


def mirror_recover_file(part: Path, wav: Path, quarantine: Path):
    """Mirror of wav_recovery_recover_file. Returns outcome string."""
    assert str(part).endswith(".wav.part")
    assert str(wav).endswith(".wav")
    assert str(wav) == str(part)[: -len(".part")]
    if not part.exists():
        if wav.exists():
            return "recovered"
        return "error"
    if wav.exists():
        # Collision: never overwrite, quarantine instead.
        dest = quarantine / part.name
        part.rename(dest)
        return "quarantined"
    size = part.stat().st_size
    if size < 44:
        dest = quarantine / part.name
        part.rename(dest)
        return "quarantined"
    header = part.read_bytes()[:44]
    if not mirror_validate_header(header):
        dest = quarantine / part.name
        part.rename(dest)
        return "quarantined"
    payload = size - 44
    recovered = payload - (payload % 2)
    data = part.read_bytes()[44 : 44 + recovered]
    part.write_bytes(build_header(recovered) + data)
    part.rename(wav)
    return "recovered"


def test_config_declares_recovery_paths():
    src = CFG.read_text(encoding="utf-8")
    assert '#define RECORDER_QUARANTINE_DIR "/sdcard/M5DAYLOG/quarantine"' in src
    assert '#define RECORDER_EVENTS_PATH "/sdcard/M5DAYLOG/events.jsonl"' in src


def test_header_validation_mirror():
    assert mirror_validate_header(build_header(0))
    assert mirror_validate_header(build_header(32768))
    bad = bytearray(build_header(100))
    bad[0:4] = b"RIFX"
    assert not mirror_validate_header(bytes(bad))
    bad2 = bytearray(build_header(100))
    bad2[22] = 2  # stereo instead of mono
    assert not mirror_validate_header(bytes(bad2))
    bad3 = bytearray(build_header(100))
    bad3[24:28] = struct.pack("<I", 8000)
    assert not mirror_validate_header(bytes(bad3))
    # Stored sizes are ignored: stale sizes still validate.
    stale = bytearray(build_header(0))
    # pretend the payload is 100 bytes but the header still says 0
    assert mirror_validate_header(bytes(stale))
    assert len(bytes(stale)) == 44


def test_recover_valid_part_rebuilds_header_and_decodes(tmp_path=None):
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        quarantine = tmp_p / "quarantine"
        quarantine.mkdir()
        # Simulate a power loss with a stale header (sizes zeroed) plus
        # two full slots of synthetic PCM.
        payload = synth_payload(16384 * 2)
        assert len(payload) == 65536
        part = tmp_p / "120000_s0001.wav.part"
        wav = tmp_p / "120000_s0001.wav"
        part.write_bytes(build_header(0) + payload)
        outcome = mirror_recover_file(part, wav, quarantine)
        assert outcome == "recovered"
        assert wav.exists() and not part.exists()
        assert list(quarantine.iterdir()) == []
        with wave.open(str(wav), "rb") as w:
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2
            assert w.getframerate() == 16000
            assert w.getnframes() == 32768
            assert len(w.readframes(32768)) == len(payload)


def test_recover_truncates_only_odd_tail(tmp_path=None):
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        quarantine = tmp_p / "quarantine"
        quarantine.mkdir()
        payload = synth_payload(100) + b"\xFF"  # 200 + 1 odd byte
        assert len(payload) == 201
        part = tmp_p / "120001_s0002.wav.part"
        wav = tmp_p / "120001_s0002.wav"
        part.write_bytes(build_header(0) + payload)
        outcome = mirror_recover_file(part, wav, quarantine)
        assert outcome == "recovered"
        with wave.open(str(wav), "rb") as w:
            assert w.getnframes() == 100
        # Only the single tail byte was discarded.
        assert wav.stat().st_size == 44 + 200


def test_recover_quarantines_too_small_and_garbage():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        quarantine = tmp_p / "quarantine"
        quarantine.mkdir()
        # Too small: cannot contain a header.
        tiny = tmp_p / "120002_s0003.wav.part"
        tiny_wav = tmp_p / "120002_s0003.wav"
        tiny.write_bytes(b"\x00" * 10)
        assert mirror_recover_file(tiny, tiny_wav, quarantine) == "quarantined"
        assert not tiny.exists() and not tiny_wav.exists()
        assert (quarantine / tiny.name).exists()
        # Garbage header: not a WAV part we wrote.
        garbage = tmp_p / "120003_s0004.wav.part"
        garbage_wav = tmp_p / "120003_s0004.wav"
        garbage.write_bytes(b"\xAA" * 100)
        assert (
            mirror_recover_file(garbage, garbage_wav, quarantine)
            == "quarantined"
        )
        assert (quarantine / garbage.name).exists()
        assert not garbage_wav.exists()
        # Quarantine files are never auto-deleted: both remain.
        assert len(list(quarantine.iterdir())) == 2


def test_recover_never_overwrites_finalized_wav():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        quarantine = tmp_p / "quarantine"
        quarantine.mkdir()
        payload = synth_payload(16)
        part = tmp_p / "120004_s0005.wav.part"
        wav = tmp_p / "120004_s0005.wav"
        wav.write_bytes(build_header(len(payload)) + payload)
        before = wav.read_bytes()
        # A stale duplicate part with different content arrives.
        other = synth_payload(16, seed=99)
        part.write_bytes(build_header(0) + other)
        assert mirror_recover_file(part, wav, quarantine) == "quarantined"
        # Original finalized file untouched, duplicate isolated.
        assert wav.read_bytes() == before
        assert (quarantine / part.name).exists()


def test_scan_recovers_date_dirs_and_logs_events():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        recordings = tmp_p / "recordings"
        day_a = recordings / "2026-09-14"
        day_b = recordings / "2026-09-15"
        day_a.mkdir(parents=True)
        day_b.mkdir(parents=True)
        quarantine = tmp_p / "quarantine"
        quarantine.mkdir()
        events = tmp_p / "events.jsonl"
        # Two valid parts on different dates + one garbage part.
        good_a = synth_payload(16384)
        good_b = synth_payload(100)
        pa = day_a / "120000_s0001.wav.part"
        pb = day_b / "000100_s0002.wav.part"
        pg = day_a / "120100_s0003.wav.part"
        pa.write_bytes(build_header(0) + good_a)
        pb.write_bytes(build_header(0) + good_b)
        pg.write_bytes(b"\x00" * 5)
        stats = {"scanned": 0, "recovered": 0, "quarantined": 0, "errors": 0}
        per_file = []
        for part in (pa, pb, pg):
            wav = part.parent / (part.name[: -len(".part")])
            oc = mirror_recover_file(part, wav, quarantine)
            stats["scanned"] += 1
            per_file.append((part.name, oc))
            if oc == "recovered":
                stats["recovered"] += 1
            elif oc == "quarantined":
                stats["quarantined"] += 1
            else:
                stats["errors"] += 1
            events_line = {
                "event": "recovery_file",
                "file": part.name,
                "result": oc,
            }
            with events.open("a", encoding="utf-8") as f:
                f.write(json.dumps(events_line) + "\n")
        with events.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "recovery", **stats}) + "\n")
        assert stats == {
            "scanned": 3,
            "recovered": 2,
            "quarantined": 1,
            "errors": 0,
        }
        # Dates never mix: each recovered file stays in its date dir.
        assert (day_a / "120000_s0001.wav").exists()
        assert (day_b / "000100_s0002.wav").exists()
        assert not pa.exists() and not pb.exists()
        # Events log carries the recovery result (summary + per-file).
        lines = [
            json.loads(line)
            for line in events.read_text(encoding="utf-8").splitlines()
        ]
        assert lines[-1]["event"] == "recovery"
        assert lines[-1]["scanned"] == 3
        assert lines[-1]["recovered"] == 2
        assert lines[-1]["quarantined"] == 1
        assert sum(1 for ln in lines if ln.get("event") == "recovery_file") == 3


def test_c_source_implements_recovery_contract():
    src = REC_C.read_text(encoding="utf-8")
    hdr = REC_H.read_text(encoding="utf-8")
    for symbol in (
        "wav_recovery_recover_file",
        "wav_recovery_scan_recordings",
        "wav_recovery_validate_header",
        "wav_recovery_build_quarantine_path",
        "wav_recovery_ensure_dir",
        "wav_recovery_append_summary_event",
        "wav_recovery_is_part_path",
        "WAV_RECOVERY_RECOVERED",
        "WAV_RECOVERY_QUARANTINED",
        "WAV_RECOVERY_ERROR",
        "wav_recovery_stats_t",
        "scanned",
        "recovered",
        "quarantined",
    ):
        assert symbol in src or symbol in hdr, symbol
    # Only rename moves files; deletion is never allowed.
    assert "rename(" in src
    assert not _has_c_call(src, "remove")
    assert not _has_c_call(src, "unlink")
    assert not _has_c_call(hdr, "remove")
    assert not _has_c_call(hdr, "unlink")
    # Header is rebuilt from the payload length with the fixed PoC format.
    assert "wav_part_build_header" in src
    assert "RECORDER_SAMPLE_RATE_HZ" in src
    # Sample-boundary tail truncation documented in code.
    assert "truncate" in src.lower() or "misaligned" in src.lower()
    # Collision guard: never overwrite an existing finalized file.
    assert "overwrite" in src.lower() or "collision" in src.lower()
    # Events log carries counts/sizes; audio/credential content is out.
    assert "events" in src.lower()
    assert "recovery" in src.lower()


def test_c_recovery_never_deletes():
    blob = "\n".join(
        p.read_text(encoding="utf-8")
        for p in COMP.rglob("*")
        if p.is_file() and p.suffix in (".c", ".h")
    )
    assert not _has_c_call(blob, "remove")
    assert not _has_c_call(blob, "unlink")
    # Recovery/quarantine are now in scope; later manifest/identity stay out.
    lowered = blob.lower()
    assert "quarantine" in lowered
    assert "wav_recovery" in lowered or "wav_recovery" in lowered
    for keyword in ("manifest", "device.json", "acks/"):
        assert keyword not in lowered, keyword


def test_main_wires_boot_recover():
    main = MAIN.read_text(encoding="utf-8")
    assert '#include "wav_recovery.h"' in main
    assert "wav_recovery_scan_recordings" in main
    assert "RECORDER_RECORDINGS_DIR" in main
    assert "RECORDER_QUARANTINE_DIR" in main
    assert "RECORDER_EVENTS_PATH" in main
    assert "sd_mount_ensure_quarantine_dir" in main
    # Fail-loud: fatal scan/dir failures enter ERROR, never silent recording.
    assert "stage: recover" in main
    assert main.count("stage: recover") >= 2
    assert "reason: scan" in main or "recover scan" in main
    assert "mkdir quarantine" in main
    # Recovery runs before the new segment opens (no collision with fresh
    # capture) and recovery counts are logged as metadata only.
    assert main.index("wav_recovery_scan_recordings") < main.index(
        "sd_pcm_sink_open"
    )
    assert "scanned:" in main and "recovered:" in main
    assert "quarantined:" in main
    # Boot order comment preserves the Spec #36 chain.
    assert "RECOVER" in main


def test_mount_creates_quarantine_dir():
    src = MOUNT_C.read_text(encoding="utf-8")
    hdr = MOUNT_H.read_text(encoding="utf-8")
    assert "sd_mount_ensure_quarantine_dir" in src
    assert "sd_mount_ensure_quarantine_dir" in hdr
    assert "RECORDER_QUARANTINE_DIR" in src
    assert "mkdir" in src
    assert "quarantine" in src.lower()
    # Mount teardown still never deletes or renames audio.
    assert not _has_c_call(src, "rename")
    assert not _has_c_call(src, "remove")
    assert not _has_c_call(src, "unlink")
    # Never formats away unacknowledged audio.
    assert ".format_if_mount_failed = false" in src


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
    # Recovery logs carry only counts/sizes/result classes, never audio
    # bytes or transcript content.
    rec = REC_C.read_text(encoding="utf-8")
    assert "pcm_bytes" in rec
    assert "transcript" not in rec.lower()
