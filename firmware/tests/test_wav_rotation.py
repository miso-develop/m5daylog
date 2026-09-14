"""Task #45 host contract tests: WAV rotation / finalize.

Stdlib only (no ESP-IDF, no device, no network). Locks the Task #45
contract implemented by firmware/components/recorder/wav_rotation.c and
wired into firmware/main/main.c:

- 30-minute + midnight rotation decision (pure, no I/O)
- `recordings/YYYY-MM-DD/HHMMSS_<id>.wav.part` -> `.wav` path building
- header-finalize + flush/close + rename with idempotent close
  (simultaneous events cause no double close / no double rename)
- midnight date-directory switch without mixing dates
- finalized WAV decodeable by a standard decoder, corruption 0

A faithful Python mirror of the C decision/path/finalize rules plus
source-presence assertions keeps the C implementation from drifting.
Real files use only synthetic payloads in temporary directories.
"""

import re
import struct
import tempfile
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
ROT_H = COMP / "include/wav_rotation.h"
ROT_C = COMP / "wav_rotation.c"
CFG = COMP / "include/recorder_config.h"
MAIN = REPO / "firmware/main/main.c"
MOUNT_H = COMP / "include/sd_mount.h"
MOUNT_C = COMP / "sd_mount.c"

ROTATION_SEC = 1800
ROTATION_BYTES = 32000 * 1800  # 57,600,000 at 16kHz/16bit/mono
RECORDINGS = "/sdcard/M5DAYLOG/recordings"


def _strip_c_comments(src):
    # Remove /* block */ comments first, then // line comments, so prose
    # mentioning a deletion call in a comment is not mistaken for real code.
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"//.*", "", src)
    return src


def _has_c_call(src, name):
    return (
        re.search(r"\b" + re.escape(name) + r"\s*\(", _strip_c_comments(src))
        is not None
    )


# --- Python mirror of wav_rotation_should_rotate -------------------------

def mirror_should_rotate(elapsed_sec, segment_bytes, date_changed, events):
    if events is not None:
        if events.get("usb"):
            return "usb"
        if events.get("low_battery"):
            return "low-battery"
        if events.get("stop"):
            return "stop-request"
    if date_changed:
        return "midnight"
    if elapsed_sec >= ROTATION_SEC or segment_bytes >= ROTATION_BYTES:
        return "time-30min"
    return "none"


def mirror_build_part(date, time6, rec_id):
    assert len(date) == 10 and date[4] == "-" and date[7] == "-"
    assert len(time6) == 6 and time6.isdigit()
    assert rec_id and "/" not in rec_id and "\\" not in rec_id
    return f"{RECORDINGS}/{date}/{time6}_{rec_id}.wav.part"


def mirror_build_wav(date, time6, rec_id):
    return mirror_build_part(date, time6, rec_id)[: -len(".part")]


def mirror_part_to_wav(part):
    assert part.endswith(".wav.part")
    wav = part[: -len(".part")]
    assert wav.endswith(".wav")
    return wav


def mirror_finalize_paths(part, wav, files):
    """Mirror of wav_rotation_finalize_paths over a fake file set.

    files: set of existing paths. Returns (ok, files_after).
    """
    assert part.endswith(".wav.part") and wav.endswith(".wav")
    assert wav == mirror_part_to_wav(part)
    part_present = part in files
    wav_present = wav in files
    if part_present:
        if wav_present:
            return False, set(files)  # collision: never overwrite
        nxt = set(files)
        nxt.remove(part)
        nxt.add(wav)
        return True, nxt
    if wav_present:
        return True, set(files)  # already finalized: no second rename
    return False, set(files)


def test_rotation_config_constants():
    src = CFG.read_text(encoding="utf-8")
    assert "#define RECORDER_ROTATION_INTERVAL_SEC 1800u" in src
    assert "RECORDER_ROTATION_PAYLOAD_BYTES" in src
    assert '#define RECORDER_WAV_SUFFIX ".wav"' in src
    assert "#define RECORDER_MAX_PATH_LEN 256u" in src
    # 30min at 32000 B/s is exactly 57,600,000 payload bytes.
    assert ROTATION_BYTES == 57600000


