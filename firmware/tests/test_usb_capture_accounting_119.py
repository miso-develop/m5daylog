"""#119: exercise the real portable production-C USB STOP accounting event."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REC = ROOT / "firmware/components/recorder"
MAIN = ROOT / "firmware/main/main.c"

C_MAIN = r"""
#include "recorder_state.h"
int main(int argc, char **argv) {
    const char *id = "01234567-89ab-4cde-8f01-23456789abcd";
    if (argc != 2) return 10;
    if (!recorder_state_append_usb_capture_accounting(argv[1], id, 180000u, 20480u)) return 11;
    if (!recorder_state_append_usb_capture_accounting(argv[1], id, 640u, 20480u)) return 12;
    if (recorder_state_append_usb_capture_accounting(argv[1], "bad\"id", 1u, 1u)) return 13;
    if (recorder_state_append_usb_capture_accounting(0, id, 1u, 1u)) return 14;
    return 0;
}
"""


def test_usb_accounting_jsonl_from_production_c(tmp_path):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        pytest.skip("host C compiler unavailable")
    harness = tmp_path / "accounting.c"
    target = tmp_path / "accounting"
    events = tmp_path / "events.jsonl"
    harness.write_text(C_MAIN, encoding="utf-8")
    subprocess.run([cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-I", str(REC / "include"),
                    str(REC / "recorder_state.c"), str(harness),
                    "-o", str(target)], capture_output=True,
                   text=True, check=True)
    subprocess.run([str(target), str(events)], capture_output=True,
                   text=True, check=True)
    records = [json.loads(line) for line in
               events.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert [x["segmentOpenElapsedMs"] for x in records] == [180000, 640]
    assert [x["pcmBytes"] for x in records] == [20480, 20480]
    assert all(x["event"] == "usb_capture_accounting" for x in records)
    assert all(x["recordingId"] ==
               "01234567-89ab-4cde-8f01-23456789abcd" for x in records)


def test_usb_accounting_only_after_manifest_finalize():
    src = MAIN.read_text(encoding="utf-8")
    terminal = src[src.index("    // The producer must complete I2S teardown"):]
    assert terminal.index("elapsed_ms =") < terminal.index(
        "if (!wav_rotation_finalize_once(") < terminal.index(
        "if (!recorder_manifest_record_wav(") < terminal.index(
        "recorder_state_append_usb_capture_accounting(")
    assert "recorder_current_state() == RECORDER_STATE_USB_PREPARE" in terminal
    assert "segment_open_elapsed_ms, seg_bytes" in terminal
