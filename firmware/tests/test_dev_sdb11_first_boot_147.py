"""#147 SDB-1.1: compile the actual DEV first-boot NVS transaction.

The guarded production primitive is intentionally NOT wired to live startup,
SD reinitialization or release. This is source-only fault evidence; the
separate physical/host/RTC gates remain required and unqualified.
"""
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

COMP = Path(__file__).resolve().parents[1] / "components" / "recorder"
GEN = "11111111-1111-4111-8111-111111111111"
DEVICE = "22222222-2222-4222-8222-222222222222"
SHA = "a" * 64

HARNESS = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "dev_sdb11_first_boot.h"
#include "device_identity.h"
#include "nvs.h"
#include "recorder_nvs.h"

#define GEN "11111111-1111-4111-8111-111111111111"
#define DEVICE "22222222-2222-4222-8222-222222222222"
#define SHA "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

static const char *mode;
static uint8_t state = 0xa2u, fence = 0x51u, lifecycle = 0u;
static uint64_t nonce;
static bool nonce_exists;
static int locks, sets, commits, read_state_count, read_fence_count;
static bool is(const char *s) { return strcmp(mode, s) == 0; }

bool device_identity_is_valid_uuid(const char *id) {
    return id && strlen(id) == 36 && id[8] == '-' && id[13] == '-' &&
           id[18] == '-' && id[23] == '-';
}
esp_err_t recorder_nvs_lock(void) { assert(locks == 0); locks++; return ESP_OK; }
void recorder_nvs_unlock(void) { assert(locks == 1); locks--; }
uint32_t esp_random(void) { return 0x654321abU; }
esp_err_t nvs_open(const char *ns, int mode_flags, nvs_handle_t *h) {
    assert(locks == 1 && strcmp(ns, "m5daylog") == 0);
    assert(mode_flags == NVS_READWRITE);
    if (is("open_failure")) return ESP_FAIL;
    *h = 1; return ESP_OK;
}
void nvs_close(nvs_handle_t h) { assert(h == 1); }
esp_err_t nvs_get_str(nvs_handle_t h, const char *key, char *out, size_t *len) {
    const char *src = NULL;
    assert(h == 1);
    if (strcmp(key, "dev_gen") == 0)
        src = is("bad_gen") ? DEVICE : GEN;
    else if (strcmp(key, "device_id") == 0)
        src = is("bad_id") ? GEN : DEVICE;
    else if (strcmp(key, "dev_bind") == 0)
        src = is("bad_binding") ? "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" : SHA;
    else assert(false);
    size_t needed = strlen(src) + 1u;
    assert(*len >= needed);
    memcpy(out, src, needed);
    *len = needed;
    return ESP_OK;
}
esp_err_t nvs_get_u8(nvs_handle_t h, const char *key, uint8_t *out) {
    assert(h == 1);
    if (strcmp(key, "dev_state") == 0) {
        read_state_count++;
        if (is("readback_stage_error") && commits == 1) return ESP_FAIL;
        if (is("readback_used_error") && commits == 2) return ESP_FAIL;
        if (is("readback_used_mismatch") && commits == 2) {
            *out = 0xa3u; return ESP_OK;
        }
        *out = state; return ESP_OK;
    }
    if (strcmp(key, "release_boot") == 0) {
        read_fence_count++;
        *out = fence; return ESP_OK;
    }
    assert(strcmp(key, "shutdown_armed") == 0);
    *out = lifecycle; return ESP_OK;
}
esp_err_t nvs_get_u64(nvs_handle_t h, const char *key, uint64_t *out) {
    assert(h == 1 && strcmp(key, "dev_nonce") == 0);
    if (!nonce_exists) return ESP_ERR_NVS_NOT_FOUND;
    *out = nonce; return ESP_OK;
}
esp_err_t nvs_set_u8(nvs_handle_t h, const char *key, uint8_t value) {
    assert(h == 1);
    sets++;
    // Eagerly persisted key writes model partially surviving NVS commits.
    if (strcmp(key, "dev_state") == 0) {
        state = value;
        if ((is("stage_set_failure") && value == 0xa3u) ||
            (is("used_set_failure") && value == 0xa4u)) return ESP_FAIL;
    } else if (strcmp(key, "release_boot") == 0) {
        assert(value == 0x52u);
        fence = value;
        if (is("fence_set_failure")) return ESP_FAIL;
    } else assert(false);
    return ESP_OK;
}
esp_err_t nvs_set_u64(nvs_handle_t h, const char *key, uint64_t value) {
    assert(h == 1 && strcmp(key, "dev_nonce") == 0);
    sets++; nonce_exists = true; nonce = value;
    if (is("nonce_set_failure")) return ESP_FAIL;
    return ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t h) {
    assert(h == 1);
    commits++;
    if ((commits == 1 && is("stage_commit_ambiguous")) ||
        (commits == 2 && is("used_commit_ambiguous"))) return ESP_FAIL;
    return ESP_OK;
}