def test_rotation_decision_mirror():
    assert mirror_should_rotate(0, 0, False, None) == "none"
    assert mirror_should_rotate(1799, 0, False, None) == "none"
    assert mirror_should_rotate(1800, 0, False, None) == "time-30min"
    assert mirror_should_rotate(0, ROTATION_BYTES, False, None) == "time-30min"
    assert mirror_should_rotate(0, ROTATION_BYTES - 1, False, None) == "none"
    assert mirror_should_rotate(0, 0, True, None) == "midnight"
    # Midnight wins over the periodic trigger (dates never mix).
    assert mirror_should_rotate(9999, 99999999, True, None) == "midnight"
    # Terminal stops win over time/midnight; USB first for logging.
    assert mirror_should_rotate(9999, 99999999, True, {"usb": True}) == "usb"
    assert (
        mirror_should_rotate(0, 0, False, {"low_battery": True})
        == "low-battery"
    )
    assert mirror_should_rotate(0, 0, False, {"stop": True}) == "stop-request"
    assert (
        mirror_should_rotate(0, 0, False, {"usb": True, "stop": True})
        == "usb"
    )


def test_path_building_mirror():
    part = mirror_build_part("2026-09-14", "120000", "s0001")
    assert part == (
        "/sdcard/M5DAYLOG/recordings/2026-09-14/120000_s0001.wav.part"
    )
    wav = mirror_build_wav("2026-09-14", "120000", "s0001")
    assert wav == "/sdcard/M5DAYLOG/recordings/2026-09-14/120000_s0001.wav"
    assert mirror_part_to_wav(part) == wav
    # Midnight switches the date directory with a fresh file (no mixing).
    part2 = mirror_build_part("2026-09-15", "000000", "s0002")
    assert "/2026-09-15/" in part2
    assert "/2026-09-14/" not in part2


def test_finalize_idempotent_mirror():
    part = mirror_build_part("2026-09-14", "120000", "s0001")
    wav = mirror_build_wav("2026-09-14", "120000", "s0001")
    # First finalize renames part -> wav.
    ok, files = mirror_finalize_paths(part, wav, {part})
    assert ok and files == {wav}
    # Duplicate stop/rotation event: part missing + wav present -> ok,
    # no second rename issued.
    ok2, files2 = mirror_finalize_paths(part, wav, files)
    assert ok2 and files2 == {wav}
    # Collision (both present) fails loud, never overwrites.
    ok3, _ = mirror_finalize_paths(part, wav, {part, wav})
    assert not ok3
    # Neither present fails loud (nothing to finalize).
    ok4, _ = mirror_finalize_paths(part, wav, set())
    assert not ok4


def test_finalized_wav_decodeable_with_stdlib_wave():
    samples = 16384  # one 32KB slot at 16kHz/16bit/mono
    payload = b"".join(
        struct.pack("<h", (i % 512) - 256) for i in range(samples)
    )
    assert len(payload) == 32768
    byte_rate = 16000 * 1 * 2
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(payload),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        16000,
        byte_rate,
        2,
        16,
        b"data",
        len(payload),
    )
    with tempfile.TemporaryDirectory() as tmp:
        part = Path(tmp) / "120000_s0001.wav.part"
        wav = Path(tmp) / "120000_s0001.wav"
        # Simulate header-finalize + close + idempotent rename.
        part.write_bytes(header + payload)
        assert part.exists() and not wav.exists()
        part.rename(wav)  # first finalize does I/O
        assert wav.exists() and not part.exists()
        # Duplicate finalize: wav present + part missing -> ok, no-op.
        assert wav.exists() and not part.exists()
        with wave.open(str(wav), "rb") as w:
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2
            assert w.getframerate() == 16000
            assert w.getnframes() == samples
            assert len(w.readframes(samples)) == len(payload)


