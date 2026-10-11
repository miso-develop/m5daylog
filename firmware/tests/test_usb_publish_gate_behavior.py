"""Task #87 regression for post-status two-phase MSC publication.

The first esp_tinyusb ATTACHED callback executes before SET_CONFIGURATION status
is sent, so it must never disconnect. A completed provisional MSC SCSI command
proves Windows finished configuration and class binding. The recorder
coordinator may then disconnect, complete the durable HOST_UNRESOLVED + APP ->
USB ownership transfer, wait a host-visible detach interval, and reconnect with
the LUN already USB-owned.
"""

from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[2]
RECORDER = REPO / "firmware/components/recorder"
INCLUDE = RECORDER / "include"
MAIN_CMAKE = REPO / "firmware/main/CMakeLists.txt"
STUB_INCLUDE = REPO / "firmware/tests/native/include"

STUB_HEADERS = {
    "esp_err.h": r"""
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
    "esp_log.h": r"""
        #pragma once
        static inline void test_esp_log(const char *tag, const char *fmt, ...) {
            (void)tag; (void)fmt;
        }
        #define ESP_LOGI(...) test_esp_log(__VA_ARGS__)
        #define ESP_LOGW(...) test_esp_log(__VA_ARGS__)
        #define ESP_LOGE(...) test_esp_log(__VA_ARGS__)
    """,
    "freertos/FreeRTOS.h": r"""
        #pragma once
        #include <stdint.h>
        typedef uint32_t TickType_t;
        typedef uint32_t EventBits_t;
        typedef int BaseType_t;
        struct event_group;
        typedef struct event_group *EventGroupHandle_t;
        #define pdTRUE 1
        #define pdFALSE 0
        #define portMAX_DELAY UINT32_MAX
        #define pdMS_TO_TICKS(ms) ((TickType_t)(ms))
    """,
    "freertos/event_groups.h": r"""
        #pragma once
        #include "freertos/FreeRTOS.h"
        EventGroupHandle_t xEventGroupCreate(void);
        void vEventGroupDelete(EventGroupHandle_t group);
        EventBits_t xEventGroupSetBits(EventGroupHandle_t group, EventBits_t bits);
        EventBits_t xEventGroupClearBits(EventGroupHandle_t group, EventBits_t bits);
        EventBits_t xEventGroupWaitBits(EventGroupHandle_t group,
                                        EventBits_t bits,
                                        BaseType_t clear_on_exit,
                                        BaseType_t wait_for_all,
                                        TickType_t ticks);
    """,
    "freertos/task.h": r"""
        #pragma once
        #include "freertos/FreeRTOS.h"
        void vTaskDelay(TickType_t ticks);
    """,
    "tinyusb.h": r"""
        #pragma once
        #include "esp_err.h"
        typedef enum {
            TINYUSB_EVENT_ATTACHED = 1,
            TINYUSB_EVENT_DETACHED = 2,
            TINYUSB_EVENT_SUSPENDED = 3,
        } tinyusb_event_id_t;
        typedef struct { tinyusb_event_id_t id; } tinyusb_event_t;
        typedef void (*tinyusb_event_cb_t)(tinyusb_event_t *event, void *arg);
        typedef struct {
            tinyusb_event_cb_t event_cb;
            void *event_arg;
        } tinyusb_config_t;
        #define TINYUSB_DEFAULT_CONFIG() ((tinyusb_config_t){0})
        esp_err_t tinyusb_driver_install(const tinyusb_config_t *config);
        esp_err_t tinyusb_driver_uninstall(void);
    """,
    "tinyusb_default_config.h": r"""
        #pragma once
        #include "tinyusb.h"
    """,
    "tusb.h": r"""
        #pragma once
        #include <stdbool.h>
        #include <stdint.h>
        bool tud_disconnect(void);
        bool tud_connect(void);
        bool tud_msc_set_sense(uint8_t lun, uint8_t sense_key,
                               uint8_t add_sense_code,
                               uint8_t add_sense_qualifier);
        void tud_msc_scsi_complete_cb(uint8_t lun,
                                      uint8_t const scsi_cmd[16]);
    """,
    "tinyusb_msc.h": r"""
        #pragma once
        #include "esp_err.h"
        typedef void *tinyusb_msc_storage_handle_t;
        typedef enum {
            TINYUSB_MSC_STORAGE_MOUNT_APP = 1,
            TINYUSB_MSC_STORAGE_MOUNT_USB = 2,
        } tinyusb_msc_mount_point_t;
        typedef enum {
            TINYUSB_MSC_EVENT_MOUNT_START = 1,
            TINYUSB_MSC_EVENT_MOUNT_COMPLETE = 2,
            TINYUSB_MSC_EVENT_MOUNT_FAILED = 3,
            TINYUSB_MSC_EVENT_FORMAT_REQUIRED = 4,
            TINYUSB_MSC_EVENT_FORMAT_FAILED = 5,
        } tinyusb_msc_event_id_t;
        typedef struct {
            tinyusb_msc_event_id_t id;
            tinyusb_msc_mount_point_t mount_point;
        } tinyusb_msc_event_t;
        typedef void (*tinyusb_msc_storage_callback_t)(
            tinyusb_msc_storage_handle_t,
            tinyusb_msc_event_t *,
            void *);
        esp_err_t tinyusb_msc_set_storage_callback(
            tinyusb_msc_storage_callback_t callback, void *arg);
    """,
    "sd_mount.h": r"""
        #pragma once
        #include <stdbool.h>
        #include "esp_err.h"
        bool sd_mount_is_mounted(void);
        esp_err_t sd_mount_transfer_to_usb(void);
        void sd_mount_note_usb_owned(void);
        esp_err_t sd_mount_release_usb_storage(void);
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

    #include "freertos/FreeRTOS.h"
    #include "freertos/event_groups.h"
    #include "freertos/task.h"
    #include "nvs.h"
    #include "recorder_nvs.h"
    #include "sd_mount.h"
    #include "shutdown_armed.h"
    #include "tinyusb.h"
    #include "tinyusb_msc.h"
    #include "tusb.h"
    #include "usb_msc_ownership.h"
    #include "usb_cdc_protocol_core.h"
    #include "usb_cdc_session_gate.h"

    #define CHECK(expr) do { \
        if (!(expr)) { \
            fprintf(stderr, "CHECK failed %s:%d: %s\n", __FILE__, __LINE__, #expr); \
            exit(2); \
        } \
    } while (0)

    enum { LIFECYCLE_HOST_UNRESOLVED = 2 };
    struct event_group { EventBits_t bits; };

    static tinyusb_event_cb_t g_device_cb;
    static void *g_device_arg;
    static tinyusb_msc_storage_callback_t g_storage_cb;
    static void *g_storage_arg;
    static bool g_mounted = true;
    static bool g_release_requested;
    static bool g_device_fs_released;
    static int g_disconnect_calls;
    static int g_connect_calls;
    static int g_transfer_calls;
    static TickType_t g_delay_ticks;
    static bool g_nvs_has_value;
    static uint8_t g_nvs_value;
    static bool g_nvs_pending;
    static uint8_t g_nvs_pending_value;
    static int g_cdc_cutoff_calls;
    static unsigned g_set_time_calls;
    static usb_cdc_session_gate_t g_cdc_gate;
    static usb_cdc_protocol_framer_t g_cdc_framer;

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
        g_nvs_pending = false;
    }
    esp_err_t nvs_get_u8(nvs_handle_t handle, const char *key, uint8_t *value) {
        CHECK(handle == 7);
        CHECK(strcmp(key, "shutdown_armed") == 0);
        if (!g_nvs_has_value) return ESP_ERR_NVS_NOT_FOUND;
        *value = g_nvs_value;
        return ESP_OK;
    }
    esp_err_t nvs_set_u8(nvs_handle_t handle, const char *key, uint8_t value) {
        CHECK(handle == 7);
        CHECK(strcmp(key, "shutdown_armed") == 0);
        g_nvs_pending = true;
        g_nvs_pending_value = value;
        return ESP_OK;
    }
    esp_err_t nvs_commit(nvs_handle_t handle) {
        CHECK(handle == 7);
        CHECK(g_nvs_pending);
        g_nvs_has_value = true;
        g_nvs_value = g_nvs_pending_value;
        g_nvs_pending = false;
        return ESP_OK;
    }

    EventGroupHandle_t xEventGroupCreate(void) {
        return calloc(1, sizeof(struct event_group));
    }
    void vEventGroupDelete(EventGroupHandle_t group) { free(group); }
    EventBits_t xEventGroupSetBits(EventGroupHandle_t group, EventBits_t bits) {
        group->bits |= bits;
        return group->bits;
    }
    EventBits_t xEventGroupClearBits(EventGroupHandle_t group, EventBits_t bits) {
        EventBits_t before = group->bits;
        group->bits &= ~bits;
        return before;
    }
    EventBits_t xEventGroupWaitBits(EventGroupHandle_t group,
                                    EventBits_t bits,
                                    BaseType_t clear_on_exit,
                                    BaseType_t wait_for_all,
                                    TickType_t ticks) {
        (void)wait_for_all;
        (void)ticks;
        EventBits_t result = group->bits;
        if (clear_on_exit && (result & bits) != 0) group->bits &= ~bits;
        return result;
    }
    void vTaskDelay(TickType_t ticks) { g_delay_ticks = ticks; }

    static bool cdc_status(usb_cdc_protocol_status_t *out, void *ctx) {
        (void)ctx;
        out->state = "USB_SYNC";
        out->reason = "usb";
        out->battery_mv = 3900;
        out->battery_valid = true;
        out->rtc_correction_pending = false;
        return true;
    }

    static usb_cdc_set_time_result_t cdc_set_time(
        const char *requested_time, char *normalized, size_t normalized_size,
        void *ctx) {
        (void)requested_time;
        (void)ctx;
        g_set_time_calls++;
        if (normalized != NULL && normalized_size > 0u) {
            normalized[0] = '\0';
        }
        return USB_CDC_SET_TIME_OK;
    }

    static usb_cdc_release_result_t cdc_release_accept(
        const char *attempt_id, void *ctx) {
        (void)attempt_id;
        (void)ctx;
        return USB_CDC_RELEASE_ACCEPTED;
    }

    static bool cdc_release_complete(const char *attempt_id, void *ctx) {
        (void)attempt_id;
        (void)ctx;
        return true;
    }

    static bool cdc_admission_open(void *ctx) {
        (void)ctx;
        return true;
    }

    static void cdc_session_cutoff(void *ctx) {
        CHECK(ctx == (void *)0x50);
        g_cdc_cutoff_calls++;

        /* Production usb_cdc_protocol_close_session() performs the same
         * generation cutoff synchronously; its worker resets framing before
         * consuming any later RX notification. Model that worker-visible
         * boundary here with the production gate/framer primitives. */
        usb_cdc_session_gate_close(&g_cdc_gate);
        usb_cdc_protocol_framer_reset(&g_cdc_framer);
    }

    bool sd_mount_is_mounted(void) { return g_mounted; }
    esp_err_t sd_mount_transfer_to_usb(void) {
        tinyusb_msc_event_t start = {
            .id = TINYUSB_MSC_EVENT_MOUNT_START,
            .mount_point = TINYUSB_MSC_STORAGE_MOUNT_APP,
        };
        tinyusb_msc_event_t complete = {
            .id = TINYUSB_MSC_EVENT_MOUNT_COMPLETE,
            .mount_point = TINYUSB_MSC_STORAGE_MOUNT_USB,
        };
        g_transfer_calls++;
        CHECK(g_storage_cb != NULL);
        CHECK(g_nvs_has_value && g_nvs_value == LIFECYCLE_HOST_UNRESOLVED);
        g_storage_cb((void *)1, &start, g_storage_arg);
        CHECK(g_release_requested);
        CHECK(g_device_fs_released);
        g_storage_cb((void *)1, &complete, g_storage_arg);
        return ESP_OK;
    }
    void sd_mount_note_usb_owned(void) { g_mounted = false; }
    esp_err_t sd_mount_release_usb_storage(void) { return ESP_OK; }

    esp_err_t tinyusb_msc_set_storage_callback(tinyusb_msc_storage_callback_t cb,
                                                void *arg) {
        g_storage_cb = cb;
        g_storage_arg = arg;
        return ESP_OK;
    }
    esp_err_t tinyusb_driver_install(const tinyusb_config_t *config) {
        g_device_cb = config->event_cb;
        g_device_arg = config->event_arg;
        return ESP_OK;
    }
    esp_err_t tinyusb_driver_uninstall(void) { return ESP_OK; }
    bool tud_disconnect(void) { g_disconnect_calls++; return true; }
    bool tud_connect(void) { g_connect_calls++; return true; }
    bool tud_msc_set_sense(uint8_t lun, uint8_t sense_key,
                           uint8_t add_sense_code,
                           uint8_t add_sense_qualifier) {
        (void)lun;
        (void)sense_key;
        (void)add_sense_code;
        (void)add_sense_qualifier;
        return true;
    }
    bool __real_tud_msc_test_unit_ready_cb(uint8_t lun) {
        (void)lun;
        return true;
    }
    void __real_tud_msc_capacity_cb(uint8_t lun,
                                    uint32_t *block_count,
                                    uint16_t *block_size) {
        (void)lun;
        *block_count = 1024u;
        *block_size = 512u;
    }
    int32_t __real_tud_msc_read10_cb(uint8_t lun, uint32_t lba,
                                     uint32_t offset, void *buffer,
                                     uint32_t bufsize) {
        (void)lun;
        (void)lba;
        (void)offset;
        (void)buffer;
        return (int32_t)bufsize;
    }
    int32_t __real_tud_msc_write10_cb(uint8_t lun, uint32_t lba,
                                      uint32_t offset, uint8_t *buffer,
                                      uint32_t bufsize) {
        (void)lun;
        (void)lba;
        (void)offset;
        (void)buffer;
        return (int32_t)bufsize;
    }
    bool __real_tud_msc_start_stop_cb(uint8_t lun,
                                      uint8_t power_condition,
                                      bool start,
                                      bool load_eject) {
        (void)lun;
        (void)power_condition;
        (void)start;
        (void)load_eject;
        return true;
    }
    static usb_msc_ownership_event_t next_event(void) {
        return usb_msc_ownership_wait_event(0);
    }

    int main(void) {
        tinyusb_event_t attached = { .id = TINYUSB_EVENT_ATTACHED };
        uint8_t test_unit_ready[16] = {0x00u};
        usb_cdc_protocol_config_t cdc_config = {
            .device_id = "01234567-89ab-4def-8123-456789abcdef",
            .status_provider = cdc_status,
            .set_time = cdc_set_time,
            .release_accept = cdc_release_accept,
            .release_response_complete = cdc_release_complete,
            .command_admission_open = cdc_admission_open,
        };
        const char *partial =
            "{\"id\":\"old\",\"cmd\":\"SET_TIME\",\"args\":{";
        const char *tail =
            "\"time\":\"2026-10-03T02:00:00Z\"}}\n";
        const char *fresh_ping =
            "{\"id\":\"new\",\"cmd\":\"PING\",\"args\":{}}\n";
        const uint8_t *line = NULL;
        size_t line_len = 0u;
        size_t i;
        char response[768] = {0};
        usb_cdc_protocol_effect_t effect = {0};
        uint32_t stale_generation;
        uint32_t discard_epoch;

        usb_cdc_session_gate_init(&g_cdc_gate);
        usb_cdc_protocol_framer_init(&g_cdc_framer);

        CHECK(usb_msc_ownership_init() == ESP_OK);
        CHECK(usb_msc_ownership_set_physical_session_cutoff(
                  cdc_session_cutoff, (void *)0x50) == ESP_OK);
        CHECK(usb_msc_ownership_start() == ESP_OK);
        CHECK(g_device_cb != NULL);
        CHECK(g_storage_cb != NULL);

        /* ATTACHED runs inside tud_mount_cb before SET_CONFIGURATION status.
         * It records provisional configuration only and must not disconnect. */
        g_device_cb(&attached, g_device_arg);
        CHECK(g_disconnect_calls == 0);
        CHECK(g_connect_calls == 0);
        CHECK(g_transfer_calls == 0);
        CHECK(g_mounted);
        CHECK(!g_nvs_has_value);
        CHECK(next_event() == USB_MSC_EVENT_NONE);

        /* SCSI completion occurs after command status and proves MSC class
         * binding. Only then may the recorder coordinator begin prepare. */
        tud_msc_scsi_complete_cb(0, test_unit_ready);
        CHECK(next_event() == USB_MSC_EVENT_ATTACH);
        CHECK(g_disconnect_calls == 0);
        CHECK(usb_msc_ownership_begin_prepare() == ESP_OK);
        CHECK(g_disconnect_calls == 1);
        CHECK(!g_nvs_has_value);

        /* Model recorder USB_PREPARE completion, then publish while detached. */
        g_release_requested = true;
        g_device_fs_released = true;
        CHECK(usb_msc_ownership_note_prepare_complete(true, true, true) == ESP_OK);
        CHECK(g_nvs_has_value);
        CHECK(g_nvs_value == LIFECYCLE_HOST_UNRESOLVED);
        CHECK(g_transfer_calls == 1);
        CHECK(!g_mounted);
        CHECK(!usb_msc_ownership_is_host_owned());
        CHECK(g_delay_ticks == 1000u);
        CHECK(g_connect_calls == 1);
        CHECK(next_event() == USB_MSC_EVENT_NONE);

        /* Only fresh SetConfiguration promotes reserved storage to host-owned. */
        g_device_cb(&attached, g_device_arg);
        CHECK(g_disconnect_calls == 1);
        CHECK(g_connect_calls == 1);
        CHECK(g_transfer_calls == 1);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(next_event() == USB_MSC_EVENT_HOST_OWNED);
        CHECK(next_event() == USB_MSC_EVENT_NONE);

        /* Open the application gate at the lifecycle's USB_SYNC point and
         * accumulate a mutating request prefix without a newline. */
        CHECK(usb_cdc_session_gate_open(&g_cdc_gate));
        stale_generation = usb_cdc_session_gate_generation(&g_cdc_gate);
        for (i = 0u; partial[i] != '\0'; ++i) {
            CHECK(usb_cdc_protocol_framer_feed(
                      &g_cdc_framer, (uint8_t)partial[i],
                      &line, &line_len) == USB_CDC_PROTOCOL_FRAME_NONE);
        }

        /* A physical loss while PC ownership is unresolved is transport-only:
         * ownership remains host-side, but the CDC generation is cut off
         * synchronously before any later reconnect can deliver RX/TX. */
        tinyusb_event_t detached = { .id = TINYUSB_EVENT_DETACHED };
        g_device_cb(&detached, g_device_arg);
        CHECK(g_cdc_cutoff_calls == 1);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
        CHECK(next_event() == USB_MSC_EVENT_NONE);

        /* Reconfiguration never grants Device storage ownership. ATTACHED is
         * deliberately not allowed to trigger CDC drain/reopen because it runs
         * before the SET_CONFIGURATION status stage. */
        g_device_cb(&attached, g_device_arg);
        CHECK(g_cdc_cutoff_calls == 1);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
        CHECK(next_event() == USB_MSC_EVENT_NONE);

        /* The first completed post-reconnect MSC command proves class binding.
         * Only then may the coordinator reopen a fresh CDC application session. */
        tud_msc_scsi_complete_cb(0, test_unit_ready);
        CHECK(next_event() == USB_MSC_EVENT_HOST_REATTACHED);
        CHECK(next_event() == USB_MSC_EVENT_NONE);

        /* Model the recorder coordinator's production reattach path:
         * blocking reset/drain, then explicit fresh application-session open. */
        usb_cdc_session_gate_reset(&g_cdc_gate, NULL, NULL);
        discard_epoch =
            usb_cdc_session_gate_rx_discard_epoch(&g_cdc_gate);
        usb_cdc_session_gate_mark_rx_drained(
            &g_cdc_gate, discard_epoch);
        CHECK(usb_cdc_session_gate_open(&g_cdc_gate));
        CHECK(usb_cdc_session_gate_generation(&g_cdc_gate) !=
              stale_generation);

        /* The old request tail cannot combine with the pre-detach prefix.
         * It may yield an ordinary parse/envelope error, but SET_TIME must not
         * execute. */
        for (i = 0u; tail[i] != '\0'; ++i) {
            usb_cdc_protocol_frame_result_t result =
                usb_cdc_protocol_framer_feed(
                    &g_cdc_framer, (uint8_t)tail[i],
                    &line, &line_len);
            if (tail[i] == '\n') {
                CHECK(result == USB_CDC_PROTOCOL_FRAME_LINE);
            } else {
                CHECK(result == USB_CDC_PROTOCOL_FRAME_NONE);
            }
        }
        CHECK(usb_cdc_protocol_process_line(
                  line, line_len, response, sizeof(response),
                  &cdc_config, &effect) > 0);
        CHECK(g_set_time_calls == 0u);
        CHECK(strstr(response, "\"ok\":false") != NULL);

        /* A genuinely fresh complete request in the reconnected generation is
         * processed normally after the rejected old tail. */
        memset(response, 0, sizeof(response));
        for (i = 0u; fresh_ping[i] != '\0'; ++i) {
            usb_cdc_protocol_frame_result_t result =
                usb_cdc_protocol_framer_feed(
                    &g_cdc_framer, (uint8_t)fresh_ping[i],
                    &line, &line_len);
            if (fresh_ping[i] == '\n') {
                CHECK(result == USB_CDC_PROTOCOL_FRAME_LINE);
            } else {
                CHECK(result == USB_CDC_PROTOCOL_FRAME_NONE);
            }
        }
        CHECK(usb_cdc_protocol_process_line(
                  line, line_len, response, sizeof(response),
                  &cdc_config, &effect) > 0);
        CHECK(strstr(response, "\"pong\":true") != NULL);
        CHECK(g_set_time_calls == 0u);

        /* Duplicate configuration without a fresh DETACHED boundary is not a
         * second CDC session transition. */
        g_device_cb(&attached, g_device_arg);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
        return 0;
    }
