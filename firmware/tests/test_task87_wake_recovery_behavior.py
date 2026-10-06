"""Task #87 behavioral tests for manual-WAKE recovery and fresh-session gating.

The production WAKE recovery seam is compiled against deterministic host stubs.
These tests execute recovery ordering and failure branches instead of relying on
source-text ordering assertions.
"""

from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[2]
MAIN = REPO / "firmware/main"
RECORDER_INCLUDE = REPO / "firmware/components/recorder/include"

STUB_HEADERS = {
    "esp_err.h": r"""
        #pragma once
        typedef int esp_err_t;
        #define ESP_OK 0
        #define ESP_FAIL 1
        #define ESP_ERR_INVALID_ARG 2
        #define ESP_ERR_INVALID_STATE 3
    """,
    "shutdown_armed.h": r"""
        #pragma once
        #include "esp_err.h"
        esp_err_t shutdown_armed_mark_wake_recovery_pending(void);
        esp_err_t shutdown_armed_complete_wake_recovery(void);
    """,
}

HARNESS = r"""
    #include <stdbool.h>
    #include <stdio.h>
    #include <stdlib.h>
    #include <string.h>

    #include "esp_err.h"
    #include "task87_wake_recovery.h"

    #define CHECK(expr) do { \
        if (!(expr)) { \
            fprintf(stderr, "CHECK failed %s:%d: %s\n", __FILE__, __LINE__, #expr); \
            exit(2); \
        } \
    } while (0)

    static char g_trace[16];
    static size_t g_trace_len;
    static esp_err_t g_rtc_result = ESP_OK;
    static esp_err_t g_pending_result = ESP_OK;
    static esp_err_t g_complete_result = ESP_OK;

    static void trace(char value) {
        CHECK(g_trace_len + 1 < sizeof(g_trace));
        g_trace[g_trace_len++] = value;
        g_trace[g_trace_len] = '\0';
    }

    esp_err_t rtc_correction_flush_pending_event(const char *events_path) {
        CHECK(events_path != NULL);
        CHECK(strcmp(events_path, "/sdcard/M5DAYLOG/events.jsonl") == 0);
        trace('R');
        return g_rtc_result;
    }

    esp_err_t shutdown_armed_mark_wake_recovery_pending(void) {
        trace('P');
        return g_pending_result;
    }

    esp_err_t shutdown_armed_complete_wake_recovery(void) {
        trace('C');
        return g_complete_result;
    }

    static void reset_stubs(void) {
        memset(g_trace, 0, sizeof(g_trace));
        g_trace_len = 0;
        g_rtc_result = ESP_OK;
        g_pending_result = ESP_OK;
        g_complete_result = ESP_OK;
    }

    static void run_success(void) {
        task87_wake_recovery_t recovery;
        reset_stubs();
        task87_wake_recovery_init(&recovery, true);

        CHECK(task87_wake_recovery_pending(&recovery));
        CHECK(task87_wake_recovery_requires_shutdown(&recovery));
        CHECK(!task87_wake_recovery_usb_rearm_allowed(&recovery));

        CHECK(task87_wake_recovery_complete_device_recovery(
                  &recovery, "/sdcard/M5DAYLOG/events.jsonl") == ESP_OK);
        CHECK(strcmp(g_trace, "RP") == 0);

        /* Recovery replaces SHUTDOWN_ARMED with a durable fail-closed pending
         * marker; NORMAL is not committed until fresh recording proof. */
        CHECK(task87_wake_recovery_pending(&recovery));
        CHECK(task87_wake_recovery_requires_shutdown(&recovery));
        CHECK(!task87_wake_recovery_usb_rearm_allowed(&recovery));
        CHECK(task87_wake_recovery_note_recording_started(&recovery, true) == ESP_OK);
        CHECK(strcmp(g_trace, "RPC") == 0);
        CHECK(!task87_wake_recovery_pending(&recovery));
        CHECK(!task87_wake_recovery_requires_shutdown(&recovery));
        CHECK(task87_wake_recovery_usb_rearm_allowed(&recovery));
    }

    static void run_rtc_failure(void) {
        task87_wake_recovery_t recovery;
        reset_stubs();
        g_rtc_result = ESP_FAIL;
        task87_wake_recovery_init(&recovery, true);

        CHECK(task87_wake_recovery_complete_device_recovery(
                  &recovery, "/sdcard/M5DAYLOG/events.jsonl") == ESP_FAIL);
        CHECK(strcmp(g_trace, "R") == 0);
        CHECK(task87_wake_recovery_pending(&recovery));
        CHECK(task87_wake_recovery_requires_shutdown(&recovery));
        CHECK(!task87_wake_recovery_usb_rearm_allowed(&recovery));
        CHECK(task87_wake_recovery_note_recording_started(&recovery, true) ==
              ESP_ERR_INVALID_STATE);
    }

    static void run_pending_transition_failure(void) {
        task87_wake_recovery_t recovery;
        reset_stubs();
        g_pending_result = ESP_FAIL;
        task87_wake_recovery_init(&recovery, true);

        CHECK(task87_wake_recovery_complete_device_recovery(
                  &recovery, "/sdcard/M5DAYLOG/events.jsonl") == ESP_FAIL);
        CHECK(strcmp(g_trace, "RP") == 0);
        CHECK(task87_wake_recovery_pending(&recovery));
        CHECK(task87_wake_recovery_requires_shutdown(&recovery));
        CHECK(!task87_wake_recovery_usb_rearm_allowed(&recovery));
        CHECK(task87_wake_recovery_note_recording_started(&recovery, true) ==
              ESP_ERR_INVALID_STATE);
    }

    static void run_missing_fresh_id(void) {
        task87_wake_recovery_t recovery;
        reset_stubs();
        task87_wake_recovery_init(&recovery, true);
        CHECK(task87_wake_recovery_complete_device_recovery(
                  &recovery, "/sdcard/M5DAYLOG/events.jsonl") == ESP_OK);

        CHECK(task87_wake_recovery_note_recording_started(&recovery, false) ==
              ESP_ERR_INVALID_STATE);
        CHECK(task87_wake_recovery_pending(&recovery));
        CHECK(task87_wake_recovery_requires_shutdown(&recovery));
        CHECK(!task87_wake_recovery_usb_rearm_allowed(&recovery));
    }

    static void run_recording_proof_failure(void) {
        task87_wake_recovery_t recovery;
        reset_stubs();
        task87_wake_recovery_init(&recovery, true);
        CHECK(task87_wake_recovery_complete_device_recovery(
                  &recovery, "/sdcard/M5DAYLOG/events.jsonl") == ESP_OK);
        CHECK(strcmp(g_trace, "RP") == 0);

        g_complete_result = ESP_FAIL;
        CHECK(task87_wake_recovery_note_recording_started(&recovery, true) ==
              ESP_FAIL);
        CHECK(strcmp(g_trace, "RPC") == 0);
        CHECK(task87_wake_recovery_pending(&recovery));
        CHECK(task87_wake_recovery_requires_shutdown(&recovery));
        CHECK(!task87_wake_recovery_usb_rearm_allowed(&recovery));
    }

    int main(int argc, char **argv) {
        CHECK(argc == 2);
        if (strcmp(argv[1], "success") == 0) run_success();
        else if (strcmp(argv[1], "rtc-failure") == 0) run_rtc_failure();
        else if (strcmp(argv[1], "pending-transition-failure") == 0) run_pending_transition_failure();
        else if (strcmp(argv[1], "missing-fresh-id") == 0) run_missing_fresh_id();
        else if (strcmp(argv[1], "recording-proof-failure") == 0) run_recording_proof_failure();
        else CHECK(false);
        return 0;
    }
"""


def _write_headers(root: Path) -> None:
    for rel, content in STUB_HEADERS.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")


def _build(tmp_path: Path) -> Path:
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("host C compiler is unavailable")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _write_headers(stubs)
    harness = tmp_path / "harness.c"
    harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
    binary = tmp_path / "task87_wake_recovery"
    subprocess.run(
        [
            cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-DESP_PLATFORM",
            "-I", str(stubs), "-I", str(MAIN), "-I", str(RECORDER_INCLUDE),
            str(MAIN / "task87_wake_recovery.c"), str(harness),
            "-o", str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "rtc-failure",
        "pending-transition-failure",
        "missing-fresh-id",
        "recording-proof-failure",
    ],
)
def test_manual_wake_recovery_behavior(tmp_path: Path, scenario: str) -> None:
    binary = _build(tmp_path)
    subprocess.run([str(binary), scenario], check=True, capture_output=True, text=True)
