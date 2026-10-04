"""Task #87 Strategy 2 behavioral tests for explicit-eject USB quiescence.

The production C ownership coordinator is compiled against deterministic host
stubs. These tests exercise the linker-wrapped START STOP UNIT callback used by
esp_tinyusb 2.2.1, rather than inventing a duplicate TinyUSB callback symbol.
"""

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
        #define ESP_ERR_NO_MEM 4
        #define ESP_ERR_NOT_SUPPORTED 5
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
        bool __real_tud_msc_start_stop_cb(uint8_t lun,
                                          uint8_t power_condition,
                                          bool start,
                                          bool load_eject);
        bool __wrap_tud_msc_start_stop_cb(uint8_t lun,
                                          uint8_t power_condition,
                                          bool start,
                                          bool load_eject);
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
    "shutdown_armed.h": r"""
        #pragma once
        #include "esp_err.h"
        esp_err_t shutdown_armed_mark_host_unresolved(void);
        esp_err_t shutdown_armed_commit(void);
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

    enum {
        LIFECYCLE_NORMAL = 0,
        LIFECYCLE_ARMED = 1,
        LIFECYCLE_HOST_UNRESOLVED = 2,
    };

    struct event_group { EventBits_t bits; };
    static tinyusb_event_cb_t g_device_cb;
    static void *g_device_arg;
    static tinyusb_msc_storage_callback_t g_storage_cb;
    static void *g_storage_arg;
    static bool g_mounted = true;
    static bool g_release_requested;
    static bool g_device_fs_released;
    static int g_disconnect_calls;
    static int g_real_start_stop_calls;
    static int g_uninstall_calls;
    static int g_release_storage_calls;
    static int g_mark_unresolved_calls;
    static int g_arm_calls;
    static int g_lifecycle_state = LIFECYCLE_NORMAL;
    static esp_err_t g_uninstall_result = ESP_OK;
    static esp_err_t g_release_storage_result = ESP_OK;
    static esp_err_t g_mark_unresolved_result = ESP_OK;
    static esp_err_t g_arm_result = ESP_OK;

    EventGroupHandle_t xEventGroupCreate(void) {
        return calloc(1, sizeof(struct event_group));
    }
    void vEventGroupDelete(EventGroupHandle_t group) { free(group); }
    EventBits_t xEventGroupSetBits(EventGroupHandle_t group, EventBits_t bits) {
        group->bits |= bits; return group->bits;
    }
    EventBits_t xEventGroupClearBits(EventGroupHandle_t group, EventBits_t bits) {
        EventBits_t before = group->bits; group->bits &= ~bits; return before;
    }
    EventBits_t xEventGroupWaitBits(EventGroupHandle_t group,
                                    EventBits_t bits,
                                    BaseType_t clear_on_exit,
                                    BaseType_t wait_for_all,
                                    TickType_t ticks) {
        (void)wait_for_all; (void)ticks;
        if (bits == (1u << 1) && (group->bits & bits) == 0) {
            g_release_requested = true;
            g_device_fs_released = true;
            CHECK(usb_msc_ownership_note_prepare_complete(true, true, true) == ESP_OK);
        }
        EventBits_t result = group->bits;
        if (clear_on_exit && (result & bits) != 0) group->bits &= ~bits;
        return result;
    }

    bool sd_mount_is_mounted(void) { return g_mounted; }
    esp_err_t sd_mount_transfer_to_usb(void) {
        tinyusb_msc_event_t event = {
            .id = TINYUSB_MSC_EVENT_MOUNT_COMPLETE,
            .mount_point = TINYUSB_MSC_STORAGE_MOUNT_USB,
        };
        if (!g_release_requested || !g_device_fs_released || !g_mounted) {
            return ESP_ERR_INVALID_STATE;
        }
        CHECK(g_lifecycle_state == LIFECYCLE_HOST_UNRESOLVED);
        CHECK(g_storage_cb != NULL);
        g_storage_cb((void *)1, &event, g_storage_arg);
        return ESP_OK;
    }
    void sd_mount_note_usb_owned(void) { g_mounted = false; }
    esp_err_t sd_mount_release_usb_storage(void) {
        g_release_storage_calls++;
        if (g_release_storage_result != ESP_OK) return g_release_storage_result;
        CHECK(!g_mounted);
        return ESP_OK;
    }

    esp_err_t shutdown_armed_mark_host_unresolved(void) {
        g_mark_unresolved_calls++;
        if (g_mark_unresolved_result != ESP_OK) return g_mark_unresolved_result;
        g_lifecycle_state = LIFECYCLE_HOST_UNRESOLVED;
        return ESP_OK;
    }
    esp_err_t shutdown_armed_commit(void) {
        g_arm_calls++;
        if (g_arm_result != ESP_OK) return g_arm_result;
        g_lifecycle_state = LIFECYCLE_ARMED;
        return ESP_OK;
    }

    esp_err_t tinyusb_msc_set_storage_callback(tinyusb_msc_storage_callback_t cb,
                                                void *arg) {
        g_storage_cb = cb; g_storage_arg = arg; return ESP_OK;
    }
    esp_err_t tinyusb_driver_install(const tinyusb_config_t *config) {
        g_device_cb = config->event_cb; g_device_arg = config->event_arg; return ESP_OK;
    }
    esp_err_t tinyusb_driver_uninstall(void) {
        g_uninstall_calls++; return g_uninstall_result;
    }
    bool tud_disconnect(void) {
        g_disconnect_calls++; return true;
    }
    bool __real_tud_msc_start_stop_cb(uint8_t lun,
                                      uint8_t power_condition,
                                      bool start,
                                      bool load_eject) {
        (void)lun; (void)power_condition; (void)start; (void)load_eject;
        g_real_start_stop_calls++;
        return true;
    }

    static usb_msc_ownership_event_t next_event(void) {
        return usb_msc_ownership_wait_event(0);
    }

    static void enter_host_owned(void) {
        tinyusb_event_t attached = { .id = TINYUSB_EVENT_ATTACHED };
        CHECK(usb_msc_ownership_init() == ESP_OK);
        CHECK(usb_msc_ownership_start() == ESP_OK);
        CHECK(g_device_cb != NULL);
        g_device_cb(&attached, g_device_arg);
        CHECK(next_event() == USB_MSC_EVENT_ATTACH);
        CHECK(next_event() == USB_MSC_EVENT_HOST_OWNED);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
        CHECK(g_mark_unresolved_calls == 1);
        CHECK(g_lifecycle_state == LIFECYCLE_HOST_UNRESOLVED);
    }

    static void prove_ambiguous_events_do_not_release(void) {
        tinyusb_event_t suspended = { .id = TINYUSB_EVENT_SUSPENDED };
        tinyusb_event_t detached = { .id = TINYUSB_EVENT_DETACHED };
        g_device_cb(&suspended, g_device_arg);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
        CHECK(g_disconnect_calls == 0);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(g_lifecycle_state == LIFECYCLE_HOST_UNRESOLVED);
        g_device_cb(&detached, g_device_arg);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
        CHECK(g_disconnect_calls == 0);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(g_lifecycle_state == LIFECYCLE_HOST_UNRESOLVED);
    }

    static void request_explicit_eject(void) {
        CHECK(__wrap_tud_msc_start_stop_cb(0, 0, false, true));
        CHECK(g_real_start_stop_calls == 0);
        CHECK(g_disconnect_calls == 1);
        CHECK(next_event() == USB_MSC_EVENT_RELEASE_REQUESTED);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
        CHECK(g_lifecycle_state == LIFECYCLE_HOST_UNRESOLVED);
    }

    static void run_success(void) {
        enter_host_owned();
        prove_ambiguous_events_do_not_release();
        request_explicit_eject();
        CHECK(usb_msc_ownership_complete_release_quiesce() == ESP_OK);
        CHECK(g_uninstall_calls == 1);
        CHECK(g_release_storage_calls == 1);
        CHECK(g_arm_calls == 1);
        CHECK(g_lifecycle_state == LIFECYCLE_ARMED);
        CHECK(!usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
        CHECK(next_event() == USB_MSC_EVENT_RELEASE_QUIESCED);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
    }

    static void run_non_eject_start_stop(void) {
        enter_host_owned();
        CHECK(__wrap_tud_msc_start_stop_cb(0, 0, true, true));
        CHECK(__wrap_tud_msc_start_stop_cb(0, 0, false, false));
        CHECK(g_real_start_stop_calls == 2);
        CHECK(g_disconnect_calls == 0);
        CHECK(g_arm_calls == 0);
        CHECK(g_lifecycle_state == LIFECYCLE_HOST_UNRESOLVED);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
    }

    static void run_teardown_failure(void) {
        enter_host_owned();
        request_explicit_eject();
        g_uninstall_result = ESP_FAIL;
        CHECK(usb_msc_ownership_complete_release_quiesce() == ESP_FAIL);
        CHECK(g_release_storage_calls == 0);
        CHECK(g_arm_calls == 0);
        CHECK(g_lifecycle_state == LIFECYCLE_HOST_UNRESOLVED);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
        CHECK(next_event() == USB_MSC_EVENT_FAILED);
    }

    static void run_deferred_write_failure(void) {
        enter_host_owned();
        request_explicit_eject();
        g_release_storage_result = ESP_FAIL;
        CHECK(usb_msc_ownership_complete_release_quiesce() == ESP_FAIL);
        CHECK(g_uninstall_calls == 1);
        CHECK(g_release_storage_calls == 1);
        CHECK(g_arm_calls == 0);
        CHECK(g_lifecycle_state == LIFECYCLE_HOST_UNRESOLVED);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
        CHECK(next_event() == USB_MSC_EVENT_FAILED);
    }

    static void run_armed_commit_failure(void) {
        enter_host_owned();
        request_explicit_eject();
        g_arm_result = ESP_FAIL;
        CHECK(usb_msc_ownership_complete_release_quiesce() == ESP_FAIL);
        CHECK(g_uninstall_calls == 1);
        CHECK(g_release_storage_calls == 1);
        CHECK(g_arm_calls == 1);
        CHECK(g_lifecycle_state == LIFECYCLE_HOST_UNRESOLVED);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
        CHECK(next_event() == USB_MSC_EVENT_FAILED);
    }

    int main(int argc, char **argv) {
        CHECK(argc == 2);
        if (strcmp(argv[1], "success") == 0) run_success();
        else if (strcmp(argv[1], "non-eject") == 0) run_non_eject_start_stop();
        else if (strcmp(argv[1], "teardown") == 0) run_teardown_failure();
        else if (strcmp(argv[1], "deferred") == 0) run_deferred_write_failure();
        else if (strcmp(argv[1], "arm-failure") == 0) run_armed_commit_failure();
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
    binary = tmp_path / "usb_strategy2"
    subprocess.run(
        [
            cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-DESP_PLATFORM", "-DCONFIG_TINYUSB_SUSPEND_CALLBACK=1",
            "-I", str(stubs), "-I", str(INCLUDE),
            str(RECORDER / "usb_msc_ownership.c"), str(harness),
            "-o", str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


@pytest.mark.parametrize(
    "scenario", ["success", "non-eject", "teardown", "deferred", "arm-failure"]
)
def test_strategy2_explicit_eject_behavior(tmp_path: Path, scenario: str) -> None:
    binary = _build(tmp_path)
    subprocess.run([str(binary), scenario], check=True, capture_output=True, text=True)
