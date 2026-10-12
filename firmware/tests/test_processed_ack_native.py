"""Execute the real #52 Device C ingestion/marker logic with fault injection.

All paths are redirected via compile-time sandbox overrides. No actual SD,
device identity, raw WAV, serial port, or physical hardware is accessed.
The system libcjson library is used when available; on minimal CI hosts a
strict synthetic-vector parser implements the used cJSON API. Production
always uses ESP-IDF's cJSON, which is separately compiled in Firmware CI.
Fault wrappers intercept marker fsync / rename.
"""
import ctypes
import ctypes.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMP = ROOT / "firmware/components/recorder"
STUBS = ROOT / "firmware/tests/native/processed_ack_native_stubs.c"
ACK_ID = "11111111-1111-4111-8111-111111111111"
UNKNOWN_ID = "22222222-2222-4222-8222-222222222222"
DEVICE_ID = "33333333-3333-4333-8333-333333333333"
OTHER_DEVICE = "44444444-4444-4444-8444-444444444444"


class Stats(ctypes.Structure):
    _fields_ = [
        ("applied", ctypes.c_uint32),
        ("already_applied", ctypes.c_uint32),
        ("unknown_ids", ctypes.c_uint32),
        ("invalid_payloads", ctypes.c_uint32),
        ("retention_candidates", ctypes.c_uint32),
        ("deleted_raw", ctypes.c_uint32),
    ]