int main(int argc, char **argv) {
    assert(argc == 2); mode = argv[1];
    if (is("no_ready")) state = 0xa1u;
    if (is("used")) state = 0xa4u;
    if (is("staged")) state = 0xa3u;
    if (is("stale_fence")) fence = 0x52u;
    if (is("host_unresolved")) lifecycle = 2u;
    if (is("nonce_present")) { nonce_exists = true; nonce = 99u; }
    esp_err_t err = dev_sdb11_consume_boot_intent(
        GEN, DEVICE, SHA, !is("short_hold"));
    bool success = is("success");
    assert((err == ESP_OK) == success);
    assert(dev_sdb11_boot_intent_consumed_this_boot() == success);
    assert(locks == 0);
    if (success) {
        assert(state == 0xa4u && fence == 0x52u && nonce_exists);
        assert(commits == 2 && sets == 4);
    }
    if (is("short_hold") || is("no_ready") || is("used") ||
        is("staged") || is("stale_fence") || is("host_unresolved") ||
        is("nonce_present") || is("bad_gen") || is("bad_id") ||
        is("bad_binding") || is("open_failure")) {
        assert(sets == 0 && commits == 0);
    }
    int commits_before = commits, sets_before = sets;
    // This process must NEVER repair/retry, including after ambiguous writes.
    assert(dev_sdb11_consume_boot_intent(GEN, DEVICE, SHA, true) != ESP_OK);
    assert(commits == commits_before && sets == sets_before);
    puts("SDB11_NVS_INTENT=PASS");
    return 0;
}
"""

@pytest.fixture(scope="module")
def production_binary(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        pytest.skip("native C compiler not available")
    d = tmp_path_factory.mktemp("sdb11_nvs")
    inc = d / "include"
    inc.mkdir()
    (inc / "esp_err.h").write_text(textwrap.dedent("""\
        #pragma once
        typedef int esp_err_t;
        #define ESP_OK 0
        #define ESP_FAIL 1
        #define ESP_ERR_INVALID_STATE 3
        #define ESP_ERR_NOT_SUPPORTED 5
        #define ESP_ERR_NVS_NOT_FOUND 10
    """))
    (inc / "esp_system.h").write_text("#pragma once\n#include <stdint.h>\nuint32_t esp_random(void);\n")
    (inc / "nvs.h").write_text(textwrap.dedent("""\
        #pragma once
        #include <stddef.h>
        #include <stdint.h>
        #include "esp_err.h"
        typedef int nvs_handle_t;
        #define NVS_READWRITE 1
        esp_err_t nvs_open(const char *, int, nvs_handle_t *);
        void nvs_close(nvs_handle_t);
        esp_err_t nvs_get_str(nvs_handle_t, const char *, char *, size_t *);
        esp_err_t nvs_get_u8(nvs_handle_t, const char *, uint8_t *);
        esp_err_t nvs_get_u64(nvs_handle_t, const char *, uint64_t *);
        esp_err_t nvs_set_u8(nvs_handle_t, const char *, uint8_t);
        esp_err_t nvs_set_u64(nvs_handle_t, const char *, uint64_t);
        esp_err_t nvs_commit(nvs_handle_t);
    """))
    src = d / "harness.c"
    src.write_text(HARNESS)
    exe = d / "intent"
    proc = subprocess.run([
        cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
        "-DESP_PLATFORM", "-DCONFIG_M5DAYLOG_SDB11_DEV_INSTALL=1",
        "-I", str(inc), "-I", str(COMP / "include"),
        str(COMP / "dev_sdb11_first_boot.c"), str(src),
        "-o", str(exe)
    ], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return exe

@pytest.mark.parametrize("mode", [
    "success", "short_hold", "no_ready", "used", "staged",
    "stale_fence", "host_unresolved", "nonce_present", "bad_gen",
    "bad_id", "bad_binding", "open_failure", "stage_set_failure",
    "stage_commit_ambiguous", "readback_stage_error", "nonce_set_failure",
    "used_set_failure", "fence_set_failure", "used_commit_ambiguous",
    "readback_used_error", "readback_used_mismatch",
])
def test_sdb11_actual_production_nvs_transaction(production_binary, mode):
    proc = subprocess.run([str(production_binary), mode],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "SDB11_NVS_INTENT=PASS" in proc.stdout

def test_default_build_excludes_writes_and_no_startup_wiring():
    src = (COMP / "dev_sdb11_first_boot.c").read_text()
    assert "CONFIG_M5DAYLOG_SDB11_DEV_INSTALL" in src
    for path in [
        Path(__file__).resolve().parents[1] / "main" / "task49_runtime.c",
        Path(__file__).resolve().parents[1] / "main" / "main.c",
    ]:
        assert "dev_sdb11_consume_boot_intent(" not in path.read_text()
