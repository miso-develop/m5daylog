// microSD SPI ownership + Tasks #44/#45/#46 directory bring-up.
//
// Task #49 initializes SDSPI once and lets esp_tinyusb own FAT/VFS mounting.
// APP -> USB transfer keeps the raw card alive while moving the filesystem
// registration, which is the required single-owner boundary for MSC.

#include "sd_mount.h"

#include "recorder_config.h"

#include <string.h>

#ifdef ESP_PLATFORM

#include <errno.h>
#include <stdio.h>
#include <sys/stat.h>

#include "driver/sdspi_host.h"
#include "driver/spi_common.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "sdmmc_cmd.h"
#include "tinyusb_msc.h"

static const char *TAG = "recorder_sd";

static sdmmc_card_t s_card;
static sdspi_dev_handle_t s_sdspi = SDSPI_DEFAULT_HOST;
static tinyusb_msc_storage_handle_t s_storage = NULL;
static spi_host_device_t s_spi_host = SPI2_HOST;
static bool s_bus_init = false;
static bool s_sdspi_init = false;
static bool s_device_init = false;
static bool s_mounted = false;
static bool s_usb_release_requested = false;
static bool s_device_fs_released = false;
static portMUX_TYPE s_owner_lock = portMUX_INITIALIZER_UNLOCKED;

static esp_err_t ensure_recording_dirs(void) {
    if (mkdir(RECORDER_M5DAYLOG_DIR, 0755) != 0) {
        int mkdir_errno = errno;
        if (mkdir_errno != EEXIST) {
            ESP_LOGE(TAG,
                     "stage: record, result: error, reason: mkdir daylog, errno: %d",
                     mkdir_errno);
            return ESP_FAIL;
        }
    }
    if (mkdir(RECORDER_RECORDINGS_DIR, 0755) != 0) {
        int mkdir_errno = errno;
        if (mkdir_errno != EEXIST) {
            ESP_LOGE(TAG,
                     "stage: record, result: error, reason: mkdir rec, errno: %d",
                     mkdir_errno);
            return ESP_FAIL;
        }
    }
    if (mkdir(RECORDER_QUARANTINE_DIR, 0755) != 0) {
        int mkdir_errno = errno;
        if (mkdir_errno != EEXIST) {
            ESP_LOGE(TAG,
                     "stage: recover, result: error, reason: mkdir quarantine, errno: %d",
                     mkdir_errno);
            return ESP_FAIL;
        }
    }
    return ESP_OK;
}

static void sd_mount_raw_cleanup(void) {
    if (s_device_init) {
        (void)sdspi_host_remove_device(s_sdspi);
        s_device_init = false;
    }
    if (s_sdspi_init) {
        (void)sdspi_host_deinit();
        s_sdspi_init = false;
    }
    if (s_bus_init) {
        (void)spi_bus_free(s_spi_host);
        s_bus_init = false;
    }
    memset(&s_card, 0, sizeof(s_card));
}

static void sd_mount_terminal_storage_event(tinyusb_msc_storage_handle_t handle,
                                            tinyusb_msc_event_t *event,
                                            void *arg) {
    (void)handle;
    (void)event;
    (void)arg;
}

