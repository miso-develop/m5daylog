"""Task #50 host contract tests: USB CDC JSON protocol and RTC correction."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
PROTO_H = COMP / "include/usb_cdc_protocol.h"
PROTO_C = COMP / "usb_cdc_protocol.c"
RTC_H = COMP / "include/rtc_correction.h"
RTC_C = COMP / "rtc_correction.c"
RUNTIME = REPO / "firmware/main/task50_runtime.c"
MAIN_CMAKE = REPO / "firmware/main/CMakeLists.txt"
COMP_CMAKE = COMP / "CMakeLists.txt"
SDKCONFIG = REPO / "firmware/sdkconfig.defaults"

MAX_LINE = 1024
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
TIME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T"
    r"(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d+)?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})$"
)


class MirrorState:
    def __init__(self):
        self.pending = None
        self.rtc = "2026-10-03T00:00:00+00:00"


def _error(request_id, code):
    return {"id": request_id, "ok": False, "error": {"code": code, "message": code}}


def _normalize_time(value):
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        raise ValueError("INVALID_ARGS")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    offset = parsed.utcoffset()
    if offset is None:
        raise ValueError("INVALID_ARGS")
    utc = parsed.astimezone(dt.timezone.utc).replace(microsecond=0)
    if not 2000 <= utc.year <= 2099:
        raise OverflowError("RANGE_ERROR")
    return utc.isoformat(timespec="seconds")


def mirror_handle(raw, state):
    if len(raw.encode("utf-8")) > MAX_LINE:
        return _error(None, "REQUEST_TOO_LARGE")
    if raw.endswith("\r"):
        raw = raw[:-1]
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        return _error(None, "INVALID_JSON")
    if not isinstance(doc, dict):
        return _error(None, "INVALID_REQUEST")

    request_id = doc.get("id")
    if not isinstance(request_id, str) or not ID_RE.fullmatch(request_id):
        return _error(None, "INVALID_REQUEST")
    cmd = doc.get("cmd")
    args = doc.get("args")
    if not isinstance(cmd, str) or not isinstance(args, dict):
        return _error(request_id, "INVALID_REQUEST")

    if cmd == "PING":
        if args:
            return _error(request_id, "INVALID_ARGS")
        return {"id": request_id, "ok": True, "result": {"pong": True}}

    if cmd == "GET_INFO":
        if args:
            return _error(request_id, "INVALID_ARGS")
        return {
            "id": request_id,
            "ok": True,
            "result": {
                "protocolMajor": 1,
                "schemaVersion": 1,
                "deviceId": "01234567-89ab-4def-8123-456789abcdef",
                "model": "M5Capsule v1.1",
                "firmwareVersion": "0.1.0",
                "audioCapabilities": {
                    "sampleRate": 16000,
                    "bitDepth": 16,
                    "channels": 1,
                    "format": "pcm",
                },
            },
        }

    if cmd == "GET_STATUS":
        if args:
            return _error(request_id, "INVALID_ARGS")
        return {
            "id": request_id,
            "ok": True,
            "result": {
                "state": "USB_SYNC",
                "reason": "usb",
                "batteryMv": 3900,
                "rtcCorrectionPending": state.pending is not None,
            },
        }

    if cmd == "SET_TIME":
        if "time" not in args:
            return _error(request_id, "INVALID_ARGS")
        if state.pending is not None:
            return _error(request_id, "BUSY")
        before = state.rtc
        try:
            normalized = _normalize_time(args["time"])
        except OverflowError:
            return _error(request_id, "RANGE_ERROR")
        except (TypeError, ValueError):
            return _error(request_id, "INVALID_ARGS")
        state.rtc = normalized
        state.pending = {"before": before, "after": normalized, "source": "pc"}
        return {
            "id": request_id,
            "ok": True,
            "result": {"time": normalized, "eventPending": True},
        }

    return _error(request_id, "UNKNOWN_COMMAND")


def test_protocol_vectors_cover_read_only_commands_and_stable_errors():
    state = MirrorState()
    assert mirror_handle('{"id":"p1","cmd":"PING","args":{}}', state) == {
        "id": "p1",
        "ok": True,
        "result": {"pong": True},
    }
    info = mirror_handle('{"id":"i1","cmd":"GET_INFO","args":{}}', state)
    assert info["result"]["protocolMajor"] == 1
    assert info["result"]["audioCapabilities"] == {
        "sampleRate": 16000,
        "bitDepth": 16,
        "channels": 1,
        "format": "pcm",
    }
    status = mirror_handle('{"id":"s1","cmd":"GET_STATUS","args":{}}', state)
    assert status["result"]["state"] == "USB_SYNC"
    assert state.pending is None
    assert state.rtc == "2026-10-03T00:00:00+00:00"
    assert mirror_handle("{", state)["error"]["code"] == "INVALID_JSON"
    assert mirror_handle("[]", state)["error"]["code"] == "INVALID_REQUEST"
    assert mirror_handle('{"id":"bad id","cmd":"PING","args":{}}', state)["id"] is None
    unknown = mirror_handle('{"id":"u1","cmd":"NOPE","args":{}}', state)
    assert unknown["error"]["code"] == "UNKNOWN_COMMAND"
    assert unknown["id"] == "u1"


def test_set_time_accepts_offset_and_z_and_rejects_invalid_without_mutation():
    state = MirrorState()
    response = mirror_handle(
        '{"id":"t1","cmd":"SET_TIME","args":{"time":"2026-10-03T10:30:00+09:00"}}',
        state,
    )
    assert response["ok"] is True
    assert response["result"] == {
        "time": "2026-10-03T01:30:00+00:00",
        "eventPending": True,
    }
    assert state.pending == {
        "before": "2026-10-03T00:00:00+00:00",
        "after": "2026-10-03T01:30:00+00:00",
        "source": "pc",
    }
    busy = mirror_handle(
        '{"id":"t2","cmd":"SET_TIME","args":{"time":"2026-10-03T02:00:00Z"}}',
        state,
    )
    assert busy["error"]["code"] == "BUSY"
    assert state.rtc == "2026-10-03T01:30:00+00:00"

    for bad, code in (
        ("2026-10-03T10:30:00", "INVALID_ARGS"),
        ("2026-02-30T10:30:00+09:00", "INVALID_ARGS"),
        ("1999-12-31T23:59:59Z", "RANGE_ERROR"),
    ):
        fresh = MirrorState()
        before = fresh.rtc
        res = mirror_handle(
            json.dumps({"id": "bad", "cmd": "SET_TIME", "args": {"time": bad}}),
            fresh,
        )
        assert res["error"]["code"] == code
        assert fresh.rtc == before
        assert fresh.pending is None


def test_framing_contract_crlf_size_and_disconnect_reset_are_encoded_in_source():
    assert PROTO_H.exists() and PROTO_C.exists()
    hdr = PROTO_H.read_text(encoding="utf-8")
    src = PROTO_C.read_text(encoding="utf-8")
    for marker in (
        "USB_CDC_PROTOCOL_MAX_LINE_BYTES",
        "1024",
        "USB_CDC_ERROR_REQUEST_TOO_LARGE",
        "CDC_EVENT_LINE_STATE_CHANGED",
        "tinyusb_cdcacm_read",
        "tinyusb_cdcacm_write_queue",
        "tinyusb_cdcacm_write_flush",
    ):
        assert marker in hdr or marker in src, marker
    callback_at = src.index("static void usb_cdc_rx_callback")
    worker_at = src.index("static void usb_cdc_worker_task")
    callback = src[callback_at:worker_at]
    assert "tinyusb_cdcacm_write_flush" not in callback
    assert "cdc_protocol_process_line" not in callback


def test_cdc_composite_and_runtime_wiring_preserve_task49_ownership():
    sdk = SDKCONFIG.read_text(encoding="utf-8")
    comp_cmake = COMP_CMAKE.read_text(encoding="utf-8")
    main_cmake = MAIN_CMAKE.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")
    assert "CONFIG_TINYUSB_MSC_ENABLED=y" in sdk
    assert "CONFIG_TINYUSB_CDC_ENABLED=y" in sdk
    assert "CONFIG_TINYUSB_CDC_COUNT=1" in sdk
    assert '"usb_cdc_protocol.c"' in comp_cmake
    assert '"rtc_correction.c"' in comp_cmake
    assert 'SRCS "task50_runtime.c"' in main_cmake
    assert "usb_msc_ownership_init" in runtime
    assert "usb_msc_ownership_start" in runtime
    assert "usb_cdc_protocol_init" in runtime
    assert "usb_cdc_protocol_start" in runtime


def test_set_time_pending_store_is_nvs_only_during_usb_sync():
    assert RTC_H.exists() and RTC_C.exists()
    src = RTC_C.read_text(encoding="utf-8")
    for marker in (
        "nvs_open",
        "nvs_set_blob",
        "nvs_commit",
        "rtc_correction_apply",
        "settimeofday",
        "BM8563",
    ):
        assert marker in src, marker
    apply_at = src.index("rtc_correction_apply")
    flush_at = src.index("rtc_correction_flush_pending_event", apply_at)
    apply_region = src[apply_at:flush_at]
    for forbidden in ("fopen(", "RECORDER_EVENTS_PATH", "/sdcard"):
        assert forbidden not in apply_region


def test_pending_event_flush_is_after_remount_before_recording_and_exactly_once_safe():
    runtime = RUNTIME.read_text(encoding="utf-8")
    rtc = RTC_C.read_text(encoding="utf-8")
    detach_at = runtime.index("static void recorder_handle_usb_detach")
    remount_at = runtime.index("sd_mount_remount_after_usb", detach_at)
    flush_at = runtime.index("rtc_correction_flush_pending_event", remount_at)
    recover_at = runtime.index("RECORDER_STATE_RECOVER", flush_at)
    restart_at = runtime.index("recorder_start_session", recover_at)
    assert remount_at < flush_at < recover_at < restart_at
    for marker in (
        "correctionId",
        "rtc_correction_event_already_recorded",
        "fsync",
        "nvs_erase_key",
    ):
        assert marker in rtc, marker
    assert rtc.index("fsync") < rtc.index("nvs_erase_key")


def test_protocol_has_no_application_logging_surface_for_command_data():
    src = PROTO_C.read_text(encoding="utf-8")
    # Protocol handling intentionally contains no application logging API;
    # therefore command bodies/metadata cannot accidentally be emitted here.
    assert "ESP_LOG" not in src


def test_oversized_line_is_recoverable_at_next_line_in_reference_framer():
    state = MirrorState()
    oversized = "x" * 1025
    assert mirror_handle(oversized, state)["error"]["code"] == "REQUEST_TOO_LARGE"
    good = mirror_handle('{"id":"after","cmd":"PING","args":{}}', state)
    assert good["ok"] is True and good["id"] == "after"
