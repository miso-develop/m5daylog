"""#147 FI2-I1: production C pristine-NVS provision and fault boundaries.

This source-only test uses in-memory NVS. It neither authorizes a physical
partition write nor proves a selected target, receipt, FAT32, H0 or first release.
"""
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

COMP = Path(__file__).resolve().parents[1] / "components" / "recorder"

HARNESS = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "dev_fresh_install_provision.h"
#include "dev_fresh_install_nvs.h"
#include "nvs.h"
#include "recorder_nvs.h"

#define ID "11111111-1111-4111-8111-111111111111"
#define OTHER "22222222-2222-4222-8222-222222222222"
static const char *mode;
static bool has_device, has_rtc, staged_id, staged_fence, staged_lifecycle;
static bool persisted_fence, persisted_lifecycle;
static int locks, writes, commits, read_probe;
static char persisted_id[37];
static uint8_t fence, lifecycle;

static bool is(const char *s) { return strcmp(mode, s) == 0; }
bool device_identity_is_valid_uuid(const char *s) {
    if (!s || strlen(s) != 36) return false;
    for (int i = 0; i < 36; i++) {
        char c = s[i];
        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (c != '-') return false;
        } else if (!((c >= '0' && c <= '9') ||
                     (c >= 'a' && c <= 'f'))) return false;
    }
    return true;
}
esp_err_t recorder_nvs_lock(void) { assert(locks == 0); locks++; return ESP_OK; }
void recorder_nvs_unlock(void) { assert(locks == 1); locks--; }
esp_err_t nvs_open(const char *ns, int access, nvs_handle_t *h) {
    assert(locks == 1);
    if (strcmp(ns, "m5daylog") == 0) {
        if (access == NVS_READONLY) {
            read_probe++;
            if (is("dev_probe_eio")) return ESP_FAIL;
            if (!has_device) return ESP_ERR_NVS_NOT_FOUND;
        } else {
            assert(access == NVS_READWRITE);
            if (is("write_open_eio")) return ESP_FAIL;
        }
        *h = 1; return ESP_OK;
    }
    assert(strcmp(ns, "m5daylog_rtc") == 0);
    assert(access == NVS_READONLY);
    if (is("rtc_probe_eio")) return ESP_FAIL;
    if (!has_rtc) return ESP_ERR_NVS_NOT_FOUND;
    *h = 2; return ESP_OK;
}
void nvs_close(nvs_handle_t h) { assert(h == 1 || h == 2); }
esp_err_t nvs_set_str(nvs_handle_t h, const char *key, const char *value) {
    assert(locks == 1 && h == 1 && strcmp(key, "device_id") == 0);
    writes++;
    if (is("set_id_eio")) return ESP_FAIL;
    assert(strcmp(value, ID) == 0);
    // NVS may persist a key before nvs_commit; model that explicitly.
    if (is("partial_set_id")) {
        has_device = true;
        strcpy(persisted_id, ID);
        return ESP_FAIL;
    }
    staged_id = true; return ESP_OK;
}
esp_err_t nvs_set_u8(nvs_handle_t h, const char *key, uint8_t value) {
    assert(locks == 1 && h == 1);
    writes++;
    if (strcmp(key, "release_boot") == 0) {
        assert(value == 0x51);
        if (is("set_fence_eio")) return ESP_FAIL;
        if (is("partial_set_fence")) {
            has_device = true;
            strcpy(persisted_id, ID);
            persisted_fence = true;
            fence = 0x51;
            return ESP_FAIL;
        }
        staged_fence = true;
    } else {
        assert(strcmp(key, "shutdown_armed") == 0 && value == 0);
        if (is("set_lifecycle_eio")) return ESP_FAIL;
        if (is("partial_set_lifecycle")) {
            has_device = true;
            strcpy(persisted_id, ID);
            fence = 0x51;
            persisted_fence = true;
            lifecycle = 0;
            persisted_lifecycle = true;
            return ESP_FAIL;
        }
        staged_lifecycle = true;
    }
    return ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t h) {
    assert(locks == 1 && h == 1);
    commits++;
    assert(staged_id && staged_fence && staged_lifecycle);
    if (is("partial_commit") || is("partial_commit_ok")) {
        has_device = true;
        strcpy(persisted_id, ID);
        fence = 0x51;
        persisted_fence = true;
        // A torn write might report error, or a seemingly successful commit
        // while the follow-up key readback discovers missing lifecycle.
        return is("partial_commit") ? ESP_FAIL : ESP_OK;
    }
    if (!is("commit_eio")) {
        has_device = true;
        if (is("postcommit_pending")) has_rtc = true;
        strcpy(persisted_id, is("postcommit_mismatch") ? OTHER : ID);
        fence = 0x51;
        lifecycle = 0;
        persisted_fence = true;
        persisted_lifecycle = true;
    }
    if (is("commit_eio") || is("commit_ambiguous")) return ESP_FAIL;
    return ESP_OK;
}
esp_err_t nvs_get_str(nvs_handle_t h, const char *key,
                      char *out, size_t *length) {
    assert(h == 1 && strcmp(key, "device_id") == 0 && has_device);
    assert(*length >= sizeof(persisted_id));
    memcpy(out, persisted_id, sizeof(persisted_id));
    *length = sizeof(persisted_id);
    return ESP_OK;
}
esp_err_t nvs_get_u8(nvs_handle_t h, const char *key, uint8_t *out) {
    assert(h == 1 && has_device);
    if (strcmp(key, "release_boot") == 0) {
        if (!persisted_fence) return ESP_ERR_NVS_NOT_FOUND;
        *out = fence;
    } else {
        assert(strcmp(key, "shutdown_armed") == 0);
        if (!persisted_lifecycle) return ESP_ERR_NVS_NOT_FOUND;
        *out = lifecycle;
    }
    return ESP_OK;
}
esp_err_t nvs_get_blob(nvs_handle_t h, const char *key,
                       void *out, size_t *length) {
    assert(h == 2 && out == NULL && strcmp(key, "pending") == 0);
    if (is("postcommit_pending")) { *length = 4; return ESP_ERR_NVS_INVALID_LENGTH; }
    return ESP_ERR_NVS_NOT_FOUND;
}
int main(int argc, char **argv) {
    assert(argc == 2);
    mode = argv[1];
    if (is("preexisting") || is("preexisting_partial")) has_device = true;
    if (is("rtc_present")) has_rtc = true;
    const bool preexisting = has_device || has_rtc;
    esp_err_t result = dev_fresh_install_nvs_provision_pristine(
        is("badid") ? "not-a-uuid" : ID);
    const bool success = is("valid");
    assert((result == ESP_OK) == success);
    assert(locks == 0);
    if (preexisting || is("badid") || is("dev_probe_eio") ||
        is("rtc_probe_eio") || is("write_open_eio")) {
        assert(writes == 0 && commits == 0);
    }
    if (success) {
        assert(commits == 1 && writes == 3 && has_device);
        assert(dev_fresh_install_nvs_provision_pristine(ID) ==
               ESP_ERR_INVALID_STATE);
        assert(writes == 3 && commits == 1 && locks == 0);
    }
    if (is("commit_ambiguous") || is("partial_set_id") ||
        is("partial_set_fence") || is("partial_set_lifecycle") ||
        is("partial_commit") || is("partial_commit_ok")) {
        assert(result != ESP_OK && has_device);
        const int commits_before_retry = commits;
        const int writes_before_retry = writes;
        // After a reboot with any surviving namespace, no second install
        // attempt is allowed to re-enter the production NVS writer.
        assert(dev_fresh_install_nvs_provision_pristine(ID) ==
               ESP_ERR_INVALID_STATE);
        assert(commits == commits_before_retry &&
               writes == writes_before_retry && locks == 0);
    }
    puts("FI2_NVS_PROVISION=PASS");
    return 0;
}
"""

@pytest.fixture(scope="module")
def provision_binary(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        pytest.skip("native C compiler unavailable")
    tmp = tmp_path_factory.mktemp("fi2_nvs_provision")
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
        esp_err_t nvs_set_str(nvs_handle_t, const char *, const char *);
        esp_err_t nvs_set_u8(nvs_handle_t, const char *, uint8_t);
        esp_err_t nvs_commit(nvs_handle_t);
    """))
    harness = tmp / "harness.c"
    harness.write_text(HARNESS)
    exe = tmp / "fi2_provision"
    proc = subprocess.run([
        cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
        "-DESP_PLATFORM", "-I", str(inc), "-I", str(COMP / "include"),
        str(COMP / "dev_fresh_install_nvs.c"),
        str(COMP / "dev_fresh_install_provision.c"),
        str(harness), "-o", str(exe),
    ], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return exe

@pytest.mark.parametrize("mode", [
    "valid", "badid", "preexisting", "preexisting_partial",
    "rtc_present", "dev_probe_eio", "rtc_probe_eio", "write_open_eio",
    "set_id_eio", "set_fence_eio", "set_lifecycle_eio", "commit_eio",
    "commit_ambiguous", "postcommit_mismatch", "postcommit_pending",
    "partial_set_id", "partial_set_fence", "partial_set_lifecycle",
    "partial_commit", "partial_commit_ok",
])
def test_pristine_nvs_fail_closed(provision_binary, mode):
    run = subprocess.run([str(provision_binary), mode],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr + run.stdout
    assert "FI2_NVS_PROVISION=PASS" in run.stdout

def test_no_normal_boot_wiring_or_sd_mutation():
    src = (COMP / "dev_fresh_install_provision.c").read_text()
    for forbidden in ("nvs_flash_erase", "nvs_erase_", "fopen(", "remove(",
                      "rename(", "RELEASE_STORAGE", "sd_mount_"):
        assert forbidden not in src
    main = (Path(__file__).resolve().parents[1] / "main" / "main.c").read_text()
    assert "dev_fresh_install_nvs_provision_pristine(" not in main