esp_err_t sd_mount_recordings(void) {
    esp_vfs_fat_mount_config_t mount_config = {
        .format_if_mount_failed = false,
        .max_files = 4,
        .allocation_unit_size = 16 * 1024,
    };
    sdmmc_host_t host = SDSPI_HOST_DEFAULT();
    sdspi_device_config_t dev = SDSPI_DEVICE_CONFIG_DEFAULT();
    spi_bus_config_t bus_cfg = {
        .mosi_io_num = RECORDER_SD_MOSI_PIN,
        .miso_io_num = RECORDER_SD_MISO_PIN,
        .sclk_io_num = RECORDER_SD_CLK_PIN,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4096,
    };
    tinyusb_msc_storage_config_t storage_cfg = {0};
    esp_err_t err;

    if (sd_mount_is_mounted()) {
        return ensure_recording_dirs();
    }
    if (s_storage != NULL) {
        // USB owns the medium. A recorder session may only restart after the
        // TinyUSB callback has mounted it back to APP.
        return ESP_ERR_INVALID_STATE;
    }

    err = spi_bus_initialize(host.slot, &bus_cfg, SDSPI_DEFAULT_DMA);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: spi bus");
        return err;
    }
    s_spi_host = host.slot;
    s_bus_init = true;

    err = sdspi_host_init();
    if (err != ESP_OK) {
        sd_mount_raw_cleanup();
        return err;
    }
    s_sdspi_init = true;

    dev.host_id = host.slot;
    dev.gpio_cs = RECORDER_SD_CS_PIN;
    err = sdspi_host_init_device(&dev, &s_sdspi);
    if (err != ESP_OK) {
        sd_mount_raw_cleanup();
        return err;
    }
    s_device_init = true;

    host.slot = s_sdspi;
    memset(&s_card, 0, sizeof(s_card));
    err = sdmmc_card_init(&host, &s_card);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: sd init");
        sd_mount_raw_cleanup();
        return err;
    }

    storage_cfg.medium.card = &s_card;
    storage_cfg.fat_fs.base_path = RECORDER_SD_MOUNT_POINT;
    storage_cfg.fat_fs.config = mount_config;
    storage_cfg.fat_fs.do_not_format = true;
    storage_cfg.mount_point = TINYUSB_MSC_STORAGE_MOUNT_APP;
    err = tinyusb_msc_new_storage_sdmmc(&storage_cfg, &s_storage);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: sd mount");
        s_storage = NULL;
        sd_mount_raw_cleanup();
        return err;
    }

    portENTER_CRITICAL(&s_owner_lock);
    s_mounted = true;
    s_usb_release_requested = false;
    s_device_fs_released = false;
    portEXIT_CRITICAL(&s_owner_lock);

    err = ensure_recording_dirs();
    if (err != ESP_OK) {
        sd_mount_unmount();
        return err;
    }

    ESP_LOGI(TAG, "stage: record, result: sd ready, mount: %s",
             RECORDER_SD_MOUNT_POINT);
    return ESP_OK;
}

