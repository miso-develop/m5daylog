"""Task #48 host contract tests: explicit lifecycle, fail-loud, low battery.

Stdlib only: no ESP-IDF, device, network, or external packages. These tests
lock the portable lifecycle model plus the source-level wiring needed to keep
SD/mic/DMA failures from being mistaken for RECORDING. Physical LED/battery
behavior remains in the consolidated Device milestone gate after #50.
"""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
MAIN = REPO / "firmware/main/main.c"
CFG = COMP / "include/recorder_config.h"
STATE_H = COMP / "include/recorder_state.h"
STATE_C = COMP / "recorder_state.c"
POWER_C = COMP / "recorder_power.c"
LED_C = COMP / "recorder_status_led.c"
CMAKE = COMP / "CMakeLists.txt"

STATES = (
    "BOOT",
    "RECOVER",
    "RECORDING",
    "USB_PREPARE",
    "USB_SYNC",
    "REMOUNT",
    "LOW_BATTERY_STOP",
    "ERROR",
)


def mirror_transition(state, next_state):
    if state == next_state:
        return next_state
    if next_state == "ERROR" and state != "ERROR":
        return "ERROR"
    if state in ("LOW_BATTERY_STOP", "ERROR"):
        return None
    allowed = {
        "BOOT": {"RECOVER"},
        "RECOVER": {"RECORDING"},
        "RECORDING": {"USB_PREPARE", "LOW_BATTERY_STOP"},
        "USB_PREPARE": {"USB_SYNC"},
        "USB_SYNC": {"REMOUNT"},
        "REMOUNT": {"RECOVER"},
    }
    return next_state if next_state in allowed.get(state, set()) else None


def test_state_set_matches_spec_36():
    hdr = STATE_H.read_text(encoding="utf-8")
    for state in STATES:
        assert f"RECORDER_STATE_{state}" in hdr
        assert f'"{state}"' in STATE_C.read_text(encoding="utf-8")


def test_state_transition_contract():
    assert mirror_transition("BOOT", "RECOVER") == "RECOVER"
    assert mirror_transition("RECOVER", "RECORDING") == "RECORDING"
    assert mirror_transition("RECORDING", "LOW_BATTERY_STOP") == "LOW_BATTERY_STOP"
    assert mirror_transition("RECORDING", "USB_PREPARE") == "USB_PREPARE"
    assert mirror_transition("USB_PREPARE", "USB_SYNC") == "USB_SYNC"
    assert mirror_transition("USB_SYNC", "REMOUNT") == "REMOUNT"
    assert mirror_transition("REMOUNT", "RECOVER") == "RECOVER"
    # Fatal failure can supersede any non-ERROR state, including a failed
    # low-battery finalize after the safe-stop decision was already made.
    assert mirror_transition("LOW_BATTERY_STOP", "ERROR") == "ERROR"
    assert mirror_transition("ERROR", "RECORDING") is None
    assert mirror_transition("BOOT", "RECORDING") is None


def test_state_event_is_jsonl_and_cause_traceable():
    src = STATE_C.read_text(encoding="utf-8")
    for key in ('"event"', '"timestamp"', '"from"', '"to"', '"reason"', '"batteryMv"'):
        assert key.replace('"', '\\"') in src or key in src
    for reason in (
        "sd-mount",
        "sd-write",
        "sd-flush",
        "mic-init",
        "i2s-read",
        "dma-overrun",
        "buffer-overflow",
        "low-battery",
        "manifest",
        "finalize",
    ):
        assert f'"{reason}"' in src
    assert 'fopen(path, "ab")' in src
    assert "fflush(f)" in src
    assert '"}\\n"' in src or "}\\n" in src


def test_battery_monitor_uses_capsule_adc_and_calibrated_voltage():
    cfg = CFG.read_text(encoding="utf-8")
    src = POWER_C.read_text(encoding="utf-8")
    assert "#define RECORDER_BATTERY_ADC_PIN 6" in cfg
    assert "ADC_UNIT_1" in src
    assert "ADC_CHANNEL_5" in src
    assert "RECORDER_BATTERY_DIVIDER_NUM 2" in src
    assert "adc_cali_raw_to_voltage" in src
    assert "#define RECORDER_LOW_BATTERY_MV 3600" in cfg
    assert "RECORDER_LOW_BATTERY_CONFIRM_SAMPLES 3u" in cfg


def test_status_led_is_dark_normally_and_distinguishes_terminal_states():
    cfg = CFG.read_text(encoding="utf-8")
    src = LED_C.read_text(encoding="utf-8")
    assert "#define RECORDER_STATUS_LED_DATA_PIN 21" in cfg
    assert "#define RECORDER_STATUS_LED_POWER_PIN 38" in cfg
    assert "RECORDER_STATE_ERROR" in src
    assert "RECORDER_STATE_LOW_BATTERY_STOP" in src
    # ERROR red, low-battery amber, all normal states off.
    assert "recorder_status_led_write(32, 0, 0, true)" in src
    assert "recorder_status_led_write(24, 8, 0, true)" in src
    assert "recorder_status_led_write(0, 0, 0, false)" in src


def test_task_48_sources_are_in_component_build():
    cmake = CMAKE.read_text(encoding="utf-8")
    for source in (
        '"recorder_state.c"',
        '"recorder_power.c"',
        '"recorder_status_led.c"',
    ):
        assert source in cmake
    assert "esp_adc" in cmake


def test_main_wires_state_failures_and_low_battery_monitor():
    main = MAIN.read_text(encoding="utf-8")
    for symbol in (
        "recorder_state_machine_init",
        "recorder_transition_state",
        "recorder_enter_error",
        "recorder_battery_task",
        "recorder_power_read_battery_mv",
        "RECORDER_LOW_BATTERY_CONFIRM_SAMPLES",
        "RECORDER_STATE_RECOVER",
        "RECORDER_STATE_RECORDING",
        "RECORDER_STATE_LOW_BATTERY_STOP",
        "RECORDER_STATE_ERROR",
    ):
        assert symbol in main, symbol
    # Each loss/failure class from #48 must map to an explicit ERROR reason.
    for reason in (
        "RECORDER_REASON_SD_MOUNT",
        "RECORDER_REASON_SD_WRITE",
        "RECORDER_REASON_SD_FLUSH",
        "RECORDER_REASON_MIC_INIT",
        "RECORDER_REASON_I2S_READ",
        "RECORDER_REASON_DMA_OVERRUN",
        "RECORDER_REASON_BUFFER_OVERFLOW",
    ):
        assert reason in main, reason
    # Final status must not collapse every stop into ERROR/stopped.
    assert "LOW_BATTERY_STOP" in main
    assert 'reason: stopped' not in main


def test_low_battery_policy_is_voltage_based_not_fake_soc_percentage():
    cfg = CFG.read_text(encoding="utf-8").lower()
    main = MAIN.read_text(encoding="utf-8").lower()
    assert "recalibr" in cfg
    assert "battery_mv" in main or "battery mv" in main
    assert "battery_percent" not in main