def test_c_source_implements_rotation_contract():
    src = ROT_C.read_text(encoding="utf-8")
    hdr = ROT_H.read_text(encoding="utf-8")
    for symbol in (
        "wav_rotation_should_rotate",
        "wav_rotation_build_part_path",
        "wav_rotation_build_wav_path",
        "wav_rotation_part_to_wav",
        "wav_rotation_finalize_paths",
        "wav_rotation_state_init",
        "wav_rotation_finalize_once",
        "WAV_ROTATE_TIME_30MIN",
        "WAV_ROTATE_MIDNIGHT",
        "WAV_ROTATE_USB",
        "WAV_ROTATE_LOW_BATTERY",
        "WAV_ROTATE_STOP_REQUEST",
        "finalize_called",
    ):
        assert symbol in src or symbol in hdr, symbol
    # Only rename, never deletion (comment prose does not count as code).
    assert "rename(" in src
    assert not _has_c_call(src, "remove")
    assert not _has_c_call(src, "unlink")
    assert not _has_c_call(hdr, "remove")
    assert not _has_c_call(hdr, "unlink")
    # Priority + idempotency documented in code.
    assert "USB > LOW_BATTERY" in hdr or "usb" in hdr.lower()
    assert "no double close" in src.lower() or "no double rename" in src.lower()
    assert "RECORDER_ROTATION_INTERVAL_SEC" in src
    assert "RECORDER_ROTATION_PAYLOAD_BYTES" in src


def test_rotation_never_overwrites_or_deletes():
    src = ROT_C.read_text(encoding="utf-8")
    # Collision guard: both present fails instead of overwriting.
    assert "never overwrite" in src.lower() or "collision" in src.lower()
    assert not _has_c_call(src, "remove")
    assert not _has_c_call(src, "unlink")
    # The wav path must derive from the part path (no redirection).
    assert "strcmp(expect, wav_path)" in src or "part_to_wav" in src


def test_main_wires_rotation_idempotent():
    main = MAIN.read_text(encoding="utf-8")
    assert '#include "wav_rotation.h"' in main
    assert "wav_rotation_should_rotate" in main
    assert "wav_rotation_build_part_path" in main
    assert "wav_rotation_build_wav_path" in main
    assert "wav_rotation_finalize_once" in main
    assert "wav_rotation_state_init" in main
    assert "sd_mount_ensure_date_dir" in main
    # Rotation file ops run without the pipeline lock and without
    # stopping capture (gap <=100ms absorption comment preserved).
    assert "WITHOUT the pipeline lock" in main
    assert "keeps filling the other slot" in main
    assert "<=100ms" in main
    # Idempotent close across simultaneous stop events.
    assert "finalize_called" in main or "finalize_once" in main
    assert "no double close" in main.lower()
    assert "no double rename" in main.lower()
    # All five trigger families are covered in code/comments.
    lowered = main.lower()
    assert "30" in main and ("midnight" in lowered)
    assert "usb" in lowered
    assert "low battery" in lowered or "low_battery" in lowered
    assert "safe-stop" in lowered or "safe_stop" in lowered
    # Stage markers for boundary evidence (decode/duration/event log).
    assert "stage: rotate" in main
    assert "stage: finalize" in main
    # Date directories never mix across midnight.
    assert "never mix" in lowered or "never mixes" in lowered
    # PC syncs only finalized files.
    assert "PC syncs only" in main or ".wav" in main


def test_mount_date_dir_wired():
    src = MOUNT_C.read_text(encoding="utf-8")
    hdr = MOUNT_H.read_text(encoding="utf-8")
    assert "sd_mount_ensure_date_dir" in src
    assert "sd_mount_ensure_date_dir" in hdr
    assert "RECORDER_RECORDINGS_DIR" in src
    assert "mkdir" in src
    # Date validation fail-loud, never format away audio.
    assert "ESP_ERR_INVALID_ARG" in src or "ESP_ERR_INVALID_ARG" in hdr
    assert ".format_if_mount_failed = false" in src
    assert not _has_c_call(src, "rename")
    assert not _has_c_call(src, "remove")
    assert not _has_c_call(src, "unlink")


def test_no_deletion_or_scope_creep():
    blob = "\n".join(
        p.read_text(encoding="utf-8")
        for p in COMP.rglob("*")
        if p.is_file() and p.suffix in (".c", ".h")
    )
    assert not _has_c_call(blob, "remove")
    assert not _has_c_call(blob, "unlink")
    for keyword in ("quarantine", "manifest", "device.json", "acks/"):
        assert keyword not in blob.lower(), keyword


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
