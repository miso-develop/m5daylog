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
    #include "sd_mount.h"
    #include "shutdown_armed.h"
    #include "tinyusb.h"
    #include "tinyusb_msc.h"
    #include "tusb.h"
    #include "usb_msc_ownership.h"

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

    esp_err_t nvs_flash_init(void) { return ESP_OK; }
    esp_err_t nvs_flash_deinit(void) { return ESP_OK; }
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
    static usb_msc_ownership_event_t next_event(void) {
        return usb_msc_ownership_wait_event(0);
    }

    int main(void) {
        tinyusb_event_t attached = { .id = TINYUSB_EVENT_ATTACHED };
        uint8_t inquiry[16] = {0x12u};

        CHECK(usb_msc_ownership_init() == ESP_OK);
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
        tud_msc_scsi_complete_cb(0, inquiry);
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
            "-DESP_PLATFORM", "-DCONFIG_TINYUSB_SUSPEND_CALLBACK=1",
            "-I", str(stubs), "-I", str(INCLUDE),
            str(RECORDER / "usb_msc_ownership.c"),
            str(RECORDER / "shutdown_armed.c"),
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