esp_err_t sd_mount_ensure_date_dir(const char *date_yyyy_mm_dd) {
    char dir[RECORDER_MAX_PATH_LEN];
    int needed;
    size_t i;

    if (!sd_mount_is_mounted() || sd_mount_device_fs_released()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (date_yyyy_mm_dd == NULL || strlen(date_yyyy_mm_dd) != 10u) {
        return ESP_ERR_INVALID_ARG;
    }
    for (i = 0; i < 10u; i++) {
        char c = date_yyyy_mm_dd[i];
        if (i == 4u || i == 7u) {
            if (c != '-') {
                return ESP_ERR_INVALID_ARG;
            }
        } else if (c < '0' || c > '9') {
            return ESP_ERR_INVALID_ARG;
        }
    }
    needed = snprintf(dir, sizeof(dir), "%s/%s", RECORDER_RECORDINGS_DIR,
                      date_yyyy_mm_dd);
    if (needed < 0 || (size_t)needed >= sizeof(dir)) {
        return ESP_ERR_INVALID_ARG;
    }
    if (mkdir(dir, 0755) != 0) {
        int mkdir_errno = errno;
        if (mkdir_errno != EEXIST) {
            ESP_LOGE(TAG,
                     "stage: rotate, result: error, reason: mkdir date, errno: %d",
                     mkdir_errno);
            return ESP_FAIL;
        }
    }
    return ESP_OK;
}

esp_err_t sd_mount_ensure_quarantine_dir(void) {
    if (!sd_mount_is_mounted() || sd_mount_device_fs_released()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (mkdir(RECORDER_QUARANTINE_DIR, 0755) != 0) {
        int mkdir_errno = errno;
        if (mkdir_errno != EEXIST) {
            ESP_LOGE(TAG,
                     "stage: recover, result: error, reason: mkdir quarantine, errno: %d",
                     mkdir_errno);
            return ESP_FAIL;
        }
    }
    return ESP_OK;
}

bool sd_mount_is_mounted(void) {
    bool mounted;
    portENTER_CRITICAL(&s_owner_lock);
    mounted = s_mounted;
    portEXIT_CRITICAL(&s_owner_lock);
    return mounted;
}

esp_err_t sd_mount_release_for_usb(void) {
    esp_err_t ret = ESP_OK;
    portENTER_CRITICAL(&s_owner_lock);
    if (!s_mounted || s_storage == NULL || s_usb_release_requested) {
        ret = ESP_ERR_INVALID_STATE;
    } else {
        s_usb_release_requested = true;
        s_device_fs_released = false;
    }
    portEXIT_CRITICAL(&s_owner_lock);
    return ret;
}

bool sd_mount_device_fs_released(void) {
    bool released;
    portENTER_CRITICAL(&s_owner_lock);
    released = s_device_fs_released;
    portEXIT_CRITICAL(&s_owner_lock);
    return released;
}

void sd_mount_note_usb_owned(void) {
    portENTER_CRITICAL(&s_owner_lock);
    if (s_usb_release_requested && s_device_fs_released) {
        s_mounted = false;
    }
    portEXIT_CRITICAL(&s_owner_lock);
}

void sd_mount_note_app_owned(void) {
    portENTER_CRITICAL(&s_owner_lock);
    s_mounted = true;
    s_usb_release_requested = false;
    s_device_fs_released = false;
    portEXIT_CRITICAL(&s_owner_lock);
}

esp_err_t sd_mount_remount_after_usb(void) {
    if (!sd_mount_is_mounted() || s_storage == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    portENTER_CRITICAL(&s_owner_lock);
    s_usb_release_requested = false;
    s_device_fs_released = false;
    portEXIT_CRITICAL(&s_owner_lock);
    return ensure_recording_dirs();
}

void sd_mount_unmount(void) {
    bool usb_release;
    portENTER_CRITICAL(&s_owner_lock);
    usb_release = s_usb_release_requested;
    if (usb_release) {
        // The writer reaches this only after closing/finalizing its segment and
        // committing metadata. Keep raw SD alive; TinyUSB will physically
        // unmount VFS after the blocked ownership callback is released.
        s_device_fs_released = true;
    }
    portEXIT_CRITICAL(&s_owner_lock);
    if (usb_release) {
        ESP_LOGI(TAG,
                 "stage: usb, result: device-fs-released, mount: app");
        return;
    }

    if (s_storage != NULL) {
        // Terminal low-battery/error shutdown is not a USB handoff. Replace
        // the blocking ownership callback before deleting the storage.
        (void)tinyusb_msc_set_storage_callback(sd_mount_terminal_storage_event,
                                               NULL);
        if (tinyusb_msc_delete_storage(s_storage) != ESP_OK) {
            ESP_LOGE(TAG,
                     "stage: record, result: error, reason: sd storage delete");
            return;
        }
        s_storage = NULL;
    }
    portENTER_CRITICAL(&s_owner_lock);
    s_mounted = false;
    s_device_fs_released = true;
    portEXIT_CRITICAL(&s_owner_lock);
    sd_mount_raw_cleanup();
}

#else

esp_err_t sd_mount_recordings(void) {
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t sd_mount_ensure_date_dir(const char *date_yyyy_mm_dd) {
    (void)date_yyyy_mm_dd;
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t sd_mount_ensure_quarantine_dir(void) {
    return ESP_ERR_NOT_SUPPORTED;
}

bool sd_mount_is_mounted(void) {
    return false;
}

esp_err_t sd_mount_release_for_usb(void) {
    return ESP_ERR_NOT_SUPPORTED;
}

bool sd_mount_device_fs_released(void) {
    return false;
}

void sd_mount_note_usb_owned(void) {
}

void sd_mount_note_app_owned(void) {
}

esp_err_t sd_mount_remount_after_usb(void) {
    return ESP_ERR_NOT_SUPPORTED;
}

void sd_mount_unmount(void) {
}

#endif