"""


def _write_headers(root: Path) -> None:
    for rel, content in STUB_HEADERS.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")


def test_two_phase_publication_reconnects_only_after_usb_owned_lun(tmp_path: Path) -> None:
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("host C compiler is unavailable")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _write_headers(stubs)
    harness = tmp_path / "harness.c"
    harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
    binary = tmp_path / "usb_publish_gate"

    subprocess.run(
        [
            cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-include", "stdio.h",
            "-D_POSIX_C_SOURCE=200809L",
            "-DESP_PLATFORM", "-DCONFIG_TINYUSB_SUSPEND_CALLBACK=1",
            "-I", str(stubs), "-I", str(STUB_INCLUDE), "-I", str(INCLUDE),
            str(RECORDER / "usb_msc_ownership.c"),
            str(RECORDER / "shutdown_armed.c"),
            str(RECORDER / "usb_cdc_session_gate.c"),
            str(RECORDER / "usb_cdc_protocol_core.c"),
            str(REPO / "firmware/tests/native/cjson_stub.c"),
            str(harness),
            "-o", str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)


def test_usb_status_callbacks_are_not_linker_wrapped() -> None:
    cmake = MAIN_CMAKE.read_text(encoding="utf-8")
    assert "--wrap=tud_mount_cb" not in cmake
    assert "--wrap=tud_msc_start_stop_cb" not in cmake
