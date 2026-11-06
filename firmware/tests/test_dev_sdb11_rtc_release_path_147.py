"""Compiled production CDC status/release eligibility after DEV initial RTC proof.

Extracts the actual recorder_cdc_status, recorder_cdc_release_accept, and
RTC witness methods. Mocks transport/NVS/USB, never physical hardware.
"""
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "main" / "task49_runtime.c"
RTC = ROOT / "components" / "recorder" / "rtc_correction.c"


def _between(text: str, begin: str, end: str) -> str:
    i = text.index(begin)
    return text[i:text.index(end, i)]


def test_prod_qualified_first_rtc_witness_exposes_pair_and_admits_canonical_release(tmp_path):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        pytest.skip("No native compiler")
    runtime = RUNTIME.read_text()
    rtc = RTC.read_text()
    rtc_predicates = _between(
        rtc, "static void rtc_cycle_note_set_time_started_locked(void) {",
        "static bool rtc_is_leap(")
    rtc_clock = _between(
        rtc, "static void rtc_sync_system_clock_best_effort_locked(void) {",
        "static void rtc_format_epoch(")
    rtc_getters = _between(
        rtc, "bool rtc_correction_cycle_status_qualified(void) {",
        "rtc_correction_result_t rtc_correction_apply(")
    status = _between(
        runtime, "static bool recorder_cdc_status(usb_cdc_protocol_status_t *out, void *ctx) {",
        "static usb_cdc_set_time_result_t recorder_cdc_set_time(")
    release = _between(
        runtime, "static usb_cdc_release_result_t recorder_cdc_release_accept(",
        "static bool recorder_cdc_release_response_complete(")
    preamble = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <sys/time.h>
#include <time.h>
#define CONFIG_M5DAYLOG_SDB11_DEV_INSTALL 1
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL 1
#define pdTRUE 1
#define portMAX_DELAY 0
typedef void *SemaphoreHandle_t;
static int fake_mutex;
static SemaphoreHandle_t s_state_lock = &fake_mutex;
static SemaphoreHandle_t s_rtc_lock = &fake_mutex;
static int xSemaphoreTake(SemaphoreHandle_t m, int timeout) {
    (void)m; (void)timeout;
    return pdTRUE;
}
static void xSemaphoreGive(SemaphoreHandle_t m) { (void)m; }
static bool s_boot_rtc_clock_verified;
static bool s_cycle_time_mutation_safe;
static bool s_cycle_time_pending_committed_in_session;
static bool s_pending;
static int rtc_fail;
static esp_err_t rtc_hw_init(void) { return rtc_fail ? ESP_FAIL : ESP_OK; }
static esp_err_t rtc_hw_read_epoch(time_t *t) {
    if (rtc_fail) return ESP_FAIL;
    *t = (time_t)1780000000;
    return ESP_OK;
}
static int mock_settimeofday(const struct timeval *tv, const void *tz) {
    (void)tv; (void)tz; return rtc_fail ? -1 : 0;
}
#define settimeofday mock_settimeofday
static esp_err_t rtc_hw_deinit(void) { return rtc_fail ? ESP_FAIL : ESP_OK; }
typedef int recorder_state_t;
typedef int recorder_reason_t;
enum { RECORDER_STATE_USB_SYNC = 4 };
typedef struct {
    recorder_state_t state;
    recorder_reason_t reason;
} fake_state_t;
static fake_state_t s_rec_state = {RECORDER_STATE_USB_SYNC, 0};
static int s_last_battery_mv = 3750;
static const char *recorder_state_str(recorder_state_t s) {
    return s == RECORDER_STATE_USB_SYNC ? "USB_SYNC" : "OTHER";
}
static const char *recorder_reason_str(recorder_reason_t s) {
    (void)s; return "USB";
}
typedef struct {
    const char *state;
    const char *reason;
    int battery_mv;
    bool battery_valid;
    bool rtc_correction_pending;
    const char *release_cycle_id;
    const char *release_cycle_origin;
} usb_cdc_protocol_status_t;
static bool card_mounted;
static bool host_owned = true;
static bool command_admission = true;
static bool boot_qualified = true;
static bool fence_host_owned = true;
static const char *cycle_uuid = "11111111-1111-4111-8111-111111111111";
static const char *origin = "SUPERVISED_FIRST_USE";
static bool accepted;
static bool sd_mount_is_mounted(void) { return card_mounted; }
static bool usb_msc_ownership_is_host_owned(void) { return host_owned; }
static bool usb_msc_ownership_release_command_admission_open(void) {
    return command_admission;
}
static bool release_cycle_fence_boot_qualified(void) { return boot_qualified; }
static bool release_cycle_fence_host_owned_qualified(void) {
    return fence_host_owned;
}
static const char *release_cycle_witness_frozen_id(void) { return cycle_uuid; }
static const char *release_cycle_fence_current_origin(void) { return origin; }
typedef enum {
    USB_MSC_RELEASE_STORAGE_ACCEPTED,
    USB_MSC_RELEASE_STORAGE_INVALID_ARGS,
    USB_MSC_RELEASE_STORAGE_WRONG_STATE,
    USB_MSC_RELEASE_STORAGE_CONFLICT
} usb_msc_release_storage_result_t;
typedef enum {
    USB_CDC_RELEASE_ACCEPTED,
    USB_CDC_RELEASE_INVALID_ARGS,
    USB_CDC_RELEASE_WRONG_STATE,
    USB_CDC_RELEASE_CONFLICT
} usb_cdc_release_result_t;
static usb_msc_release_storage_result_t
usb_msc_ownership_accept_release_storage(const char *id) {
    assert(id != NULL);
    accepted = true;
    return USB_MSC_RELEASE_STORAGE_ACCEPTED;
}
static void release_cycle_fence_note_canonical_release_accepted(void) {}
static bool recorder_cdc_status(usb_cdc_protocol_status_t *out, void *ctx);
"""
    main = r"""
