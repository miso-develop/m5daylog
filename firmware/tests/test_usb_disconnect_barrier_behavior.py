"""Task #87 behavioral host tests for the USB ownership state machine.

Unlike the source-contract tests, this suite compiles and executes the production
usb_msc_ownership.c and sd_mount.c against deterministic host stubs.  The stubs
model only ESP-IDF/TinyUSB primitives; ownership state and failure handling stay
inside the production sources under test.
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
        #define ESP_LOGI(tag, fmt, ...) do { (void)(tag); (void)sizeof(fmt); } while (0)
        #define ESP_LOGE(tag, fmt, ...) do { (void)(tag); (void)sizeof(fmt); } while (0)
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
        typedef struct {
            tinyusb_event_id_t id;
        } tinyusb_event_t;
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
        #include <stddef.h>
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
        typedef struct {
            int host_id;
            int gpio_cs;
        } sdspi_device_config_t;
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


HARNESS = r"""
    #include <pthread.h>
    #include <sched.h>
    #include <stdatomic.h>
    #include <stdbool.h>
    #include <stdint.h>
    #include <stdio.h>
    #include <stdlib.h>
    #include <string.h>
    #include <time.h>

    #include "freertos/FreeRTOS.h"
    #include "freertos/event_groups.h"
    #include "sd_mount.h"
    #include "tinyusb.h"
    #include "tinyusb_msc.h"
    #include "usb_msc_ownership.h"

    #define CHECK(expr) do { \
        if (!(expr)) { \
            fprintf(stderr, "CHECK failed at %s:%d: %s\n", __FILE__, __LINE__, #expr); \
            exit(2); \
        } \
    } while (0)

    struct event_group {
        pthread_mutex_t mutex;
        pthread_cond_t cond;
        EventBits_t bits;
    };

    static tinyusb_event_cb_t g_device_cb;
    static void *g_device_arg;
    static tinyusb_msc_storage_callback_t g_storage_cb;
    static void *g_storage_arg;
    static int g_storage_token;
    static esp_err_t g_uninstall_result = ESP_OK;
    static esp_err_t g_delete_result = ESP_OK;
    static esp_err_t g_new_storage_result = ESP_OK;
    static bool g_disconnect_result = true;
    static int g_driver_install_calls;
    static int g_driver_uninstall_calls;
    static int g_delete_calls;
    static int g_new_storage_calls;
    static int g_disconnect_calls;
    static int g_sequence[16];
    static int g_sequence_len;
    static atomic_int g_attach_done;

    enum {
        STEP_UNINSTALL = 1,
        STEP_DELETE_STORAGE = 2,
        STEP_NEW_STORAGE = 3,
    };

    static void record_step(int step) {
        CHECK(g_sequence_len < (int)(sizeof(g_sequence) / sizeof(g_sequence[0])));
        g_sequence[g_sequence_len++] = step;
    }

    static void sleep_ms(long ms) {
        struct timespec ts = {
            .tv_sec = ms / 1000,
            .tv_nsec = (ms % 1000) * 1000000L,
        };
        (void)nanosleep(&ts, NULL);
    }

    EventGroupHandle_t xEventGroupCreate(void) {
        struct event_group *group = calloc(1, sizeof(*group));
        CHECK(group != NULL);
        CHECK(pthread_mutex_init(&group->mutex, NULL) == 0);
        CHECK(pthread_cond_init(&group->cond, NULL) == 0);
        return group;
    }

    void vEventGroupDelete(EventGroupHandle_t group) {
        if (group == NULL) return;
        (void)pthread_cond_destroy(&group->cond);
        (void)pthread_mutex_destroy(&group->mutex);
        free(group);
    }

    EventBits_t xEventGroupSetBits(EventGroupHandle_t group, EventBits_t bits) {
        EventBits_t result;
        CHECK(group != NULL);
        CHECK(pthread_mutex_lock(&group->mutex) == 0);
        group->bits |= bits;
        result = group->bits;
        CHECK(pthread_cond_broadcast(&group->cond) == 0);
        CHECK(pthread_mutex_unlock(&group->mutex) == 0);
        return result;
    }

    EventBits_t xEventGroupClearBits(EventGroupHandle_t group, EventBits_t bits) {
        EventBits_t previous;
        CHECK(group != NULL);
        CHECK(pthread_mutex_lock(&group->mutex) == 0);
        previous = group->bits;
        group->bits &= ~bits;
        CHECK(pthread_mutex_unlock(&group->mutex) == 0);
        return previous;
    }

    EventBits_t xEventGroupWaitBits(EventGroupHandle_t group,
                                    EventBits_t bits,
                                    BaseType_t clear_on_exit,
                                    BaseType_t wait_for_all,
                                    TickType_t ticks) {
        EventBits_t result;
        bool ready;
        CHECK(group != NULL);
        CHECK(pthread_mutex_lock(&group->mutex) == 0);
        for (;;) {
            EventBits_t matched = group->bits & bits;
            ready = wait_for_all ? matched == bits : matched != 0;
            if (ready || ticks != portMAX_DELAY) break;
            CHECK(pthread_cond_wait(&group->cond, &group->mutex) == 0);
        }
        result = group->bits;
        if (ready && clear_on_exit) group->bits &= ~bits;
        CHECK(pthread_mutex_unlock(&group->mutex) == 0);
        return result;
    }

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
        g_storage_cb = config->callback;
        g_storage_arg = config->callback_arg;
        return ESP_OK;
    }
    esp_err_t tinyusb_msc_uninstall_driver(void) { return ESP_OK; }
    esp_err_t tinyusb_msc_set_storage_callback(tinyusb_msc_storage_callback_t callback,
                                                void *arg) {
        g_storage_cb = callback;
        g_storage_arg = arg;
        return ESP_OK;
    }
    esp_err_t tinyusb_msc_new_storage_sdmmc(const tinyusb_msc_storage_config_t *config,
                                            tinyusb_msc_storage_handle_t *handle) {
        tinyusb_msc_event_t event;
        g_new_storage_calls++;
        record_step(STEP_NEW_STORAGE);
        if (g_new_storage_result != ESP_OK) return g_new_storage_result;
        *handle = &g_storage_token;
        if (g_storage_cb != NULL) {
            event.id = TINYUSB_MSC_EVENT_MOUNT_COMPLETE;
            event.mount_point = config->mount_point;
            g_storage_cb(*handle, &event, g_storage_arg);
        }
        return ESP_OK;
    }
    esp_err_t tinyusb_msc_delete_storage(tinyusb_msc_storage_handle_t handle) {
        (void)handle;
        g_delete_calls++;
        record_step(STEP_DELETE_STORAGE);
        return g_delete_result;
    }
    esp_err_t tinyusb_msc_set_storage_mount_point(tinyusb_msc_storage_handle_t handle,
                                                   tinyusb_msc_mount_point_t mount_point) {
        tinyusb_msc_event_t event;
        CHECK(handle != NULL);
        CHECK(g_storage_cb != NULL);
        event.id = TINYUSB_MSC_EVENT_MOUNT_START;
        event.mount_point = mount_point;
        g_storage_cb(handle, &event, g_storage_arg);
        event.id = TINYUSB_MSC_EVENT_MOUNT_COMPLETE;
        g_storage_cb(handle, &event, g_storage_arg);
        return ESP_OK;
    }

    esp_err_t tinyusb_driver_install(const tinyusb_config_t *config) {
        g_driver_install_calls++;
        g_device_cb = config->event_cb;
        g_device_arg = config->event_arg;
        return ESP_OK;
    }
    esp_err_t tinyusb_driver_uninstall(void) {
        g_driver_uninstall_calls++;
        record_step(STEP_UNINSTALL);
        return g_uninstall_result;
    }
    bool tud_disconnect(void) {
        g_disconnect_calls++;
        return g_disconnect_result;
    }

    static usb_msc_ownership_event_t wait_expected(usb_msc_ownership_event_t expected) {
        for (int i = 0; i < 1000; ++i) {
            usb_msc_ownership_event_t event = usb_msc_ownership_wait_event(0);
            if (event == expected) return event;
            if (event != USB_MSC_EVENT_NONE) {
                fprintf(stderr, "unexpected USB event %d while waiting for %d\n",
                        (int)event, (int)expected);
                exit(2);
            }
            sleep_ms(1);
        }
        fprintf(stderr, "timeout waiting for USB event %d\n", (int)expected);
        exit(2);
    }

    static void *attach_thread(void *arg) {
        tinyusb_event_t event = { .id = TINYUSB_EVENT_ATTACHED };
        (void)arg;
        CHECK(g_device_cb != NULL);
        g_device_cb(&event, g_device_arg);
        atomic_store(&g_attach_done, 1);
        return NULL;
    }

    static void enter_host_owned(void) {
        pthread_t thread;
        CHECK(sd_mount_recordings() == ESP_OK);
        CHECK(sd_mount_is_mounted());
        CHECK(usb_msc_ownership_init() == ESP_OK);
        CHECK(usb_msc_ownership_start() == ESP_OK);
        CHECK(g_driver_install_calls == 1);

        atomic_store(&g_attach_done, 0);
        CHECK(pthread_create(&thread, NULL, attach_thread, NULL) == 0);
        (void)wait_expected(USB_MSC_EVENT_ATTACH);
        CHECK(atomic_load(&g_attach_done) == 0);

        CHECK(sd_mount_release_for_usb() == ESP_OK);
        sd_mount_unmount();
        CHECK(sd_mount_device_fs_released());
        CHECK(usb_msc_ownership_note_prepare_complete(true, true, true) == ESP_OK);
        CHECK(pthread_join(thread, NULL) == 0);
        CHECK(atomic_load(&g_attach_done) == 1);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!sd_mount_is_mounted());
        (void)wait_expected(USB_MSC_EVENT_HOST_OWNED);
    }

    static void trigger_suspend_barrier(void) {
        tinyusb_event_t event = { .id = TINYUSB_EVENT_SUSPENDED };
        CHECK(g_device_cb != NULL);
        g_device_cb(&event, g_device_arg);
        CHECK(g_disconnect_calls == 1);
        (void)wait_expected(USB_MSC_EVENT_BARRIER_REQUIRED);
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(!sd_mount_is_mounted());
        CHECK(usb_msc_ownership_rearm() == ESP_ERR_INVALID_STATE);
    }

    static void reset_sequence(void) { g_sequence_len = 0; }

    static void assert_failed_closed(void) {
        CHECK(!sd_mount_is_mounted());
        CHECK(usb_msc_ownership_is_host_owned());
        CHECK(usb_msc_ownership_rearm() == ESP_ERR_INVALID_STATE);
        (void)wait_expected(USB_MSC_EVENT_FAILED);
        CHECK(usb_msc_ownership_wait_event(0) == USB_MSC_EVENT_NONE);
    }

    static void run_success(void) {
        pthread_t thread;
        int installs_before_rearm;

        enter_host_owned();
        trigger_suspend_barrier();
        reset_sequence();
        CHECK(usb_msc_ownership_complete_disconnect_barrier() == ESP_OK);
        CHECK(g_sequence_len == 3);
        CHECK(g_sequence[0] == STEP_UNINSTALL);
        CHECK(g_sequence[1] == STEP_DELETE_STORAGE);
        CHECK(g_sequence[2] == STEP_NEW_STORAGE);
        CHECK(g_driver_uninstall_calls == 1);
        CHECK(g_delete_calls == 1);
        CHECK(sd_mount_is_mounted());
        CHECK(!usb_msc_ownership_is_host_owned());
        (void)wait_expected(USB_MSC_EVENT_DETACH);
        CHECK(usb_msc_ownership_wait_event(0) == USB_MSC_EVENT_NONE);
        CHECK(sd_mount_remount_after_usb() == ESP_OK);

        /* Barrier completion alone is not permission to publish a new USB session. */
        CHECK(usb_msc_ownership_rearm() == ESP_ERR_INVALID_STATE);
        CHECK(usb_msc_ownership_note_recording_recovered() == ESP_OK);
        installs_before_rearm = g_driver_install_calls;
        CHECK(usb_msc_ownership_rearm() == ESP_OK);
        CHECK(g_driver_install_calls == installs_before_rearm + 1);
        CHECK(usb_msc_ownership_wait_event(0) == USB_MSC_EVENT_NONE);

        /* Fresh rearm must not reuse the prior session's PREPARE_OK proof. */
        atomic_store(&g_attach_done, 0);
        CHECK(pthread_create(&thread, NULL, attach_thread, NULL) == 0);
        (void)wait_expected(USB_MSC_EVENT_ATTACH);
        sleep_ms(20);
        CHECK(atomic_load(&g_attach_done) == 0);
        CHECK(sd_mount_release_for_usb() == ESP_OK);
        sd_mount_unmount();
        CHECK(usb_msc_ownership_note_prepare_complete(true, true, true) == ESP_OK);
        CHECK(pthread_join(thread, NULL) == 0);
        CHECK(usb_msc_ownership_is_host_owned());
    }

    static void run_teardown_failure(void) {
        enter_host_owned();
        trigger_suspend_barrier();
        reset_sequence();
        g_uninstall_result = ESP_FAIL;
        CHECK(usb_msc_ownership_complete_disconnect_barrier() == ESP_FAIL);
        CHECK(g_sequence_len == 1);
        CHECK(g_sequence[0] == STEP_UNINSTALL);
        CHECK(g_delete_calls == 0);
        assert_failed_closed();
    }

    static void run_deferred_write_failure(void) {
        enter_host_owned();
        trigger_suspend_barrier();
        reset_sequence();
        g_delete_result = ESP_FAIL;
        CHECK(usb_msc_ownership_complete_disconnect_barrier() == ESP_FAIL);
        CHECK(g_sequence_len == 2);
        CHECK(g_sequence[0] == STEP_UNINSTALL);
        CHECK(g_sequence[1] == STEP_DELETE_STORAGE);
        assert_failed_closed();
    }

    static void run_app_rebuild_failure(void) {
        enter_host_owned();
        trigger_suspend_barrier();
        reset_sequence();
        g_new_storage_result = ESP_FAIL;
        CHECK(usb_msc_ownership_complete_disconnect_barrier() == ESP_FAIL);
        CHECK(g_sequence_len == 3);
        CHECK(g_sequence[0] == STEP_UNINSTALL);
        CHECK(g_sequence[1] == STEP_DELETE_STORAGE);
        CHECK(g_sequence[2] == STEP_NEW_STORAGE);
        assert_failed_closed();
    }

    int main(int argc, char **argv) {
        CHECK(argc == 2);
        if (strcmp(argv[1], "success") == 0) run_success();
        else if (strcmp(argv[1], "teardown") == 0) run_teardown_failure();
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


@pytest.fixture(scope="module")
def ownership_harness(tmp_path_factory: pytest.TempPathFactory) -> Path:
    build = tmp_path_factory.mktemp("usb-ownership-behavior")
    stubs = build / "stubs"
    _write_stubs(stubs)
    harness = build / "harness.c"
    harness.write_text(textwrap.dedent(HARNESS).lstrip(), encoding="utf-8")
    output = build / "usb_ownership_harness"
    compiler = shutil.which("cc") or shutil.which("gcc")
    assert compiler is not None, "Firmware host behavioral tests require a C compiler"
    command = [
        compiler,
        "-std=c11",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-pthread",
        "-DESP_PLATFORM=1",
        "-DCONFIG_TINYUSB_SUSPEND_CALLBACK=1",
        "-I",
        str(stubs),
        "-I",
        str(INCLUDE),
        str(RECORDER / "usb_msc_ownership.c"),
        str(RECORDER / "sd_mount.c"),
        str(harness),
        "-o",
        str(output),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    assert result.returncode == 0, (
        "behavioral harness compile failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return output


@pytest.mark.parametrize(
    "scenario",
    ["success", "teardown", "deferred-write", "app-rebuild"],
)
def test_usb_disconnect_barrier_executes_production_state_machine(
    ownership_harness: Path, scenario: str
) -> None:
    result = subprocess.run(
        [str(ownership_harness), scenario],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"scenario={scenario}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
