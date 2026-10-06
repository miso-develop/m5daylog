"""Task #87 Strategy 2 behavioral tests for gated MSC publication and eject.

The production ownership coordinator and persistent lifecycle implementation are
compiled together against deterministic host stubs. The scenarios execute the
wrapped pre-configuration mount path, explicit START STOP UNIT eject, ambiguous
bus events, failure injection, reconnect freshness, and durable fail-closed boot
classification.
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
        g_storage_cb((void *)1, &start, g_storage_arg);
        if (!g_release_requested || !g_device_fs_released || !g_mounted) {
            return ESP_ERR_INVALID_STATE;
        }
        CHECK(g_nvs_has_value && g_nvs_value == LIFECYCLE_HOST_UNRESOLVED);
        g_storage_cb((void *)1, &complete, g_storage_arg);
        return ESP_OK;
    }
    void sd_mount_note_usb_owned(void) { g_mounted = false; }
    esp_err_t sd_mount_release_usb_storage(void) {
        g_release_storage_calls++;
        if (g_release_storage_result != ESP_OK) return g_release_storage_result;
        CHECK(!g_mounted);
        return ESP_OK;
    }

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
    esp_err_t tinyusb_driver_uninstall(void) {
        g_uninstall_calls++;
        return g_uninstall_result;
    }
    bool tud_disconnect(void) {
        g_disconnect_calls++;
        return true;
    }
    void __real_tud_mount_cb(void) {
        tinyusb_event_t attached = { .id = TINYUSB_EVENT_ATTACHED };
        g_real_mount_calls++;
        CHECK(g_device_cb != NULL);
        g_device_cb(&attached, g_device_arg);
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

    static void assert_reboot_stays_fail_closed(void) {
        shutdown_armed_boot_action_t action = SHUTDOWN_ARMED_BOOT_NORMAL;
        CHECK(g_nvs_has_value && g_nvs_value == LIFECYCLE_HOST_UNRESOLVED);
        CHECK(shutdown_armed_boot_action(false, &action) == ESP_OK);
        CHECK(action == SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN);
        CHECK(shutdown_armed_boot_action(true, &action) == ESP_OK);
        CHECK(action == SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN);
    }

    static void enter_host_owned(void) {
        CHECK(usb_msc_ownership_init() == ESP_OK);
        CHECK(usb_msc_ownership_start() == ESP_OK);
        CHECK(g_device_cb != NULL);
        CHECK(g_storage_cb != NULL);

        __wrap_tud_mount_cb();
        CHECK(g_transfer_calls == 1);
        CHECK(g_real_mount_calls == 1);
        CHECK(g_disconnect_calls == 0);
        CHECK(next_event() == USB_MSC_EVENT_ATTACH);
        CHECK(next_event() == USB_MSC_EVENT_HOST_OWNED);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
        CHECK(g_nvs_commit_calls == 1);
        CHECK(g_nvs_value == LIFECYCLE_HOST_UNRESOLVED);
    }

    static void prove_ambiguous_events_do_not_release(void) {
        tinyusb_event_t suspended = { .id = TINYUSB_EVENT_SUSPENDED };
        tinyusb_event_t detached = { .id = TINYUSB_EVENT_DETACHED };

        g_device_cb(&suspended, g_device_arg);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
        CHECK(g_disconnect_calls == 0);
        CHECK(usb_msc_ownership_is_host_owned());
        assert_reboot_stays_fail_closed();

        /* Accidental pre-eject physical detach remains unresolved. */
        g_device_cb(&detached, g_device_arg);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
        CHECK(g_disconnect_calls == 0);
        CHECK(usb_msc_ownership_is_host_owned());
        assert_reboot_stays_fail_closed();

        /* Reconfiguration reuses the existing USB-owned storage; no stale APP proof. */
        __wrap_tud_mount_cb();
        CHECK(g_transfer_calls == 1);
        CHECK(g_real_mount_calls == 2);
        CHECK(g_disconnect_calls == 0);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
    }

    static void request_explicit_eject(void) {
        CHECK(__wrap_tud_msc_start_stop_cb(0, 0, false, true));
        CHECK(g_real_start_stop_calls == 0);
        CHECK(g_disconnect_calls == 1);
        CHECK(next_event() == USB_MSC_EVENT_RELEASE_REQUESTED);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
        CHECK(g_nvs_value == LIFECYCLE_HOST_UNRESOLVED);
    }

    static void run_success(void) {
        shutdown_armed_boot_action_t action = SHUTDOWN_ARMED_BOOT_NORMAL;
        enter_host_owned();
        assert_reboot_stays_fail_closed();
        prove_ambiguous_events_do_not_release();
        request_explicit_eject();
        CHECK(usb_msc_ownership_complete_release_quiesce() == ESP_OK);
        CHECK(g_uninstall_calls == 1);
        CHECK(g_release_storage_calls == 1);
        CHECK(g_nvs_commit_calls == 2);
        CHECK(g_nvs_value == LIFECYCLE_ARMED);
        CHECK(!usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
        CHECK(next_event() == USB_MSC_EVENT_RELEASE_QUIESCED);
        CHECK(next_event() == USB_MSC_EVENT_NONE);

        CHECK(shutdown_armed_boot_action(false, &action) == ESP_OK);
        CHECK(action == SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN);
        CHECK(shutdown_armed_boot_action(true, &action) == ESP_OK);
        CHECK(action == SHUTDOWN_ARMED_BOOT_MANUAL_RESUME);
    }

    static void run_non_eject_start_stop(void) {
        enter_host_owned();
        CHECK(__wrap_tud_msc_start_stop_cb(0, 0, true, true));
        CHECK(__wrap_tud_msc_start_stop_cb(0, 0, false, false));
        CHECK(g_real_start_stop_calls == 2);
        CHECK(g_disconnect_calls == 0);
        CHECK(g_nvs_commit_calls == 1);
        assert_reboot_stays_fail_closed();
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
        CHECK(g_nvs_commit_calls == 1);
        assert_reboot_stays_fail_closed();
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
        CHECK(g_nvs_commit_calls == 1);
        assert_reboot_stays_fail_closed();
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
        CHECK(next_event() == USB_MSC_EVENT_FAILED);
    }

    static void run_armed_commit_failure(void) {
        enter_host_owned();
        request_explicit_eject();
        g_nvs_commit_result = ESP_FAIL;
        CHECK(usb_msc_ownership_complete_release_quiesce() == ESP_FAIL);
        CHECK(g_uninstall_calls == 1);
        CHECK(g_release_storage_calls == 1);
        CHECK(g_nvs_commit_calls == 2);
        CHECK(g_nvs_value == LIFECYCLE_HOST_UNRESOLVED);
        assert_reboot_stays_fail_closed();
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
            str(RECORDER / "usb_msc_ownership.c"),
            str(RECORDER / "shutdown_armed.c"),
            str(harness),
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
def test_strategy2_explicit_eject_and_reboot_behavior(tmp_path: Path, scenario: str) -> None:
    binary = _build(tmp_path)
    subprocess.run([str(binary), scenario], check=True, capture_output=True, text=True)
