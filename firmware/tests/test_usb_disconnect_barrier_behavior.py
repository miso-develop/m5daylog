"""Task #87 behavioral host tests for USB/SD ownership transitions.

These tests compile and execute the production C sources against deterministic
host stubs. Source-text tests remain supplemental; this file drives the actual
state transitions and injected failure returns required by AC-10.
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
            (void)tag;
            (void)fmt;
        }
        #define ESP_LOGI(...) test_esp_log(__VA_ARGS__)
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
        typedef int portMUX_TYPE;
        #define pdTRUE 1
        #define pdFALSE 0
        #define portMAX_DELAY UINT32_MAX
        #define pdMS_TO_TICKS(ms) ((TickType_t)(ms))
        #define portMUX_INITIALIZER_UNLOCKED 0
        #define portENTER_CRITICAL(lock) ((void)(lock))
        #define portEXIT_CRITICAL(lock) ((void)(lock))
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
        bool tud_disconnect(void);
    """,
    "tinyusb_msc.h": r"""
        #pragma once
        #include <stdbool.h>
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
            tinyusb_msc_storage_handle_t handle,
            tinyusb_msc_event_t *event,
            void *arg);

        typedef struct {
            bool format_if_mount_failed;
            int max_files;
            int allocation_unit_size;
        } esp_vfs_fat_mount_config_t;
        typedef struct {
            struct { void *card; } medium;
            struct {
                const char *base_path;
                esp_vfs_fat_mount_config_t config;
                bool do_not_format;
            } fat_fs;
            tinyusb_msc_mount_point_t mount_point;
        } tinyusb_msc_storage_config_t;
        typedef struct {
            struct { int auto_mount_off; } user_flags;
            tinyusb_msc_storage_callback_t callback;
            void *callback_arg;
        } tinyusb_msc_driver_config_t;

        esp_err_t tinyusb_msc_install_driver(const tinyusb_msc_driver_config_t *config);
        esp_err_t tinyusb_msc_uninstall_driver(void);
        esp_err_t tinyusb_msc_set_storage_callback(tinyusb_msc_storage_callback_t callback,
                                                    void *arg);
        esp_err_t tinyusb_msc_new_storage_sdmmc(const tinyusb_msc_storage_config_t *config,
                                                tinyusb_msc_storage_handle_t *handle);
        esp_err_t tinyusb_msc_delete_storage(tinyusb_msc_storage_handle_t handle);
        esp_err_t tinyusb_msc_set_storage_mount_point(tinyusb_msc_storage_handle_t handle,
                                                       tinyusb_msc_mount_point_t mount_point);
    """,
    "sdmmc_cmd.h": r"""
        #pragma once
        #include "esp_err.h"
        typedef struct { int placeholder; } sdmmc_card_t;
        typedef struct { int slot; } sdmmc_host_t;
        esp_err_t sdmmc_card_init(const sdmmc_host_t *host, sdmmc_card_t *card);
    """,
    "driver/sdspi_host.h": r"""
        #pragma once
        #include "esp_err.h"
        #include "sdmmc_cmd.h"
        typedef int sdspi_dev_handle_t;
        typedef struct { int host_id; int gpio_cs; } sdspi_device_config_t;
        #define SDSPI_DEFAULT_HOST 0
        #define SDSPI_HOST_DEFAULT() ((sdmmc_host_t){ .slot = 2 })
        #define SDSPI_DEVICE_CONFIG_DEFAULT() ((sdspi_device_config_t){0})
        esp_err_t sdspi_host_init(void);
        esp_err_t sdspi_host_deinit(void);
        esp_err_t sdspi_host_init_device(const sdspi_device_config_t *config,
                                         sdspi_dev_handle_t *handle);
        esp_err_t sdspi_host_remove_device(sdspi_dev_handle_t handle);
    """,
    "driver/spi_common.h": r"""
        #pragma once
        #include "esp_err.h"
        typedef int spi_host_device_t;
        typedef struct {
            int mosi_io_num;
            int miso_io_num;
            int sclk_io_num;
            int quadwp_io_num;
            int quadhd_io_num;
            int max_transfer_sz;
        } spi_bus_config_t;
        #define SPI2_HOST 2
        #define SDSPI_DEFAULT_DMA 1
        esp_err_t spi_bus_initialize(spi_host_device_t host,
                                     const spi_bus_config_t *config,
                                     int dma);
        esp_err_t spi_bus_free(spi_host_device_t host);
    """,
    "recorder_config.h": r"""
        #pragma once
        #define RECORDER_M5DAYLOG_DIR "/tmp"
        #define RECORDER_RECORDINGS_DIR "/tmp"
        #define RECORDER_QUARANTINE_DIR "/tmp"
        #define RECORDER_SD_MOUNT_POINT "/tmp"
        #define RECORDER_MAX_PATH_LEN 128
        #define RECORDER_SD_MOSI_PIN 1
        #define RECORDER_SD_MISO_PIN 2
        #define RECORDER_SD_CLK_PIN 3
        #define RECORDER_SD_CS_PIN 4
    """,
}


