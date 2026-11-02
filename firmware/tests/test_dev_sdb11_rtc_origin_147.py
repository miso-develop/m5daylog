"""SDB-1.1/ER-144-SDB11-02: production RTC witness and DEV first-boot path.

Compiled C extracts the actual RTC clock-proof predicates and BM8563 RTC
validation coordinator. Hardware/FS/NVS stubs are synthetic: this test does
not establish electrical BM8563, selected-card or physical GPIO success.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = (ROOT / "main" / "task49_runtime.c")
RTC = ROOT / "components" / "recorder" / "rtc_correction.c"


def _source_slice(source: str, start: str, end: str) -> str:
    first = source.index(start)
    return source[first:source.index(end, first)]


def test_real_dev_initial_recorder_boot_validates_rtc_before_recording():
    runtime = RUNTIME.read_text()
    rtc = RTC.read_text()
    app = runtime[runtime.index("void app_main(void) {"):]
    admitted = app.index("release_cycle_fence_admit_dev_first_boot()")
    rtc_section = app[app.index("if (dev_first_boot) {", admitted):
                      app.index("xTaskCreate(recorder_base_task,")]
    checks = [
        "rtc_correction_init() != ESP_OK",
        "rtc_correction_is_pending()",
        "rtc_correction_flush_pending_event(RECORDER_EVENTS_PATH) != ESP_OK",
        "!rtc_correction_cycle_status_qualified()",
    ]
    for marker in checks:
        assert marker in rtc_section
    assert all(rtc_section.index(a) < rtc_section.index(b)
               for a, b in zip(checks, checks[1:]))
    assert "recorder_shutdown_armed_now();" in rtc_section
    assert "return;  // Intent stays USED" in rtc_section
    assert "CONFIG_M5DAYLOG_SDB11_DEV_INSTALL" in app[
        admitted:app.index("if (dev_first_boot) {", admitted)]
    assert app.index("if (dev_first_boot) {", admitted) < app.index(
        "xTaskCreate(recorder_base_task,")
    assert app.index("rtc_correction_flush_pending_event(RECORDER_EVENTS_PATH)") < (
        app.index("rtc_correction_init() != ESP_OK", app.index(
            "if (action == SHUTDOWN_ARMED_BOOT_NORMAL &&")))
    # The reused #50 hook must return before any SD audit when !s_pending.
    hook = _source_slice(rtc, "esp_err_t rtc_correction_flush_pending_event(",
                         "#else  // !ESP_PLATFORM")
    assert hook.index("rtc_sync_system_clock_best_effort_locked();") < (
        hook.index("if (!s_pending)"))
    assert hook.index("if (!s_pending)") < hook.index(
        "rtc_correction_recover_event(")
    assert "s_boot_rtc_clock_verified && s_cycle_time_mutation_safe" in rtc


def test_production_rtc_clock_witness_success_and_failure_matrix(tmp_path):
    compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if compiler is None:
        pytest.skip("No C compiler on test host")
    rtc = RTC.read_text()
    predicates = _source_slice(
        rtc, "static void rtc_cycle_note_set_time_started_locked(void) {",
        "static bool rtc_is_leap(")
    actual_clock = _source_slice(
        rtc, "static void rtc_sync_system_clock_best_effort_locked(void) {",
        "static void rtc_format_epoch(")
    prefix = r"""
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <sys/time.h>
#include <time.h>
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL 1
static bool s_boot_rtc_clock_verified;
static bool s_cycle_time_mutation_safe;
static bool s_cycle_time_pending_committed_in_session;
static bool s_pending;
static int fail_init, fail_read, fail_clock, fail_deinit;
static esp_err_t rtc_hw_init(void) { return fail_init ? ESP_FAIL : ESP_OK; }
static esp_err_t rtc_hw_read_epoch(time_t *t) {
    if (fail_read) return ESP_FAIL; /* I2C, VL or invalid BCD/date */
    *t = (time_t)1780000000;
    return ESP_OK;
}
static int test_settimeofday(const struct timeval *tv, const void *tz) {
    (void)tv; (void)tz;
    return fail_clock ? -1 : 0;
}
#define settimeofday test_settimeofday
static esp_err_t rtc_hw_deinit(void) {
    return fail_deinit ? ESP_FAIL : ESP_OK;
}
"""
    main = r"""
int main(void) {
    /* NORMAL first-use boot has no prior RTC witness. */
    assert(!rtc_cycle_status_ready_locked());
    rtc_sync_system_clock_best_effort_locked();
    assert(rtc_cycle_status_ready_locked());

    /* A fully successful same-session SET_TIME preserves the boot proof. */
    rtc_cycle_note_set_time_started_locked();
    assert(!rtc_cycle_status_ready_locked());
    rtc_cycle_note_set_time_committed_locked();
    s_pending = true;
    assert(rtc_cycle_status_ready_locked());

    /* An uncommitted / indeterminate SET_TIME is not release-authorizing. */
    rtc_cycle_note_set_time_started_locked();
    assert(!rtc_cycle_status_ready_locked());
    s_pending = false;

    /* The existing production proof fails closed for all RTC failure seams. */
    for (int failure = 1; failure <= 5; ++failure) {
        fail_init = failure == 1;
        fail_read = (failure == 2 || failure == 3) ? failure : 0;
        fail_clock = failure == 4;
        fail_deinit = failure == 5;
        rtc_sync_system_clock_best_effort_locked();
        assert(!rtc_cycle_status_ready_locked());
        assert(!s_boot_rtc_clock_verified);
    }

    /* A pending audit inherited from another boot is never a fresh proof. */
    fail_init = fail_read = fail_clock = fail_deinit = 0;
    s_pending = true;
    rtc_sync_system_clock_best_effort_locked();
    assert(!rtc_cycle_status_ready_locked());

    /* Reestablishing a real valid RTC proof is a separate new-run action. */
    s_pending = false;
    rtc_sync_system_clock_best_effort_locked();
    assert(rtc_cycle_status_ready_locked());
    return 0;
}
"""
    source = prefix + predicates + actual_clock + main
    path = tmp_path / "rtc_witness_actual.c"
    exe = tmp_path / "rtc_witness"
    path.write_text(source)
    subprocess.run(
        [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
         str(path), "-o", str(exe)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run([str(exe)], check=True, capture_output=True, text=True)
