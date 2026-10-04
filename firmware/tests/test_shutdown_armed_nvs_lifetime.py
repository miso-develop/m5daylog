"""Task #87 regression: early shutdown boot-gate NVS must not stay resident."""

from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[2]
RECORDER = REPO / "firmware/components/recorder"
INCLUDE = RECORDER / "include"

STUB_HEADERS = {
    "esp_err.h": r"""
        #pragma once
        typedef int esp_err_t;
        #define ESP_OK 0
        #define ESP_FAIL 1
        #define ESP_ERR_INVALID_ARG 2
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

    #include "esp_err.h"
    #include "nvs.h"
    #include "shutdown_armed.h"

    #define CHECK(expr) do { \
        if (!(expr)) { \
            fprintf(stderr, "CHECK failed %s:%d: %s\n", __FILE__, __LINE__, #expr); \
            exit(2); \
        } \
    } while (0)

    static bool g_initialized;
    static bool g_has_value;
    static uint8_t g_value;
    static uint8_t g_pending_value;
    static int g_init_calls;
    static int g_deinit_calls;

    esp_err_t nvs_flash_init(void) {
        g_initialized = true;
        g_init_calls++;
        return ESP_OK;
    }

    esp_err_t nvs_flash_deinit(void) {
        CHECK(g_initialized);
        g_initialized = false;
        g_deinit_calls++;
        return ESP_OK;
    }

    esp_err_t nvs_open(const char *name, int mode, nvs_handle_t *handle) {
        CHECK(g_initialized);
        CHECK(strcmp(name, "m5daylog") == 0);
        (void)mode;
        *handle = 7;
        return ESP_OK;
    }

    void nvs_close(nvs_handle_t handle) { CHECK(handle == 7); }

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
        g_pending_value = value;
        return ESP_OK;
    }

    esp_err_t nvs_commit(nvs_handle_t handle) {
        CHECK(handle == 7);
        g_has_value = true;
        g_value = g_pending_value;
        return ESP_OK;
    }

    int main(void) {
        shutdown_armed_boot_action_t action = SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN;

        /* A normal boot probes durable state before recorder tasks are created. */
        CHECK(shutdown_armed_boot_action(false, &action) == ESP_OK);
        CHECK(action == SHUTDOWN_ARMED_BOOT_NORMAL);
        CHECK(!g_initialized);
        CHECK(g_init_calls == 1);
        CHECK(g_deinit_calls == 1);

        /* Writes must also release NVS after their durable commit completes. */
        CHECK(shutdown_armed_commit() == ESP_OK);
        CHECK(g_has_value && g_value == 1);
        CHECK(!g_initialized);
        CHECK(g_init_calls == 2);
        CHECK(g_deinit_calls == 2);

        return 0;
    }
"""


def test_shutdown_gate_releases_nvs_between_lifecycle_operations(tmp_path: Path) -> None:
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("host C compiler is unavailable")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for rel, content in STUB_HEADERS.items():
        path = stubs / rel
        path.write_text(textwrap.dedent(content), encoding="utf-8")

    harness = tmp_path / "harness.c"
    harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
    binary = tmp_path / "shutdown_armed_nvs_lifetime"
    subprocess.run(
        [
            cc,
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-DESP_PLATFORM",
            "-I",
            str(stubs),
            "-I",
            str(INCLUDE),
            str(RECORDER / "shutdown_armed.c"),
            str(harness),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)