int main(void) {
    usb_cdc_protocol_status_t status = {0};
    /* No clock witness: selected card, PCM and physical origin are not enough. */
    assert(recorder_cdc_status(&status, NULL));
    assert(status.release_cycle_id == NULL);
    assert(status.release_cycle_origin == NULL);
    assert(recorder_cdc_release_accept("attempt", NULL) == USB_CDC_RELEASE_CONFLICT);
    assert(!accepted);
    /* First DEV NORMAL recording has an explicit pre-start valid RTC read. */
    rtc_sync_system_clock_best_effort_locked();
    assert(rtc_correction_cycle_status_qualified());
    assert(recorder_cdc_status(&status, NULL));
    assert(status.release_cycle_id == cycle_uuid);
    assert(status.release_cycle_origin == origin);
    /* Subsequent successful SET_TIME retains the same frozen pair. */
    rtc_cycle_note_set_time_started_locked();
    assert(!rtc_correction_cycle_status_qualified());
    rtc_cycle_note_set_time_committed_locked();
    s_pending = true;
    assert(recorder_cdc_status(&status, NULL));
    assert(status.release_cycle_id == cycle_uuid);
    assert(status.release_cycle_origin == origin);
    assert(recorder_cdc_release_accept("attempt", NULL) == USB_CDC_RELEASE_ACCEPTED);
    assert(accepted);
    /* Ambiguous clock mutation, RTC transport error or loss of custody deny. */
    accepted = false;
    rtc_cycle_note_set_time_started_locked();
    assert(recorder_cdc_release_accept("attempt", NULL) == USB_CDC_RELEASE_CONFLICT);
    assert(!accepted);
    s_pending = false;
    rtc_fail = 1;
    rtc_sync_system_clock_best_effort_locked();
    assert(recorder_cdc_release_accept("attempt", NULL) == USB_CDC_RELEASE_CONFLICT);
    rtc_fail = 0;
    rtc_sync_system_clock_best_effort_locked();
    card_mounted = true;
    assert(recorder_cdc_release_accept("attempt", NULL) == USB_CDC_RELEASE_CONFLICT);
    card_mounted = false;
    host_owned = false;
    assert(recorder_cdc_release_accept("attempt", NULL) == USB_CDC_RELEASE_CONFLICT);
    host_owned = true;
    fence_host_owned = false;
    assert(recorder_cdc_release_accept("attempt", NULL) == USB_CDC_RELEASE_CONFLICT);
    assert(!accepted);
    return 0;
}
"""
    path = tmp_path / "production_rtc_cdc_release.c"
    exe = tmp_path / "production_rtc_cdc_release"
    path.write_text(
        preamble + rtc_predicates + rtc_clock + rtc_getters
        + release + status + main
    )
    subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
         str(path), "-o", str(exe)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run([str(exe)], check=True, capture_output=True, text=True)