OWNERSHIP_HARNESS = r"""
    #include <stdbool.h>
    #include <stdint.h>
    #include <stdio.h>
    #include <stdlib.h>
    #include <string.h>

    #include "freertos/FreeRTOS.h"
    #include "freertos/event_groups.h"
    #include "sd_mount.h"
    #include "tinyusb.h"
    #include "tinyusb_msc.h"
    #include "usb_msc_ownership.h"

    #define CHECK(expr) do { \
        if (!(expr)) { \
            fprintf(stderr, "CHECK failed %s:%d: %s\n", __FILE__, __LINE__, #expr); \
            exit(2); \
        } \
    } while (0)

    struct event_group { EventBits_t bits; };
    static tinyusb_event_cb_t g_device_cb;
    static void *g_device_arg;
    static tinyusb_msc_storage_callback_t g_storage_cb;
    static void *g_storage_arg;
    static bool g_mounted = true;
    static bool g_release_requested;
    static bool g_device_fs_released;
    static bool g_auto_prepare = true;
    static int g_prepare_bit_was_set = -1;
    static esp_err_t g_uninstall_result = ESP_OK;
    static esp_err_t g_transfer_app_result = ESP_OK;
    static int g_uninstall_calls;
    static int g_transfer_app_calls;
    static int g_disconnect_calls;

    EventGroupHandle_t xEventGroupCreate(void) {
        return calloc(1, sizeof(struct event_group));
    }
    void vEventGroupDelete(EventGroupHandle_t group) { free(group); }
    EventBits_t xEventGroupSetBits(EventGroupHandle_t group, EventBits_t bits) {
        group->bits |= bits;
        return group->bits;
    }
    EventBits_t xEventGroupClearBits(EventGroupHandle_t group, EventBits_t bits) {
        EventBits_t previous = group->bits;
        group->bits &= ~bits;
        return previous;
    }
    EventBits_t xEventGroupWaitBits(EventGroupHandle_t group,
                                    EventBits_t bits,
                                    BaseType_t clear_on_exit,
                                    BaseType_t wait_for_all,
                                    TickType_t ticks) {
        (void)wait_for_all;
        (void)ticks;
        if (bits == (1u << 1)) {
            g_prepare_bit_was_set = (group->bits & bits) != 0;
            if (g_auto_prepare && !g_prepare_bit_was_set) {
                g_release_requested = true;
                g_device_fs_released = true;
                CHECK(usb_msc_ownership_note_prepare_complete(true, true, true) == ESP_OK);
            }
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
        CHECK(g_storage_cb != NULL);
        g_storage_cb((void *)1, &event, g_storage_arg);
        return ESP_OK;
    }
    esp_err_t sd_mount_transfer_to_app(void) {
        tinyusb_msc_event_t event = {
            .id = TINYUSB_MSC_EVENT_MOUNT_COMPLETE,
            .mount_point = TINYUSB_MSC_STORAGE_MOUNT_APP,
        };
        g_transfer_app_calls++;
        if (g_transfer_app_result != ESP_OK) return g_transfer_app_result;
        CHECK(g_storage_cb != NULL);
        g_storage_cb((void *)1, &event, g_storage_arg);
        return ESP_OK;
    }
    void sd_mount_note_usb_owned(void) { g_mounted = false; }
    void sd_mount_note_app_owned(void) {
        g_mounted = true;
        g_release_requested = false;
        g_device_fs_released = false;
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

    static usb_msc_ownership_event_t next_event(void) {
        return usb_msc_ownership_wait_event(0);
    }
    static void enter_host_owned(void) {
        tinyusb_event_t attached = { .id = TINYUSB_EVENT_ATTACHED };
        CHECK(usb_msc_ownership_init() == ESP_OK);
        CHECK(usb_msc_ownership_start() == ESP_OK);
        CHECK(g_device_cb != NULL);
        g_prepare_bit_was_set = -1;
        g_device_cb(&attached, g_device_arg);
        CHECK(g_prepare_bit_was_set == 0);
        CHECK(next_event() == USB_MSC_EVENT_ATTACH);
        CHECK(next_event() == USB_MSC_EVENT_HOST_OWNED);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
    }
    static void start_barrier(void) {
        tinyusb_event_t suspended = { .id = TINYUSB_EVENT_SUSPENDED };
        g_device_cb(&suspended, g_device_arg);
        CHECK(g_disconnect_calls == 1);
        CHECK(next_event() == USB_MSC_EVENT_BARRIER_REQUIRED);
        CHECK(usb_msc_ownership_rearm() == ESP_ERR_INVALID_STATE);
    }
    static void assert_failed_closed(void) {
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!g_mounted);
        CHECK(next_event() == USB_MSC_EVENT_FAILED);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
        CHECK(usb_msc_ownership_rearm() == ESP_ERR_INVALID_STATE);
    }

    static void run_success(void) {
        tinyusb_event_t attached = { .id = TINYUSB_EVENT_ATTACHED };
        enter_host_owned();
        start_barrier();
        CHECK(usb_msc_ownership_complete_disconnect_barrier() == ESP_OK);
        CHECK(g_uninstall_calls == 1);
        CHECK(g_transfer_app_calls == 1);
        CHECK(!usb_msc_ownership_is_host_owned());
        CHECK(g_mounted);
        CHECK(next_event() == USB_MSC_EVENT_DETACH);
        CHECK(next_event() == USB_MSC_EVENT_NONE);
        CHECK(usb_msc_ownership_rearm() == ESP_ERR_INVALID_STATE);
        CHECK(usb_msc_ownership_note_recording_recovered() == ESP_OK);
        CHECK(usb_msc_ownership_rearm() == ESP_OK);
        CHECK(next_event() == USB_MSC_EVENT_NONE);

        g_prepare_bit_was_set = -1;
        g_device_cb(&attached, g_device_arg);
        CHECK(g_prepare_bit_was_set == 0);
        CHECK(next_event() == USB_MSC_EVENT_ATTACH);
        CHECK(next_event() == USB_MSC_EVENT_HOST_OWNED);
    }
    static void run_teardown_failure(void) {
        enter_host_owned();
        start_barrier();
        g_uninstall_result = ESP_FAIL;
        CHECK(usb_msc_ownership_complete_disconnect_barrier() == ESP_FAIL);
        CHECK(g_uninstall_calls == 1);
        CHECK(g_transfer_app_calls == 0);
        assert_failed_closed();
    }
    static void run_app_transfer_failure(void) {
        enter_host_owned();
        start_barrier();
        g_transfer_app_result = ESP_FAIL;
        CHECK(usb_msc_ownership_complete_disconnect_barrier() == ESP_FAIL);
        CHECK(g_uninstall_calls == 1);
        CHECK(g_transfer_app_calls == 1);
        assert_failed_closed();
    }

    int main(int argc, char **argv) {
        CHECK(argc == 2);
        if (strcmp(argv[1], "success") == 0) run_success();
        else if (strcmp(argv[1], "teardown") == 0) run_teardown_failure();
        else if (strcmp(argv[1], "app-transfer") == 0) run_app_transfer_failure();
        else CHECK(false);
        return 0;
    }
"""


