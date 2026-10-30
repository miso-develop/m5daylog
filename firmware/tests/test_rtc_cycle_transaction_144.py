"""#144 F-144-R05: execute production RTC SET_TIME + exact CDC status provider.

A host-only dependency harness replaces BM8563/I2C, NVS, FreeRTOS and system
clock boundaries. It compiles the *whole* production rtc_correction.c (not
extracted state helpers), immutable release witness, CDC dispatcher, and the
unmodified recorder_cdc_status() body selected from task49_runtime.c.
No actual USB, RTC, flash, SD, power, or device mutation is performed.
"""
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMP = ROOT / "firmware/components/recorder"
NATIVE = ROOT / "firmware/tests/native"

STUB_HEADERS = {
    "esp_err.h": """
        #pragma once
        typedef int esp_err_t;
        #define ESP_OK 0
        #define ESP_FAIL 1
        #define ESP_ERR_INVALID_ARG 2
        #define ESP_ERR_INVALID_STATE 3
        #define ESP_ERR_NO_MEM 4
        #define ESP_ERR_NOT_SUPPORTED 5
        #define ESP_ERR_NVS_NOT_FOUND 10
    """,
    "esp_random.h": """
        #pragma once
        #include <stdint.h>
        uint32_t esp_random(void);
    """,
    "freertos/FreeRTOS.h": """
        #pragma once
        #define pdTRUE 1
        #define portMAX_DELAY 0xffffffffu
    """,
    "freertos/semphr.h": """
        #pragma once
        #include "freertos/FreeRTOS.h"
        typedef void *SemaphoreHandle_t;
        SemaphoreHandle_t xSemaphoreCreateMutex(void);
        int xSemaphoreTake(SemaphoreHandle_t handle, unsigned timeout);
        int xSemaphoreGive(SemaphoreHandle_t handle);
    """,
    "nvs.h": """
        #pragma once
        #include <stddef.h>
        #include <stdint.h>
        #include "esp_err.h"
        typedef int nvs_handle_t;
        #define NVS_READONLY 0
        #define NVS_READWRITE 1
        esp_err_t nvs_open(const char *, int, nvs_handle_t *);
        void nvs_close(nvs_handle_t);
        esp_err_t nvs_get_blob(nvs_handle_t, const char *, void *, size_t *);
        esp_err_t nvs_set_blob(nvs_handle_t, const char *, const void *, size_t);
        esp_err_t nvs_erase_key(nvs_handle_t, const char *);
        esp_err_t nvs_commit(nvs_handle_t);
    """,
    "driver/i2c_master.h": """
        #pragma once
        #include <stddef.h>
        #include <stdint.h>
        #include "esp_err.h"
        typedef void *i2c_master_bus_handle_t;
        typedef void *i2c_master_dev_handle_t;
        #define I2C_CLK_SRC_DEFAULT 0
        #define I2C_ADDR_BIT_LEN_7 7
        typedef struct {
            int i2c_port;
            int sda_io_num;
            int scl_io_num;
            int clk_source;
            int glitch_ignore_cnt;
            struct { unsigned enable_internal_pullup : 1; } flags;
        } i2c_master_bus_config_t;
        typedef struct {
            int dev_addr_length;
            int device_address;
            unsigned scl_speed_hz;
        } i2c_device_config_t;
        esp_err_t i2c_new_master_bus(const i2c_master_bus_config_t *,
                                     i2c_master_bus_handle_t *);
        esp_err_t i2c_del_master_bus(i2c_master_bus_handle_t);
        esp_err_t i2c_master_bus_add_device(i2c_master_bus_handle_t,
                                            const i2c_device_config_t *,
                                            i2c_master_dev_handle_t *);
        esp_err_t i2c_master_bus_rm_device(i2c_master_dev_handle_t);
        esp_err_t i2c_master_transmit(i2c_master_dev_handle_t,
                                      const uint8_t *, size_t, int);
        esp_err_t i2c_master_transmit_receive(i2c_master_dev_handle_t,
                                      const uint8_t *, size_t, uint8_t *,
                                      size_t, int);
    """,
}

