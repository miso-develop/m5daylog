"""#52 Device ACK retention policy and manual-WAKE boundary regressions.

Compiles and executes the production pure-C retention predicate. This file
uses only synthetic timestamps and identifiers; it never opens a device.
"""
import ctypes
from pathlib import Path
import shutil
import subprocess

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
SRC = COMP / "processed_ack.c"
POLICY = COMP / "processed_ack_policy.c"
RUNTIME = REPO / "firmware/main/task49_runtime.c"


def test_retention_predicate_native(tmp_path):
    cc = shutil.which("cc") or shutil.which("gcc")
    assert cc is not None, "Firmware CI requires a C compiler"
    library = tmp_path / "ack_policy.so"
    subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared",
         "-fPIC", "-I", str(COMP / "include"), str(POLICY),
         "-o", str(library)],
        check=True, capture_output=True, text=True,
    )
    lib = ctypes.CDLL(str(library))
    fn = lib.processed_ack_retention_eligible
    fn.argtypes = [ctypes.c_bool, ctypes.c_int64, ctypes.c_int64,
                   ctypes.c_uint]
    fn.restype = ctypes.c_bool
    week = 7 * 86400
    now = 2000000000
    # 7-day boundary is exclusive, while 80%-usage boundary is inclusive.
    assert not fn(True, now - week, now, 79)
    assert fn(True, now - week - 1, now, 79)
    assert fn(True, now - 1, now, 80)
    assert not fn(False, now - week - 1, now, 100)
    assert not fn(False, now - 1, now, 80)
    assert not fn(True, now + 10, now, 100)
    assert not fn(True, -1, now, 100)
    assert not fn(True, now - 1, now, 101)
    assert not fn(True, now - 1, now, 0)
    assert fn(True, now - 1, now, 100)


def test_production_ack_ingest_seam_does_not_touch_usb_owned_media():
    runtime = RUNTIME.read_text(encoding="utf-8")
    source = SRC.read_text(encoding="utf-8")
    start = runtime.index("bool recorder_task87_manifest_sync_wav_dir(")
    end = runtime.index("static void recorder_base_task(", start)
    seam = runtime[start:end]
    assert "task87_wake_recovery_pending(&s_wake_recovery)" in seam
    assert "processed_ack_apply_manual_wake(device_id, &ack_stats)" in seam
    assert seam.index("device_manifest_sync_wav_dir(") < seam.index(
        "processed_ack_apply_manual_wake(")
    assert seam.index("processed_ack_apply_manual_wake(") < seam.index(
        "task87_wake_recovery_complete_device_recovery(")
    assert "#define ACK_FILE RECORDER_M5DAYLOG_DIR \"/acks/processed.json\"" in source
    assert "acks/processed.json.tmp" not in source
    assert "processed-state" in source
    assert "fsync(fileno(fp))" in source
    assert "rename(tmp, path)" in source
    assert "processed_ack_retention_eligible(" in source
    # PoC strategy: evaluate retention without touching source audio.
    assert "remove(" not in source and "unlink(" not in source
    assert "deletedRaw\\\":0" in source or "deletedRaw\":0" in source


def test_ack_preflight_checks_and_safe_unknown_handling():
    source = SRC.read_text(encoding="utf-8")
    assert "valid_ack(ack, device_id)" in source
    assert "manifest_recordings(manifest, device_id)" in source
    assert "device_identity_is_valid_uuid(r->valuestring)" in source
    assert "only_unique_keys(ack)" in source
    assert "lookup_record(entries, id) == NULL" in source
    assert "stats.unknown_ids++" in source
    assert "marker_status(id) == 1" in source
    assert "if (!sd_mount_is_mounted()" in source
    assert "return true;  // invalid ACK does not prevent new raw recording" in source
