"""Task #87 behavioral tests for persistent SHUTDOWN_ARMED boot gating."""

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
        #define ESP_ERR_INVALID_STATE 3
        #define ESP_ERR_NOT_SUPPORTED 5
        #define ESP_ERR_NVS_NOT_FOUND 10
        #define ESP_ERR_NVS_NO_FREE_PAGES 11
        #define ESP_ERR_NVS_NEW_VERSION_FOUND 12
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

    static bool g_has_value;
    static uint8_t g_value;
    static int g_commit_calls;
    static esp_err_t g_flash_init_result = ESP_OK;
    static esp_err_t g_set_result = ESP_OK;
    static esp_err_t g_commit_result = ESP_OK;

    esp_err_t nvs_flash_init(void) { return g_flash_init_result; }
    esp_err_t nvs_open(const char *name, int mode, nvs_handle_t *handle) {
        CHECK(strcmp(name, "m5daylog") == 0);
        (void)mode; *handle = 7; return ESP_OK;
    }
    void nvs_close(nvs_handle_t handle) { CHECK(handle == 7); }
    esp_err_t nvs_get_u8(nvs_handle_t handle, const char *key, uint8_t *value) {
        CHECK(handle == 7);
        CHECK(strcmp(key, "shutdown_armed") == 0);
        if (!g_has_value) return ESP_ERR_NVS_NOT_FOUND;
        *value = g_value; return ESP_OK;
    }
    esp_err_t nvs_set_u8(nvs_handle_t handle, const char *key, uint8_t value) {
        CHECK(handle == 7);
        CHECK(strcmp(key, "shutdown_armed") == 0);
        if (g_set_result != ESP_OK) return g_set_result;
        g_has_value = true; g_value = value; return ESP_OK;
    }
    esp_err_t nvs_commit(nvs_handle_t handle) {
        CHECK(handle == 7);
        g_commit_calls++;
        return g_commit_result;
    }

    static void run_persistence(void) {
        bool armed = true;
        CHECK(shutdown_armed_read(&armed) == ESP_OK);
        CHECK(!armed);
        CHECK(shutdown_armed_commit() == ESP_OK);
        CHECK(g_has_value && g_value == 1);
        CHECK(g_commit_calls == 1);
        armed = false;
        CHECK(shutdown_armed_read(&armed) == ESP_OK);
        CHECK(armed);
        CHECK(shutdown_armed_clear() == ESP_OK);
        CHECK(g_value == 0);
        CHECK(g_commit_calls == 2);
        armed = true;
        CHECK(shutdown_armed_read(&armed) == ESP_OK);
        CHECK(!armed);
    }

    static void run_boot_gate(void) {
        shutdown_armed_boot_action_t action = SHUTDOWN_ARMED_BOOT_NORMAL;
        CHECK(shutdown_armed_commit() == ESP_OK);

        /* Simulated reset: durable NVS remains, no in-RAM state is reused. */
        CHECK(shutdown_armed_boot_action(false, &action) == ESP_OK);
        CHECK(action == SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN);
        CHECK(g_value == 1);

        CHECK(shutdown_armed_boot_action(true, &action) == ESP_OK);
        CHECK(action == SHUTDOWN_ARMED_BOOT_MANUAL_RESUME);
        CHECK(g_value == 1); /* manual recognition alone must never clear */

        CHECK(shutdown_armed_clear() == ESP_OK);
        CHECK(shutdown_armed_boot_action(false, &action) == ESP_OK);
        CHECK(action == SHUTDOWN_ARMED_BOOT_NORMAL);
    }

    static void run_corrupt_marker(void) {
        bool armed = false;
        shutdown_armed_boot_action_t action = SHUTDOWN_ARMED_BOOT_NORMAL;
        g_has_value = true;
        g_value = 2;
        CHECK(shutdown_armed_read(&armed) == ESP_FAIL);
        CHECK(!armed);
        CHECK(shutdown_armed_boot_action(false, &action) == ESP_FAIL);
    }

    static void run_fail_closed(void) {
        bool armed = false;
        shutdown_armed_boot_action_t action = SHUTDOWN_ARMED_BOOT_NORMAL;
        g_flash_init_result = ESP_ERR_NVS_NO_FREE_PAGES;
        CHECK(shutdown_armed_read(&armed) == ESP_ERR_NVS_NO_FREE_PAGES);
        CHECK(!armed);
        CHECK(shutdown_armed_boot_action(false, &action) == ESP_ERR_NVS_NO_FREE_PAGES);
        g_flash_init_result = ESP_OK;
        g_set_result = ESP_FAIL;
        CHECK(shutdown_armed_commit() == ESP_FAIL);
        CHECK(g_commit_calls == 0);
        g_set_result = ESP_OK;
        g_commit_result = ESP_FAIL;
        CHECK(shutdown_armed_commit() == ESP_FAIL);
        CHECK(g_commit_calls == 1);
    }

    int main(int argc, char **argv) {
        CHECK(argc == 2);
        if (strcmp(argv[1], "persistence") == 0) run_persistence();
        else if (strcmp(argv[1], "boot-gate") == 0) run_boot_gate();
        else if (strcmp(argv[1], "corrupt") == 0) run_corrupt_marker();
        else if (strcmp(argv[1], "fail-closed") == 0) run_fail_closed();
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
    binary = tmp_path / "shutdown_armed"
    subprocess.run(
        [
            cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-DESP_PLATFORM",
            "-I", str(stubs), "-I", str(INCLUDE),
            str(RECORDER / "shutdown_armed.c"), str(harness),
            "-o", str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


@pytest.mark.parametrize("scenario", ["persistence", "boot-gate", "corrupt", "fail-closed"])
def test_shutdown_armed_persistence_and_boot_gate(tmp_path: Path, scenario: str) -> None:
    binary = _build(tmp_path)
    subprocess.run([str(binary), scenario], check=True, capture_output=True, text=True)