# The full production status provider is inserted byte-for-byte at the marker.
# This retains its real USB/ownership/RTC/fence/manifest predicates without
# compiling the unrelated recorder tasks and TinyUSB application entry point.
HARNESS = r"""
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include "esp_err.h"
#include "freertos/semphr.h"
#include "driver/i2c_master.h"
#include "nvs.h"
#include "rtc_correction.h"
#include "rtc_correction_event.h"
#include "device_identity.h"
#include "recorder_config.h"
#include "release_cycle_fence.h"
#include "release_cycle_witness.h"
#include "usb_cdc_protocol_core.h"

#define A "01234567-89ab-4def-8123-456789abcdef"
#define B "11111111-2222-4333-8444-555555555555"
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "CHECK %d: %s\n", __LINE__, #x); exit(9); } } while (0)

static bool g_invalid_boot_rtc;
static bool g_fail_rtc_write;
static bool g_fail_system_clock;
static bool g_fail_nvs_set;
static int g_fail_commit; /* 1 before commit, 2 after durable write */
static bool g_has_pending_blob;
static unsigned char g_committed_blob[256];
static size_t g_committed_size;
static unsigned char g_staged_blob[256];
static size_t g_staged_size;
static bool g_blob_staged;
static int g_clock_calls;
static bool g_host_owned = true;
static bool g_sd_unmounted = true;
static bool g_admission_open = true;
static bool g_boot_fence = true;
static bool g_host_fence = true;

SemaphoreHandle_t xSemaphoreCreateMutex(void) { return (void *)1; }
int xSemaphoreTake(SemaphoreHandle_t h, unsigned t) { (void)t; return h != NULL ? pdTRUE : 0; }
int xSemaphoreGive(SemaphoreHandle_t h) { return h != NULL ? pdTRUE : 0; }
esp_err_t recorder_nvs_init(void) { return ESP_OK; }
esp_err_t recorder_nvs_lock(void) { return ESP_OK; }
void recorder_nvs_unlock(void) {}

uint32_t esp_random(void) { static uint32_t n = 1; return n++; }
bool device_identity_format_uuid_v4(const uint8_t bytes[16], char out[RECORDER_UUID_STR_LEN]) {
    (void)bytes; memcpy(out, B, sizeof(B)); return true;
}
bool device_identity_is_valid_uuid(const char *s) {
    return s != NULL && strlen(s) == 36;
}

int rtc_test_settimeofday(const struct timeval *tv, const struct timezone *tz) {
    (void)tv; (void)tz;
    g_clock_calls++;
    return (g_fail_system_clock && g_clock_calls > 1) ? -1 : 0;
}

esp_err_t i2c_new_master_bus(const i2c_master_bus_config_t *cfg, i2c_master_bus_handle_t *p) {
    (void)cfg; *p = (void *)1; return ESP_OK;
}
esp_err_t i2c_del_master_bus(i2c_master_bus_handle_t p) { (void)p; return ESP_OK; }
esp_err_t i2c_master_bus_add_device(i2c_master_bus_handle_t b, const i2c_device_config_t *c,
                                    i2c_master_dev_handle_t *p) {
    (void)b; (void)c; *p = (void *)2; return ESP_OK;
}
esp_err_t i2c_master_bus_rm_device(i2c_master_dev_handle_t p) { (void)p; return ESP_OK; }
esp_err_t i2c_master_transmit(i2c_master_dev_handle_t p, const uint8_t *data, size_t n, int t) {
    (void)p; (void)t;
    if (n == 8 && data[0] == 2 && g_fail_rtc_write) return ESP_FAIL;
    return ESP_OK;
}
esp_err_t i2c_master_transmit_receive(i2c_master_dev_handle_t p, const uint8_t *w,
                                      size_t wn, uint8_t *out, size_t n, int t) {
    (void)p; (void)t;
    CHECK(wn == 1 && w[0] == 2 && n == 7);
    /* Valid BM8563 2026-10-30 12:30:00, or VL-invalid on boot. */
    const uint8_t fixed[] = {0x00, 0x30, 0x12, 0x30, 0x05, 0x10, 0x26};
    memcpy(out, fixed, sizeof(fixed));
    if (g_invalid_boot_rtc) out[0] |= 0x80;
    return ESP_OK;
}

esp_err_t nvs_open(const char *ns, int mode, nvs_handle_t *h) {
    (void)ns; (void)mode; *h = 1; return ESP_OK;
}
void nvs_close(nvs_handle_t h) { (void)h; }
esp_err_t nvs_get_blob(nvs_handle_t h, const char *key, void *dst, size_t *n) {
    (void)h; (void)key;
    if (!g_has_pending_blob) return ESP_ERR_NVS_NOT_FOUND;
    CHECK(*n >= g_committed_size);
    memcpy(dst, g_committed_blob, g_committed_size);
    *n = g_committed_size; return ESP_OK;
}
esp_err_t nvs_set_blob(nvs_handle_t h, const char *key, const void *data, size_t n) {
    (void)h; (void)key;
    if (g_fail_nvs_set) return ESP_FAIL;
    CHECK(n <= sizeof(g_staged_blob));
    memcpy(g_staged_blob, data, n);
    g_staged_size = n; g_blob_staged = true;
    return ESP_OK;
}
esp_err_t nvs_erase_key(nvs_handle_t h, const char *key) {
    (void)h; (void)key;
    g_has_pending_blob = false; return ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t h) {
    (void)h;
    if (g_fail_commit == 1) return ESP_FAIL;
    if (g_blob_staged) {
        memcpy(g_committed_blob, g_staged_blob, g_staged_size);
        g_committed_size = g_staged_size;
        g_has_pending_blob = true;
        g_blob_staged = false;
    }
    return g_fail_commit == 2 ? ESP_FAIL : ESP_OK;
}

esp_err_t rtc_correction_recover_event(
    const char *path, const rtc_correction_event_t *event,
    rtc_correction_pending_clear_fn_t clear, void *ctx) {
    (void)path; (void)event; (void)clear; (void)ctx;
    /* Initial boot has no pending audit. */
    return ESP_FAIL;
}

typedef enum { RECORDER_STATE_RECORDING, RECORDER_STATE_USB_SYNC } recorder_state_t;
typedef enum { RECORDER_REASON_USB } recorder_reason_t;
static struct { recorder_state_t state; recorder_reason_t reason; } s_rec_state = {
    RECORDER_STATE_USB_SYNC, RECORDER_REASON_USB
};
static SemaphoreHandle_t s_state_lock = (void *)1;
static int s_last_battery_mv = 3900;
const char *recorder_state_str(recorder_state_t state) {
    return state == RECORDER_STATE_USB_SYNC ? "USB_SYNC" : "RECORDING";
}
const char *recorder_reason_str(recorder_reason_t reason) { (void)reason; return "usb"; }
bool sd_mount_is_mounted(void) { return !g_sd_unmounted; }
bool usb_msc_ownership_is_host_owned(void) { return g_host_owned; }
bool usb_msc_ownership_release_command_admission_open(void) { return g_admission_open; }
bool release_cycle_fence_boot_qualified(void) { return g_boot_fence; }
bool release_cycle_fence_host_owned_qualified(void) { return g_host_fence; }

/* INSERT_EXACT_PRODUCTION_STATUS_PROVIDER */

/* INSERT_EXACT_PRODUCTION_SET_TIME_CALLBACK */
static void request(const usb_cdc_protocol_config_t *cfg, const char *raw,
                    const char *expected, const char *missing) {
    char out[768] = {0};
    usb_cdc_protocol_effect_t effect = {0};
    int n = usb_cdc_protocol_process_line(
        (const uint8_t *)raw, strlen(raw), out, sizeof(out), cfg, &effect);
    CHECK(n > 0 && (size_t)n < sizeof(out));
    if (expected != NULL && strstr(out, expected) == NULL) {
        fprintf(stderr, "missing '%s': %s\n", expected, out); exit(8);
    }
    if (missing != NULL && strstr(out, missing) != NULL) {
        fprintf(stderr, "unexpected '%s': %s\n", missing, out); exit(8);
    }
}
#define STATUS "{\"id\":\"s\",\"cmd\":\"GET_STATUS\",\"args\":{}}"
#define TIME "{\"id\":\"t\",\"cmd\":\"SET_TIME\",\"args\":{\"time\":\"2026-10-03T10:30:00+09:00\"}}"
#define INVALID "{\"id\":\"x\",\"cmd\":\"SET_TIME\",\"args\":{\"time\":\"not-a-time\"}}"
#define ID_FIELD "\"releaseCycleId\":\"" A "\""
#define NO_ID "\"releaseCycleId\""

int main(int argc, char **argv) {
    CHECK(argc == 2);
    const char *scenario = argv[1];
    g_invalid_boot_rtc = strcmp(scenario, "bad_boot") == 0;
    CHECK(rtc_correction_init() == ESP_OK);
    CHECK(rtc_correction_flush_pending_event("host-only") == ESP_OK);
    CHECK(rtc_correction_boot_clock_verified() == !g_invalid_boot_rtc);
    if (!g_invalid_boot_rtc) {
        CHECK(release_cycle_witness_note_committed(A, 32768u, true, false));
    } else {
        CHECK(!release_cycle_witness_note_committed(A, 32768u, false, false));
    }
    usb_cdc_protocol_config_t cfg = {0};
    cfg.device_id = A;
    cfg.status_provider = recorder_cdc_status; /* actual production function */
    cfg.set_time = recorder_cdc_set_time;              /* calls real RTC transaction */
    request(&cfg, STATUS, NULL, g_invalid_boot_rtc ? NO_ID : NULL);
    if (!g_invalid_boot_rtc) request(&cfg, STATUS, ID_FIELD, NULL);

    if (strcmp(scenario, "invalid") == 0) {
        request(&cfg, INVALID, "\"code\":\"INVALID_ARGS\"", NULL);
        CHECK(!rtc_correction_is_pending());
        request(&cfg, STATUS, ID_FIELD, NULL);
    } else if (strcmp(scenario, "owner") == 0) {
        g_host_owned = false;
        request(&cfg, STATUS, NULL, NO_ID);
        g_host_owned = true;
        g_sd_unmounted = false;
        request(&cfg, STATUS, NULL, NO_ID);
        g_sd_unmounted = true;
        g_admission_open = false;
        request(&cfg, STATUS, NULL, NO_ID);
        g_admission_open = true;
        g_host_fence = false;
        request(&cfg, STATUS, NULL, NO_ID);
        g_host_fence = true;
        s_rec_state.state = RECORDER_STATE_RECORDING;
        request(&cfg, STATUS, NULL, NO_ID);
        s_rec_state.state = RECORDER_STATE_USB_SYNC;
        request(&cfg, STATUS, ID_FIELD, NULL);
    } else {
        g_fail_rtc_write = strcmp(scenario, "rtc_write_fail") == 0;
        g_fail_system_clock = strcmp(scenario, "system_clock_fail") == 0;
        g_fail_nvs_set = strcmp(scenario, "nvs_set_fail") == 0;
        if (strcmp(scenario, "commit_before") == 0) g_fail_commit = 1;
        if (strcmp(scenario, "commit_after") == 0) g_fail_commit = 2;
        bool should_succeed = strcmp(scenario, "success") == 0 ||
                              strcmp(scenario, "busy") == 0 ||
                              strcmp(scenario, "reconnect") == 0 ||
                              strcmp(scenario, "rotation") == 0 ||
                              strcmp(scenario, "bad_boot") == 0;
        request(&cfg, TIME, should_succeed ? "\"eventPending\":true" :
                     "\"code\":\"INTERNAL_ERROR\"", NULL);
        CHECK(rtc_correction_boot_clock_verified() == !g_invalid_boot_rtc);
        if (should_succeed) {
            CHECK(rtc_correction_is_pending());
            request(&cfg, STATUS, "\"rtcCorrectionPending\":true", NULL);
            request(&cfg, STATUS, g_invalid_boot_rtc ? NULL : ID_FIELD,
                    g_invalid_boot_rtc ? NO_ID : NULL);
            if (strcmp(scenario, "busy") == 0) {
                request(&cfg, TIME, "\"code\":\"BUSY\"", NULL);
                request(&cfg, STATUS, ID_FIELD, NULL);
            }
            if (strcmp(scenario, "reconnect") == 0) {
                g_host_owned = false;
                request(&cfg, STATUS, NULL, NO_ID);
                g_host_owned = true;
                request(&cfg, STATUS, ID_FIELD, NULL);
            }
            if (strcmp(scenario, "rotation") == 0) {
                CHECK(!release_cycle_witness_note_committed(B, 32768u, true, false));
                request(&cfg, STATUS, ID_FIELD, NULL);
            }
        } else {
            CHECK(strcmp(release_cycle_witness_frozen_id(), A) == 0);
            request(&cfg, STATUS, NULL, NO_ID);
            if (g_fail_commit == 2) CHECK(g_has_pending_blob);
        }
    }
    puts("RTC144_TRANSACTION=PASS");
    return 0;
}
"""