@pytest.fixture(scope="module")
def native_ack(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc")
    libcjson = ctypes.util.find_library("cjson")
    assert cc is not None, "C compiler required for Device native tests"
    build = tmp_path_factory.mktemp("processed-ack-c")
    so = build / "libprocessed_ack_test.so"
    args = [
        cc, "-std=c11", "-D_POSIX_C_SOURCE=200809L",
        "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
        "-I", str(ROOT / "firmware/tests/native/ack_include"),
        "-I", str(COMP / "include"),
        '-DACK_FILE="acks/processed.json"',
        '-DACK_MARKER_DIR="processed-state"',
        '-DACK_MANIFEST_PATH="manifest.json"',
        '-DACK_EVENTS_PATH="events.jsonl"',
        str(COMP / "processed_ack.c"),
        str(COMP / "processed_ack_policy.c"),
        str(STUBS),
        "-Wl,--wrap=fsync", "-Wl,--wrap=rename",
    ]
    if libcjson:
        args.append(f"-l:{libcjson}")
    else:
        args.append(str(ROOT / "firmware/tests/native/processed_ack_cjson_test_shim.c"))
    args.extend(["-o", str(so)])
    subprocess.run(args, check=True, capture_output=True, text=True)
    lib = ctypes.CDLL(str(so))
    lib.processed_ack_apply_manual_wake.argtypes = [
        ctypes.c_char_p, ctypes.POINTER(Stats)
    ]
    lib.processed_ack_apply_manual_wake.restype = ctypes.c_bool
    for name in ("ack_test_set_mounted", "ack_test_fail_fsync",
                 "ack_test_fail_rename"):
        getattr(lib, name).argtypes = [ctypes.c_int]
    lib.ack_test_fsync_calls.restype = ctypes.c_int
    lib.ack_test_rename_calls.restype = ctypes.c_int
    return lib


@pytest.fixture
def card(tmp_path, monkeypatch, native_ack):
    monkeypatch.chdir(tmp_path)
    native_ack.ack_test_reset_faults()
    (tmp_path / "acks").mkdir()
    (tmp_path / "processed-state").mkdir()
    # Real synthetic WAV bytes must never be modified by ACK ingestion.
    (tmp_path / "raw.wav").write_bytes(b"RIFF" + b"\x35" * 128)
    sha = "a" * 64
    entry = {
        "recordingId": ACK_ID,
        "filename": "recordings/2026-10-11/120000_" + ACK_ID + ".wav",
        "startedAt": "2026-10-11T12:00:00+09:00",
        "durationMs": 1000, "sizeBytes": 32044, "sha256": sha,
        "sampleRate": 16000, "bitDepth": 16, "channels": 1,
        "state": "finalized", "firmwareVersion": "0.1.0",
    }
    manifest = {
        "schemaVersion": 1, "deviceId": DEVICE_ID,
        "updatedAt": "2026-10-11T13:00:00+09:00",
        "recordings": [entry],
    }
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return tmp_path


def ack_json(ids=None, device=DEVICE_ID, version=1):
    return json.dumps({
        "schemaVersion": version, "deviceId": device,
        "processedAt": "2026-10-12T12:00:00+09:00",
        "recordingIds": ids if ids is not None else [ACK_ID],
    })


def invoke(lib, card):
    before = (card / "raw.wav").read_bytes()
    stats = Stats()
    ok = bool(lib.processed_ack_apply_manual_wake(
        DEVICE_ID.encode("ascii"), ctypes.byref(stats)
    ))
    assert (card / "raw.wav").read_bytes() == before
    assert stats.deleted_raw == 0
    return ok, stats


def marker(card):
    return card / "processed-state" / (ACK_ID + ".applied")


def test_valid_ack_duplicate_set_and_persisted_resend(native_ack, card):
    (card / "acks/processed.json").write_text(
        ack_json([ACK_ID, ACK_ID, UNKNOWN_ID]), encoding="utf-8"
    )
    ok, stats = invoke(native_ack, card)
    assert ok and (stats.applied, stats.already_applied,
                   stats.unknown_ids) == (1, 1, 1)
    assert marker(card).read_bytes() == ACK_ID.encode() + b"\n"
    ok, stats = invoke(native_ack, card)  # simulate a fresh process/boot
    assert ok and stats.applied == 0 and stats.already_applied == 2
    assert stats.unknown_ids == 1
    # A lost/rewritten host transfer file may not erase durable applied state.
    (card / "acks/processed.json").unlink()
    ok, stats = invoke(native_ack, card)
    assert ok and stats.applied == 0 and marker(card).exists()


@pytest.mark.parametrize("payload", [
    "not json",
    '{"schemaVersion":2,"deviceId":"' + DEVICE_ID +
        '","processedAt":"2026-10-12T12:00:00Z","recordingIds":["' + ACK_ID + '"]}',
    ack_json(device=OTHER_DEVICE),
    ack_json(ids=["not-a-uuid"]),
    '{"schemaVersion":1,"schemaVersion":1,"deviceId":"' + DEVICE_ID +
        '","processedAt":"2026-10-12T12:00:00Z","recordingIds":["' + ACK_ID + '"]}',
    '{"schemaVersion":1,"deviceId":"' + DEVICE_ID +
        '","processedAt":"invalid","recordingIds":["' + ACK_ID + '"]}',
    ack_json() + " trailing",
])
def test_invalid_payload_applies_no_id(native_ack, card, payload):
    (card / "acks/processed.json").write_text(payload, encoding="utf-8")
    ok, stats = invoke(native_ack, card)
    assert ok and stats.applied == 0 and stats.invalid_payloads == 1
    assert not marker(card).exists()


def test_embedded_nul_and_nonfinal_tmp_are_ignored(native_ack, card):
    (card / "acks/processed.json.tmp").write_text(ack_json(), encoding="utf-8")
    ok, stats = invoke(native_ack, card)
    assert ok and stats.applied == 0 and not marker(card).exists()
    (card / "acks/processed.json").write_bytes(
        ack_json().encode() + b"\x00" + ack_json().encode()
    )
    ok, stats = invoke(native_ack, card)
    assert ok and stats.invalid_payloads == 1 and not marker(card).exists()


def test_unknown_id_and_empty_ack_never_create_marker(native_ack, card):
    (card / "acks/processed.json").write_text(
        ack_json([UNKNOWN_ID]), encoding="utf-8"
    )
    ok, stats = invoke(native_ack, card)
    assert ok and stats.unknown_ids == 1 and stats.applied == 0
    assert list((card / "processed-state").iterdir()) == []
    (card / "acks/processed.json").write_text(ack_json([]), encoding="utf-8")
    ok, stats = invoke(native_ack, card)
    assert ok and stats.unknown_ids == 0 and stats.applied == 0


@pytest.mark.parametrize("fail", ["fsync", "rename"])
def test_marker_commit_failure_then_retry(native_ack, card, fail):
    (card / "acks/processed.json").write_text(ack_json(), encoding="utf-8")
    getattr(native_ack, "ack_test_fail_" + fail)(1)
    ok, stats = invoke(native_ack, card)
    assert not ok and stats.applied == 0 and not marker(card).exists()
    assert getattr(native_ack, "ack_test_" + fail + "_calls")() >= 1
    native_ack.ack_test_reset_faults()
    ok, stats = invoke(native_ack, card)
    assert ok and stats.applied == 1 and marker(card).exists()


def test_corrupt_marker_fails_closed_without_replacement(native_ack, card):
    (card / "acks/processed.json").write_text(ack_json(), encoding="utf-8")
    marker(card).write_bytes(b"corrupt")
    ok, stats = invoke(native_ack, card)
    assert not ok and stats.applied == 0
    assert marker(card).read_bytes() == b"corrupt"


def test_unmounted_device_refuses_access(native_ack, card):
    (card / "acks/processed.json").write_text(ack_json(), encoding="utf-8")
    native_ack.ack_test_set_mounted(0)
    ok, stats = invoke(native_ack, card)
    assert not ok and stats.applied == 0
    assert not marker(card).exists()


def test_invalid_manifest_fails_closed_before_marker(native_ack, card):
    (card / "acks/processed.json").write_text(ack_json(), encoding="utf-8")
    path = card / "manifest.json"
    contents = json.loads(path.read_text(encoding="utf-8"))
    contents["recordings"][0]["sha256"] = "wrong"
    path.write_text(json.dumps(contents), encoding="utf-8")
    ok, stats = invoke(native_ack, card)
    assert not ok and stats.applied == 0 and not marker(card).exists()
