"""Task #87 regression for the durable gap between recovery and fresh recording.

This compiles the production shutdown lifecycle and manual-WAKE recovery modules
together. A reset after pending recovery but before fresh-recording proof must
remain fail-closed and must not authorize an automatic normal/USB-capable boot.
"""

from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[2]
MAIN = REPO / "firmware/main"
RECORDER = REPO / "firmware/components/recorder"
INCLUDE = RECORDER / "include"

STUB_HEADERS = {
    "esp_err.h": r"""
        #pragma once
        typedef int esp_err_t;
        #define ESP_OK 0
        #define ESP_FAIL 1
        #define ESP_ERR_INVALID_ARG 2
        #define ESP_ERR_INVALID_STATE 3
        #define ESP_ERR_NOT_SUPPORTED 5
        #define ESP_ERR_NVS_NOT_FOUND 10
    """,
    "nvs.h": r"""
        #pragma once
        #include <stdint.h>
        #include "esp_err.h"
        typedef int nvs_handle_t;
        #define NVS_READONLY 0
        #define NVS_READWRITE 1
        esp_err_t nvs_open(const char *name, int mode, nvs_handle_t *handle);
        void nvs_close(nvs_handle_t handle);
        esp_err_t nvs_get_u8(nvs_handle_t handle, const char *key, uint8_t *value);
        esp_err_t nvs_set_u8(nvs_handle_t handle, const char *key, uint8_t value);
        esp_err_t nvs_commit(nvs_handle_t handle);
    """,
    "nvs_flash.h": r"""
        #pragma once
        #include "esp_err.h"
        esp_err_t nvs_flash_init(void);
        esp_err_t nvs_flash_deinit(void);
    """,
}

HARNESS = r"""
    #include <stdbool.h>
    #include <stdint.h>
    #include <stdio.h>
    #include <stdlib.h>
    #include <string.h>

    #include "nvs.h"
    #include "recorder_nvs.h"
    #include "shutdown_armed.h"
    #include "task87_wake_recovery.h"

    #define CHECK(expr) do { \
        if (!(expr)) { \
            fprintf(stderr, "CHECK failed %s:%d: %s\n", __FILE__, __LINE__, #expr); \
            exit(2); \
        } \
    } while (0)

    enum { LIFECYCLE_NORMAL = 0, LIFECYCLE_ARMED = 1,
           LIFECYCLE_WAKE_RECOVERY_PENDING = 3 };

    static bool g_has_value;
    static uint8_t g_value;
    static bool g_pending;
    static uint8_t g_pending_value;

    esp_err_t nvs_flash_init(void) { return ESP_OK; }
    esp_err_t nvs_flash_deinit(void) { return ESP_OK; }
    esp_err_t recorder_nvs_init(void) { return ESP_OK; }
    esp_err_t recorder_nvs_lock(void) { return ESP_OK; }
    void recorder_nvs_unlock(void) {}
    esp_err_t nvs_open(const char *name, int mode, nvs_handle_t *handle) {
        CHECK(strcmp(name, "m5daylog") == 0);
        (void)mode;
        *handle = 7;
        return ESP_OK;
    }
    void nvs_close(nvs_handle_t handle) {
        CHECK(handle == 7);
        g_pending = false;
    }
    esp_err_t nvs_get_u8(nvs_handle_t handle, const char *key, uint8_t *value) {
        CHECK(handle == 7);
        CHECK(strcmp(key, "shutdown_armed") == 0);
        if (!g_has_value) return ESP_ERR_NVS_NOT_FOUND;
        *value = g_value;
        return ESP_OK;
    }
    esp_err_t nvs_set_u8(nvs_handle_t handle, const char *key, uint8_t value) {
        CHECK(handle == 7);
        CHECK(strcmp(key, "shutdown_armed") == 0);
        g_pending = true;
        g_pending_value = value;
        return ESP_OK;
    }
    esp_err_t nvs_commit(nvs_handle_t handle) {
        CHECK(handle == 7);
        CHECK(g_pending);
        g_has_value = true;
        g_value = g_pending_value;
        g_pending = false;
        return ESP_OK;
    }

    esp_err_t rtc_correction_flush_pending_event(const char *events_path) {
        CHECK(strcmp(events_path, "/sdcard/M5DAYLOG/events.jsonl") == 0);
        return ESP_OK;
    }

    int main(void) {
        task87_wake_recovery_t recovery;
        shutdown_armed_boot_action_t action = SHUTDOWN_ARMED_BOOT_NORMAL;

        CHECK(shutdown_armed_commit() == ESP_OK);
        CHECK(g_value == LIFECYCLE_ARMED);

        task87_wake_recovery_init(&recovery, true);
        CHECK(task87_wake_recovery_complete_device_recovery(
                  &recovery, "/sdcard/M5DAYLOG/events.jsonl") == ESP_OK);
        CHECK(g_value == LIFECYCLE_WAKE_RECOVERY_PENDING);
        CHECK(task87_wake_recovery_pending(&recovery));
        CHECK(!task87_wake_recovery_usb_rearm_allowed(&recovery));

        /* Simulate reset/power reappearance before fresh-recording proof. */
        task87_wake_recovery_init(&recovery, false);
        CHECK(shutdown_armed_boot_action(false, &action) == ESP_OK);
        CHECK(action == SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN);
        CHECK(g_value == LIFECYCLE_WAKE_RECOVERY_PENDING);

        /* A later physical WAKE may retry recovery, but still stays gated until
         * the fresh recordingId proof durably transitions to NORMAL. */
        CHECK(shutdown_armed_boot_action(true, &action) == ESP_OK);
        CHECK(action == SHUTDOWN_ARMED_BOOT_MANUAL_RESUME);
        task87_wake_recovery_init(&recovery, true);
        CHECK(task87_wake_recovery_complete_device_recovery(
                  &recovery, "/sdcard/M5DAYLOG/events.jsonl") == ESP_OK);
        CHECK(g_value == LIFECYCLE_WAKE_RECOVERY_PENDING);
        CHECK(task87_wake_recovery_note_recording_started(&recovery, false) ==
              ESP_ERR_INVALID_STATE);
        CHECK(g_value == LIFECYCLE_WAKE_RECOVERY_PENDING);
        CHECK(!task87_wake_recovery_usb_rearm_allowed(&recovery));

        CHECK(task87_wake_recovery_note_recording_started(&recovery, true) == ESP_OK);
        CHECK(g_value == LIFECYCLE_NORMAL);
        CHECK(task87_wake_recovery_usb_rearm_allowed(&recovery));
        CHECK(shutdown_armed_boot_action(false, &action) == ESP_OK);
        CHECK(action == SHUTDOWN_ARMED_BOOT_NORMAL);
        return 0;
    }
"""

def test_recovery_reset_stays_durably_fail_closed(tmp_path: Path) -> None:
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("host C compiler is unavailable")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for rel, content in STUB_HEADERS.items():
        path = stubs / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")

    harness = tmp_path / "harness.c"
    harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
    binary = tmp_path / "task87_wake_recovery_persistence"
    subprocess.run(
        [
            cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-DESP_PLATFORM",
            "-I", str(stubs), "-I", str(MAIN), "-I", str(INCLUDE),
            str(RECORDER / "shutdown_armed.c"),
            str(MAIN / "task87_wake_recovery.c"),
            str(harness),
            "-o", str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)