SD_HARNESS = r"""
    #include <stdbool.h>
    #include <stdio.h>
    #include <stdlib.h>
    #include <string.h>

    #include "driver/sdspi_host.h"
    #include "driver/spi_common.h"
    #include "sd_mount.h"
    #include "sdmmc_cmd.h"
    #include "tinyusb_msc.h"

    #define CHECK(expr) do { \
        if (!(expr)) { \
            fprintf(stderr, "CHECK failed %s:%d: %s\n", __FILE__, __LINE__, #expr); \
            exit(2); \
        } \
    } while (0)

    static int g_storage_token;
    static int g_new_storage_calls;
    static int g_delete_calls;
    static bool g_delete_fail;
    static bool g_rebuild_fail;

    esp_err_t spi_bus_initialize(spi_host_device_t host,
                                 const spi_bus_config_t *config,
                                 int dma) {
        (void)host; (void)config; (void)dma; return ESP_OK;
    }
    esp_err_t spi_bus_free(spi_host_device_t host) { (void)host; return ESP_OK; }
    esp_err_t sdspi_host_init(void) { return ESP_OK; }
    esp_err_t sdspi_host_deinit(void) { return ESP_OK; }
    esp_err_t sdspi_host_init_device(const sdspi_device_config_t *config,
                                     sdspi_dev_handle_t *handle) {
        (void)config; *handle = 7; return ESP_OK;
    }
    esp_err_t sdspi_host_remove_device(sdspi_dev_handle_t handle) {
        (void)handle; return ESP_OK;
    }
    esp_err_t sdmmc_card_init(const sdmmc_host_t *host, sdmmc_card_t *card) {
        (void)host; (void)card; return ESP_OK;
    }

    esp_err_t tinyusb_msc_install_driver(const tinyusb_msc_driver_config_t *config) {
        (void)config; return ESP_OK;
    }
    esp_err_t tinyusb_msc_uninstall_driver(void) { return ESP_OK; }
    esp_err_t tinyusb_msc_set_storage_callback(tinyusb_msc_storage_callback_t cb,
                                                void *arg) {
        (void)cb; (void)arg; return ESP_OK;
    }
    esp_err_t tinyusb_msc_new_storage_sdmmc(const tinyusb_msc_storage_config_t *config,
                                            tinyusb_msc_storage_handle_t *handle) {
        (void)config;
        g_new_storage_calls++;
        if (g_rebuild_fail && g_new_storage_calls >= 2) return ESP_FAIL;
        *handle = &g_storage_token;
        if (g_new_storage_calls >= 2) sd_mount_note_app_owned();
        return ESP_OK;
    }
    esp_err_t tinyusb_msc_delete_storage(tinyusb_msc_storage_handle_t handle) {
        (void)handle;
        g_delete_calls++;
        return g_delete_fail ? ESP_FAIL : ESP_OK;
    }
    esp_err_t tinyusb_msc_set_storage_mount_point(tinyusb_msc_storage_handle_t handle,
                                                   tinyusb_msc_mount_point_t point) {
        (void)handle; (void)point; return ESP_OK;
    }

    static void enter_usb_owned(void) {
        CHECK(sd_mount_recordings() == ESP_OK);
        CHECK(sd_mount_is_mounted());
        CHECK(sd_mount_release_for_usb() == ESP_OK);
        sd_mount_unmount();
        CHECK(sd_mount_device_fs_released());
        sd_mount_note_usb_owned();
        CHECK(!sd_mount_is_mounted());
    }
    static void run_success(void) {
        enter_usb_owned();
        CHECK(sd_mount_transfer_to_app() == ESP_OK);
        CHECK(g_delete_calls == 1);
        CHECK(g_new_storage_calls == 2);
        CHECK(sd_mount_is_mounted());
    }
    static void run_deferred_write_failure(void) {
        enter_usb_owned();
        g_delete_fail = true;
        CHECK(sd_mount_transfer_to_app() == ESP_FAIL);
        CHECK(g_delete_calls == 1);
        CHECK(g_new_storage_calls == 1);
        CHECK(!sd_mount_is_mounted());
    }
    static void run_app_rebuild_failure(void) {
        enter_usb_owned();
        g_rebuild_fail = true;
        CHECK(sd_mount_transfer_to_app() == ESP_FAIL);
        CHECK(g_delete_calls == 1);
        CHECK(g_new_storage_calls == 2);
        CHECK(!sd_mount_is_mounted());
    }

    int main(int argc, char **argv) {
        CHECK(argc == 2);
        if (strcmp(argv[1], "success") == 0) run_success();
        else if (strcmp(argv[1], "deferred-write") == 0) run_deferred_write_failure();
        else if (strcmp(argv[1], "app-rebuild") == 0) run_app_rebuild_failure();
        else CHECK(false);
        return 0;
    }
"""


