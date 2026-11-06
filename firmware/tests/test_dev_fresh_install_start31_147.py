"""#147 FI3.1 START31: source-only regression for the actual Device prewrite seam.

The missing-NVS NORMAL behavior is intentionally a *counterexample* to an
NVS-only admission design. This neither simulates nor authorizes physical SD
exclusion, first write, selected firmware identity, or a real Human Gate.
"""

from pathlib import Path
import shutil
import subprocess
import textwrap
import pytest

FW = Path(__file__).resolve().parents[1]
REC = FW / "components" / "recorder"

HARNESS = r"""
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "nvs.h"
#include "recorder_nvs.h"
#include "shutdown_armed.h"
static const char *mode;
static int held, opened, writes;
static int is(const char *s) { return strcmp(mode, s) == 0; }
esp_err_t recorder_nvs_lock(void) { assert(!held); held = 1; return ESP_OK; }
void recorder_nvs_unlock(void) { assert(held); held = 0; }
esp_err_t nvs_open(const char *ns, int access, nvs_handle_t *h) {
    assert(held && !strcmp(ns, "m5daylog") && access == NVS_READONLY);
    opened++;
    if (is("missing_namespace")) return ESP_ERR_NVS_NOT_FOUND;
    if (is("open_error")) return ESP_FAIL;
    *h = 1; return ESP_OK;
}
esp_err_t nvs_get_u8(nvs_handle_t h, const char *key, uint8_t *v) {
    assert(h == 1 && !strcmp(key, "shutdown_armed"));
    if (is("missing_key")) return ESP_ERR_NVS_NOT_FOUND;
    if (is("read_error")) return ESP_FAIL;
    *v = is("host_unresolved") ? 2 :
         (is("armed") || is("manual_armed")) ? 1 :
         is("invalid_value") ? 255 : 0;
    return ESP_OK;
}
void nvs_close(nvs_handle_t h) { assert(h == 1); }
esp_err_t nvs_set_u8(nvs_handle_t h, const char *key, uint8_t v) {
    (void)h; (void)key; (void)v; writes++; return ESP_FAIL;
}
esp_err_t nvs_commit(nvs_handle_t h) {
    (void)h; writes++; return ESP_FAIL;
}
int main(int argc, char **argv) {
    shutdown_armed_boot_action_t action = SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN;
    assert(argc == 2); mode = argv[1];
    esp_err_t err = shutdown_armed_boot_action(is("manual_armed"), &action);
    assert(!held && opened == 1 && writes == 0);
    printf("err=%d action=%d writes=%d\n", err, (int)action, writes);
    return 0;
}
"""


@pytest.fixture(scope="module")
def classifier(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        pytest.skip("native C compiler unavailable")
    temp = tmp_path_factory.mktemp("start31_lifecycle")
    inc = temp / "include"
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
    """))
    (inc / "nvs.h").write_text(textwrap.dedent("""\
        #pragma once
        #include <stdint.h>
        #include "esp_err.h"
        typedef int nvs_handle_t;
        #define NVS_READONLY 0
        #define NVS_READWRITE 1
        esp_err_t nvs_open(const char *, int, nvs_handle_t *);
        esp_err_t nvs_get_u8(nvs_handle_t, const char *, uint8_t *);
        void nvs_close(nvs_handle_t);
        esp_err_t nvs_set_u8(nvs_handle_t, const char *, uint8_t);
        esp_err_t nvs_commit(nvs_handle_t);
    """))
    src = temp / "harness.c"
    src.write_text(HARNESS)
    exe = temp / "classifier"
    build = subprocess.run([
        cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-DESP_PLATFORM",
        "-I", str(inc), "-I", str(REC / "include"),
        str(REC / "shutdown_armed.c"), str(src), "-o", str(exe),
    ], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    return exe


@pytest.mark.parametrize("mode,err,action", [
    ("missing_namespace", 0, 0), ("missing_key", 0, 0),
    ("explicit_normal", 0, 0), ("host_unresolved", 0, 1),
    ("armed", 0, 1), ("manual_armed", 0, 2),
    ("invalid_value", 1, 1), ("open_error", 1, 1),
    ("read_error", 1, 1),
])
def test_production_c_missing_nvs_is_normal(classifier, mode, err, action):
    observed = subprocess.run([str(classifier), mode],
                              capture_output=True, text=True)
    assert observed.returncode == 0, observed.stderr + observed.stdout
    assert observed.stdout.strip() == f"err={err} action={action} writes=0"


def test_production_first_possible_sd_write_precedes_manifest_validation():
    main = (FW / "main" / "main.c").read_text(encoding="utf-8")
    writer = main.split("static void recorder_writer_task(void *arg) {", 1)[1]
    assert writer.index("sd_mount_recordings()") < writer.index(
        "device_identity_ensure_device_id(")
    assert writer.index("sd_mount_recordings()") < writer.index(
        "recorder_ensure_manifest_exists()")
    assert writer.index("sd_mount_recordings()") < writer.index(
        "wav_recovery_scan_recordings(")
    mount = (REC / "sd_mount.c").read_text(encoding="utf-8")
    body = mount.split("esp_err_t sd_mount_recordings(void) {", 1)[1].split(
        "esp_err_t sd_mount_ensure_date_dir(", 1)[0]
    assert body.index("sd_mount_create_storage(") < body.rindex(
        "ensure_recording_dirs()")


def test_existing_app_boot_has_no_implicit_install_grant():
    runtime = (FW / "main" / "task49_runtime.c").read_text(encoding="utf-8")
    main = (FW / "main" / "main.c").read_text(encoding="utf-8")
    provision = (REC / "dev_fresh_install_provision.c").read_text(
        encoding="utf-8")
    assert "dev_fresh_install_nvs_provision_pristine(" not in runtime + main
    assert "dev_fresh_install_nvs_provision_pristine(" in provision
    assert "RELEASE_STORAGE" not in provision