@pytest.fixture(scope="module")
def transaction_binary(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.fail("C compiler required: production transaction gate cannot be skipped")
    tmp = tmp_path_factory.mktemp("rtc144-transaction")
    for name, source in STUB_HEADERS.items():
        dst = tmp / "include" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(textwrap.dedent(source), encoding="utf-8")

    runtime = (ROOT / "firmware/main/task49_runtime.c").read_text(encoding="utf-8")
    marker = "/* INSERT_EXACT_PRODUCTION_STATUS_PROVIDER */"
    start = runtime.index("static bool recorder_cdc_status(")
    end = runtime.index("static usb_cdc_set_time_result_t recorder_cdc_set_time(", start)
    provider = runtime[start:end].strip()
    assert "rtc_correction_cycle_status_qualified()" in provider
    assert "release_cycle_witness_frozen_id()" in provider
    assert "release_cycle_fence_host_owned_qualified()" in provider
    assert "usb_msc_ownership_is_host_owned()" in provider
    harness = tmp / "transaction.c"
    setter_start = runtime.index("static usb_cdc_set_time_result_t recorder_cdc_set_time(", end)
    setter_end = runtime.index("static void recorder_task49_delete(", setter_start)
    setter = runtime[setter_start:setter_end].strip()
    assert "rtc_correction_apply(requested_time, normalized, normalized_size)" in setter
    generated = HARNESS.replace(marker, provider).replace(
        "/* INSERT_EXACT_PRODUCTION_SET_TIME_CALLBACK */", setter)
    harness.write_text(generated, encoding="utf-8")

    exe = tmp / "transaction"
    args = [
        cc, "-std=c11", "-D_DEFAULT_SOURCE", "-D_POSIX_C_SOURCE=200809L", "-DESP_PLATFORM",
        "-Dsettimeofday=rtc_test_settimeofday",
        "-Wall", "-Wextra", "-Werror", "-include", "stdio.h",
        "-I", str(tmp / "include"), "-I", str(COMP / "include"),
        "-I", str(NATIVE / "include"),
        str(COMP / "rtc_correction.c"),
        str(COMP / "release_cycle_witness.c"),
        str(COMP / "usb_cdc_protocol_core.c"),
        str(NATIVE / "cjson_stub.c"), str(harness), "-o", str(exe),
    ]
    result = subprocess.run(args, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return exe


@pytest.mark.parametrize("scenario", [
    "success", "invalid", "busy", "rtc_write_fail", "system_clock_fail",
    "nvs_set_fail", "commit_before", "commit_after", "bad_boot",
    "owner", "reconnect", "rotation",
])
def test_production_rtc_transaction_and_cdc_status(transaction_binary, scenario):
    result = subprocess.run(
        [str(transaction_binary), scenario], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RTC144_TRANSACTION=PASS" in result.stdout
