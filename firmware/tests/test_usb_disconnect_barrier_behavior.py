"""Task #87 Strategy 2 behavioral tests for gated MSC publication and eject.

The production ownership coordinator and persistent lifecycle implementation are
compiled together against deterministic host stubs. The scenarios execute the
two-phase disconnect/transfer/reconnect publication path, explicit START STOP
UNIT eject, ambiguous bus events, failure injection, reconnect freshness, and
durable fail-closed boot classification.
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
        bool __wrap_tud_msc_test_unit_ready_cb(uint8_t lun);
        void __wrap_tud_msc_capacity_cb(uint8_t lun,
                                        uint32_t *block_count,
                                        uint16_t *block_size);
        int32_t __wrap_tud_msc_read10_cb(uint8_t lun, uint32_t lba,
                                         uint32_t offset, void *buffer,
                                         uint32_t bufsize);
        int32_t __wrap_tud_msc_write10_cb(uint8_t lun, uint32_t lba,
                                          uint32_t offset, uint8_t *buffer,
                                          uint32_t bufsize);
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
    static int g_connect_calls;
    static int g_transfer_calls;
    static TickType_t g_delay_ticks;
    static bool g_connect_result = true;
    static esp_err_t g_transfer_result = ESP_OK;
    static int g_uninstall_calls;
    static int g_release_storage_calls;
    static esp_err_t g_uninstall_result = ESP_OK;
    static esp_err_t g_release_storage_result = ESP_OK;
    static int g_real_tur_calls;
    static int g_real_capacity_calls;
    static int g_real_read_calls;
    static int g_real_write_calls;
    static int g_sense_calls;

    /* Transactional NVS model: failed commit preserves the last durable value. */
    static bool g_nvs_has_value;
    static uint8_t g_nvs_value;
    static bool g_nvs_pending;
    static uint8_t g_nvs_pending_value;
    static int g_nvs_commit_calls;
    static esp_err_t g_nvs_commit_result = ESP_OK;

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
        g_nvs_commit_calls++;
        if (g_nvs_commit_result != ESP_OK) return g_nvs_commit_result;
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
        if (g_transfer_result != ESP_OK) return g_transfer_result;
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
    bool tud_connect(void) {
        g_connect_calls++;
        return g_connect_result;
    }
    bool tud_msc_set_sense(uint8_t lun, uint8_t sense_key,
                           uint8_t add_sense_code,
                           uint8_t add_sense_qualifier) {
        (void)lun;
        (void)sense_key;
        (void)add_sense_code;
        (void)add_sense_qualifier;
        g_sense_calls++;
        return true;
    }
    bool __real_tud_msc_test_unit_ready_cb(uint8_t lun) {
        (void)lun;
        g_real_tur_calls++;
        return true;
    }
    void __real_tud_msc_capacity_cb(uint8_t lun,
                                    uint32_t *block_count,
                                    uint16_t *block_size) {
        (void)lun;
        g_real_capacity_calls++;
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
        g_real_read_calls++;
        return (int32_t)bufsize;
    }
    int32_t __real_tud_msc_write10_cb(uint8_t lun, uint32_t lba,
                                      uint32_t offset, uint8_t *buffer,
                                      uint32_t bufsize) {
        (void)lun;
        (void)lba;
        (void)offset;
        (void)buffer;
        g_real_write_calls++;
        return (int32_t)bufsize;
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
        tinyusb_event_t attached = { .id = TINYUSB_EVENT_ATTACHED };
        uint8_t test_unit_ready[16] = {0};

        CHECK(usb_msc_ownership_init() == ESP_OK);
        CHECK(usb_msc_ownership_start() == ESP_OK);
        CHECK(g_device_cb != NULL);
        CHECK(g_storage_cb != NULL);

        g_device_cb(&attached, g_device_arg);
        CHECK(g_disconnect_calls == 0);
        CHECK(g_connect_calls == 0);
        CHECK(g_transfer_calls == 0);
        CHECK(next_event() == USB_MSC_EVENT_NONE);

        tud_msc_scsi_complete_cb(0, test_unit_ready);
        CHECK(next_event() == USB_MSC_EVENT_ATTACH);
        CHECK(usb_msc_ownership_begin_prepare() == ESP_OK);
        CHECK(g_disconnect_calls == 1);

        g_release_requested = true;
        g_device_fs_released = true;
        CHECK(usb_msc_ownership_note_prepare_complete(true, true, true) == ESP_OK);
        CHECK(g_transfer_calls == 1);
        CHECK(g_delay_ticks == 1000u);
        CHECK(g_connect_calls == 1);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
        CHECK(!usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
        CHECK(g_nvs_commit_calls == 1);
        CHECK(g_nvs_value == LIFECYCLE_HOST_UNRESOLVED);

        g_device_cb(&attached, g_device_arg);
        CHECK(g_disconnect_calls == 1);
        CHECK(g_connect_calls == 1);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(next_event() == USB_MSC_EVENT_HOST_OWNED);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
    }

    static void prove_ambiguous_events_do_not_release(void) {
        tinyusb_event_t suspended = { .id = TINYUSB_EVENT_SUSPENDED };
        tinyusb_event_t detached = { .id = TINYUSB_EVENT_DETACHED };
        tinyusb_event_t attached = { .id = TINYUSB_EVENT_ATTACHED };

        g_device_cb(&suspended, g_device_arg);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
        CHECK(g_disconnect_calls == 1);
        CHECK(usb_msc_ownership_is_host_owned());
        assert_reboot_stays_fail_closed();

        g_device_cb(&detached, g_device_arg);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
        CHECK(g_disconnect_calls == 1);
        CHECK(usb_msc_ownership_is_host_owned());
        assert_reboot_stays_fail_closed();

        g_device_cb(&attached, g_device_arg);
        CHECK(g_transfer_calls == 1);
        CHECK(g_connect_calls == 1);
        CHECK(g_disconnect_calls == 1);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
    }

    static void request_explicit_eject(void) {
        uint8_t eject[16] = {0};
        eject[0] = 0x1bu;
        eject[4] = 0x02u; /* LOEJ=1, START=0 */
        tud_msc_scsi_complete_cb(0, eject);
        CHECK(g_disconnect_calls == 1);
        CHECK(next_event() == USB_MSC_EVENT_RELEASE_REQUESTED);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
        CHECK(g_nvs_value == LIFECYCLE_HOST_UNRESOLVED);
    }

    static void run_post_eject_io_gate(void) {
        uint8_t buffer[512] = {0};
        uint32_t block_count = 0;
        uint16_t block_size = 0;
        int tur_before;
        int capacity_before;
        int read_before;
        int write_before;

        enter_host_owned();

        /* Before release authorization, wrappers are transparent. */
        CHECK(__wrap_tud_msc_test_unit_ready_cb(0));
        __wrap_tud_msc_capacity_cb(0, &block_count, &block_size);
        CHECK(block_count == 1024u);
        CHECK(block_size == 512u);
        CHECK(__wrap_tud_msc_read10_cb(0, 1u, 0u, buffer,
                                       sizeof(buffer)) == (int32_t)sizeof(buffer));
        CHECK(__wrap_tud_msc_write10_cb(0, 1u, 0u, buffer,
                                        sizeof(buffer)) == (int32_t)sizeof(buffer));
        CHECK(g_real_tur_calls == 1);
        CHECK(g_real_capacity_calls == 1);
        CHECK(g_real_read_calls == 1);
        CHECK(g_real_write_calls == 1);
        CHECK(g_sense_calls == 0);

        request_explicit_eject();
        CHECK(g_uninstall_calls == 0);
        CHECK(g_release_storage_calls == 0);

        tur_before = g_real_tur_calls;
        capacity_before = g_real_capacity_calls;
        read_before = g_real_read_calls;
        write_before = g_real_write_calls;
        block_count = 99u;
        block_size = 99u;

        /* The coordinator has not run teardown yet. Nevertheless, no newly
         * admitted media or block-I/O command may reach esp_tinyusb storage. */
        CHECK(!__wrap_tud_msc_test_unit_ready_cb(0));
        __wrap_tud_msc_capacity_cb(0, &block_count, &block_size);
        CHECK(block_count == 0u);
        CHECK(block_size == 0u);
        CHECK(__wrap_tud_msc_read10_cb(0, 2u, 0u, buffer,
                                       sizeof(buffer)) == -1);
        CHECK(__wrap_tud_msc_write10_cb(0, 2u, 0u, buffer,
                                        sizeof(buffer)) == -1);
        CHECK(g_real_tur_calls == tur_before);
        CHECK(g_real_capacity_calls == capacity_before);
        CHECK(g_real_read_calls == read_before);
        CHECK(g_real_write_calls == write_before);
        CHECK(g_sense_calls == 4);
        CHECK(g_disconnect_calls == 1);
        CHECK(g_uninstall_calls == 0);
        CHECK(g_release_storage_calls == 0);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
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
        uint8_t start_cmd[16] = {0};
        uint8_t other_cmd[16] = {0};
        enter_host_owned();
        start_cmd[0] = 0x1bu;
        start_cmd[4] = 0x03u; /* LOEJ=1, START=1 */
        other_cmd[0] = 0x00u; /* TEST UNIT READY while host-owned */
        tud_msc_scsi_complete_cb(0, start_cmd);
        tud_msc_scsi_complete_cb(0, other_cmd);
        CHECK(g_disconnect_calls == 1);
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

    static void begin_failed_publication_case(void) {
        tinyusb_event_t attached = { .id = TINYUSB_EVENT_ATTACHED };
        uint8_t test_unit_ready[16] = {0};

        CHECK(usb_msc_ownership_init() == ESP_OK);
        CHECK(usb_msc_ownership_start() == ESP_OK);
        g_device_cb(&attached, g_device_arg);
        CHECK(g_disconnect_calls == 0);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
        tud_msc_scsi_complete_cb(0, test_unit_ready);
        CHECK(next_event() == USB_MSC_EVENT_ATTACH);
        CHECK(usb_msc_ownership_begin_prepare() == ESP_OK);
        CHECK(g_disconnect_calls == 1);
        g_release_requested = true;
        g_device_fs_released = true;
    }

    static void run_publication_persist_failure(void) {
        begin_failed_publication_case();
        g_nvs_commit_result = ESP_FAIL;
        CHECK(usb_msc_ownership_note_prepare_complete(true, true, true) ==
              ESP_ERR_INVALID_STATE);
        CHECK(g_nvs_commit_calls == 1);
        CHECK(!g_nvs_has_value);
        CHECK(g_transfer_calls == 0);
        CHECK(g_connect_calls == 0);
        CHECK(g_mounted);
        CHECK(!usb_msc_ownership_is_host_owned());
        CHECK(next_event() == USB_MSC_EVENT_FAILED);
    }

    static void run_transfer_failure(void) {
        begin_failed_publication_case();
        g_transfer_result = ESP_FAIL;
        CHECK(usb_msc_ownership_note_prepare_complete(true, true, true) ==
              ESP_ERR_INVALID_STATE);
        CHECK(g_nvs_has_value);
        CHECK(g_nvs_value == LIFECYCLE_HOST_UNRESOLVED);
        CHECK(g_transfer_calls == 1);
        CHECK(g_connect_calls == 0);
        CHECK(g_mounted);
        CHECK(!usb_msc_ownership_is_host_owned());
        CHECK(next_event() == USB_MSC_EVENT_FAILED);
        assert_reboot_stays_fail_closed();
    }

    static void run_reconnect_failure(void) {
        begin_failed_publication_case();
        g_connect_result = false;
        CHECK(usb_msc_ownership_note_prepare_complete(true, true, true) ==
              ESP_ERR_INVALID_STATE);
        CHECK(g_nvs_has_value);
        CHECK(g_nvs_value == LIFECYCLE_HOST_UNRESOLVED);
        CHECK(g_transfer_calls == 1);
        CHECK(g_connect_calls == 1);
        CHECK(!g_mounted);
        CHECK(!usb_msc_ownership_is_host_owned());
        CHECK(next_event() == USB_MSC_EVENT_FAILED);
        assert_reboot_stays_fail_closed();
    }

    int main(int argc, char **argv) {
        CHECK(argc == 2);
        if (strcmp(argv[1], "success") == 0) run_success();
        else if (strcmp(argv[1], "non-eject") == 0) run_non_eject_start_stop();
        else if (strcmp(argv[1], "teardown") == 0) run_teardown_failure();
        else if (strcmp(argv[1], "deferred") == 0) run_deferred_write_failure();
        else if (strcmp(argv[1], "arm-failure") == 0) run_armed_commit_failure();
        else if (strcmp(argv[1], "publish-persist-failure") == 0)
            run_publication_persist_failure();
        else if (strcmp(argv[1], "transfer-failure") == 0)
            run_transfer_failure();
        else if (strcmp(argv[1], "reconnect-failure") == 0)
            run_reconnect_failure();
        else if (strcmp(argv[1], "post-eject-io-gate") == 0)
            run_post_eject_io_gate();
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
    "scenario",
    [
        "success",
        "non-eject",
        "teardown",
        "deferred",
        "arm-failure",
        "publish-persist-failure",
        "transfer-failure",
        "reconnect-failure",
        "post-eject-io-gate",
    ],
)
def test_strategy2_explicit_eject_and_reboot_behavior(tmp_path: Path, scenario: str) -> None:
    binary = _build(tmp_path)
    subprocess.run([str(binary), scenario], check=True, capture_output=True, text=True)
