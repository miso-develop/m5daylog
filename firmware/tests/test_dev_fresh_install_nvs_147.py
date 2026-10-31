"""#147/#144: compile the actual production readback against fault-injected NVS.

Source-only test; not a physical install, RTC/SD verification, enrollment or
human authorization. No NVS writes are exported by the mock.
"""
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMP = ROOT / "firmware/components/recorder"

H = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "dev_fresh_install_nvs.h"
#include "nvs.h"
#include "recorder_nvs.h"

#define ID "11111111-1111-4111-8111-111111111111"
#define OTHER "22222222-2222-4222-8222-222222222222"
static const char *mode;
static int locks, reads;
bool device_identity_is_valid_uuid(const char *s) {
    if (!s || strlen(s) != 36) return false;
    for (int i = 0; i < 36; ++i) {
        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (s[i] != '-') return false;
        } else if (!((s[i] >= '0' && s[i] <= '9') ||
                     (s[i] >= 'a' && s[i] <= 'f'))) return false;
    }
    return true;
}
static bool is(const char *s) { return strcmp(mode, s) == 0; }
esp_err_t recorder_nvs_lock(void) { ++locks; return ESP_OK; }
void recorder_nvs_unlock(void) { --locks; }
esp_err_t nvs_open(const char *ns, int access, nvs_handle_t *h) {
    assert(access == NVS_READONLY);
    if (is("eio")) return ESP_FAIL;
    if (strcmp(ns, "m5daylog") == 0) {
        if (is("devns_missing")) return ESP_ERR_NVS_NOT_FOUND;
        *h = 1;
    } else {
        assert(strcmp(ns, "m5daylog_rtc") == 0);
        if (is("rtcabsent")) return ESP_ERR_NVS_NOT_FOUND;
        if (is("rtc_eio")) return ESP_FAIL;
        *h = 2;
    }
    return ESP_OK;
}
void nvs_close(nvs_handle_t h) { assert(h == 1 || h == 2); }
esp_err_t nvs_get_str(nvs_handle_t h, const char *key, char *s, size_t *len) {
    assert(h == 1 && strcmp(key, "device_id") == 0);
    ++reads;
    if (is("eio_read")) return ESP_FAIL;
    if (is("id_missing")) return ESP_ERR_NVS_NOT_FOUND;
    const char *id = is("mismatch") ? OTHER : ID;
    assert(*len >= strlen(id) + 1);
    memcpy(s, id, strlen(id) + 1);
    *len = strlen(id) + 1;
    return ESP_OK;
}
esp_err_t nvs_get_u8(nvs_handle_t h, const char *key, uint8_t *v) {
    assert(h == 1);
    ++reads;
    if (strcmp(key, "release_boot") == 0) {
        if (is("fence_missing")) return ESP_ERR_NVS_NOT_FOUND;
        *v = is("staged") ? 0x54 : is("active") ? 0x52 :
             is("fu_ready") ? 0x55 : 0x51;
    } else {
        assert(strcmp(key, "shutdown_armed") == 0);
        if (is("lifecycle_missing")) return ESP_ERR_NVS_NOT_FOUND;
        *v = is("armed") ? 1 : is("host") ? 2 :
             is("unknown") ? 255 : 0;
    }
    return ESP_OK;
}
esp_err_t nvs_get_blob(nvs_handle_t h, const char *key,
                       void *out, size_t *len) {
    assert(h == 2 && strcmp(key, "pending") == 0 && out == NULL);
    ++reads;
    if (is("pending")) { *len = 16; return ESP_ERR_NVS_INVALID_LENGTH; }
    return ESP_ERR_NVS_NOT_FOUND;
}
int main(int argc, char **argv) {
    assert(argc == 2);
    mode = argv[1];
    esp_err_t err = dev_fresh_install_nvs_readback_matches(
        is("badid") ? "bad" : ID);
    bool positive = is("valid") || is("rtcabsent");
    assert((err == ESP_OK) == positive);
    assert(locks == 0);
    if (is("badid")) assert(reads == 0);
    puts("NVS_READBACK_CHECK=PASS");
    return 0;
}
"""


@pytest.fixture(scope="module")
def readback_binary(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("native C compiler unavailable")
    tmp = tmp_path_factory.mktemp("dev_fresh_nvs")
    inc = tmp / "include"
    inc.mkdir()
    (inc / "esp_err.h").write_text(textwrap.dedent("""\
        #pragma once
        typedef int esp_err_t;
        #define ESP_OK 0
        #define ESP_FAIL 1
        #define ESP_ERR_INVALID_ARG 2
        #define ESP_ERR_INVALID_STATE 3
        #define ESP_ERR_NOT_SUPPORTED 5
        #define ESP_ERR_NVS_NOT_FOUND 10
        #define ESP_ERR_NVS_INVALID_LENGTH 11
    """))
    (inc / "nvs.h").write_text(textwrap.dedent("""\
        #pragma once
        #include <stddef.h>
        #include <stdint.h>
        #include "esp_err.h"
        typedef int nvs_handle_t;
        #define NVS_READONLY 0
        #define NVS_READWRITE 1
        esp_err_t nvs_open(const char *, int, nvs_handle_t *);
        void nvs_close(nvs_handle_t);
        esp_err_t nvs_get_str(nvs_handle_t, const char *, char *, size_t *);
        esp_err_t nvs_get_u8(nvs_handle_t, const char *, uint8_t *);
        esp_err_t nvs_get_blob(nvs_handle_t, const char *, void *, size_t *);
    """))
    harness = tmp / "harness.c"
    harness.write_text(H)
    exe = tmp / "readback"
    proc = subprocess.run([
        cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
        "-DESP_PLATFORM", "-I", str(inc),
        "-I", str(COMP / "include"),
        str(COMP / "dev_fresh_install_nvs.c"), str(harness), "-o", str(exe),
    ], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return exe


@pytest.mark.parametrize("mode", [
    "valid", "rtcabsent", "badid", "id_missing", "mismatch",
    "fence_missing", "lifecycle_missing", "staged", "active", "fu_ready",
    "armed", "host", "unknown", "pending", "eio", "eio_read",
    "devns_missing", "rtc_eio",
])
def test_actual_nvs_readback_fail_closed(readback_binary, mode):
    result = subprocess.run([str(readback_binary), mode],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "NVS_READBACK_CHECK=PASS" in result.stdout


def test_readback_has_no_mutation_or_release_path():
    src = (COMP / "dev_fresh_install_nvs.c").read_text()
    assert 'nvs_open("m5daylog", NVS_READONLY' in src
    assert 'nvs_open("m5daylog_rtc", NVS_READONLY' in src
    for forbidden in ("nvs_set_", "nvs_commit(", "nvs_erase",
                      "RELEASE_STORAGE", "sd_mount_", "fopen(",
                      "rename(", "remove("):
        assert forbidden not in src