def _write_stubs(root: Path) -> None:
    for relative, content in STUB_HEADERS.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def _compile(compiler: str, stubs: Path, source: Path, harness: Path, output: Path, *defines: str) -> None:
    command = [
        compiler,
        "-std=c11",
        "-Wall",
        "-Wextra",
        "-Werror",
        *defines,
        "-I",
        str(stubs),
        "-I",
        str(INCLUDE),
        str(source),
        str(harness),
        "-o",
        str(output),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    assert result.returncode == 0, (
        f"compile failed: {' '.join(command)}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.fixture(scope="module")
def behavioral_harnesses(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    build = tmp_path_factory.mktemp("usb-barrier-behavior")
    stubs = build / "stubs"
    _write_stubs(stubs)
    compiler = shutil.which("cc") or shutil.which("gcc")
    assert compiler is not None, "Firmware behavioral tests require a C compiler"

    ownership_c = build / "ownership_harness.c"
    ownership_c.write_text(textwrap.dedent(OWNERSHIP_HARNESS).lstrip(), encoding="utf-8")
    ownership_bin = build / "ownership_harness"
    _compile(
        compiler,
        stubs,
        RECORDER / "usb_msc_ownership.c",
        ownership_c,
        ownership_bin,
        "-DESP_PLATFORM=1",
        "-DCONFIG_TINYUSB_SUSPEND_CALLBACK=1",
    )

    sd_c = build / "sd_harness.c"
    sd_c.write_text(textwrap.dedent(SD_HARNESS).lstrip(), encoding="utf-8")
    sd_bin = build / "sd_harness"
    _compile(
        compiler,
        stubs,
        RECORDER / "sd_mount.c",
        sd_c,
        sd_bin,
        "-DESP_PLATFORM=1",
    )
    return {"ownership": ownership_bin, "sd": sd_bin}


def _run(binary: Path, scenario: str) -> None:
    result = subprocess.run([str(binary), scenario], text=True, capture_output=True, check=False)
    assert result.returncode == 0, (
        f"scenario={scenario}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.mark.parametrize("scenario", ["success", "teardown", "app-transfer"])
def test_usb_ownership_state_machine_executes_barrier_paths(
    behavioral_harnesses: dict[str, Path], scenario: str
) -> None:
    _run(behavioral_harnesses["ownership"], scenario)


@pytest.mark.parametrize("scenario", ["success", "deferred-write", "app-rebuild"])
def test_sd_transfer_to_app_executes_deferred_write_and_rebuild_paths(
    behavioral_harnesses: dict[str, Path], scenario: str
) -> None:
    _run(behavioral_harnesses["sd"], scenario)
