// microSD SPI mount + Tasks #44/#45/#46 directory bring-up.
//
// M5Capsule v1.1: SD over SPI (CS 11 / MOSI 12 / CLK 14 / MISO 39),
// mounted at /sdcard. Live-recording directories, Task #45 per-date
// subdirectories (`recordings/YYYY-MM-DD/`), and the Task #46 quarantine
// directory are created. No other state.

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
#include "esp_vfs_fat.h"
#include "sdmmc_cmd.h"

static const char *TAG = "recorder_sd";

static sdmmc_card_t *s_card = NULL;
static bool s_mounted = false;
static bool s_bus_init = false;
// Only the SPI host id persists: the sdmmc_host_t itself is a call-local
// `SDSPI_HOST_DEFAULT()` value, matching the ESP-IDF v5.5.5 SDSPI example.
static spi_host_device_t s_spi_host = SPI2_HOST;

static esp_err_t ensure_recording_dirs(void) {
    if (mkdir(RECORDER_M5DAYLOG_DIR, 0755) != 0) {
        int mkdir_errno = errno;
        if (mkdir_errno != EEXIST) {
            ESP_LOGE(TAG,
                     "stage: record, result: error, reason: mkdir daylog, "
                     "errno: %d",
                     mkdir_errno);
            return ESP_FAIL;
        }
    }
    if (mkdir(RECORDER_RECORDINGS_DIR, 0755) != 0) {
        int mkdir_errno = errno;
        if (mkdir_errno != EEXIST) {
            ESP_LOGE(TAG,
                     "stage: record, result: error, reason: mkdir rec, "
                     "errno: %d",
                     mkdir_errno);
            return ESP_FAIL;
        }
    }
    // Task #46: quarantine isolation directory. Never auto-deleted;
    // recovery moves unrecoverable `.wav.part` files here with rename().
    if (mkdir(RECORDER_QUARANTINE_DIR, 0755) != 0) {
        int mkdir_errno = errno;
        if (mkdir_errno != EEXIST) {
            ESP_LOGE(TAG,
                     "stage: recover, result: error, reason: mkdir quarantine, "
                     "errno: %d",
                     mkdir_errno);
            return ESP_FAIL;
        }
    }
    return ESP_OK;
}

esp_err_t sd_mount_recordings(void) {
    esp_vfs_fat_mount_config_t mount_config = {
        // Never format: a mount failure must surface as ERROR, never
        // destroy unacknowledged recordings.
        .format_if_mount_failed = false,
        .max_files = 4,
        .allocation_unit_size = 16 * 1024,
    };
    sdmmc_host_t host = SDSPI_HOST_DEFAULT();
    sdspi_device_config_t slot_config = SDSPI_DEVICE_CONFIG_DEFAULT();
    spi_bus_config_t bus_cfg = {
        .mosi_io_num = RECORDER_SD_MOSI_PIN,
        .miso_io_num = RECORDER_SD_MISO_PIN,
        .sclk_io_num = RECORDER_SD_CLK_PIN,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4000,
    };
    esp_err_t err;

    if (s_mounted) {
        return ensure_recording_dirs();
    }

    // The SPI host value goes to the slot config directly (no invented
    // wrapper types): host.slot is exactly what spi_bus_initialize takes.
    slot_config.host_id = host.slot;
    slot_config.gpio_cs = RECORDER_SD_CS_PIN;

    err = spi_bus_initialize(host.slot, &bus_cfg, SDSPI_DEFAULT_DMA);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: spi bus");
        return err;
    }
    s_spi_host = host.slot;
    s_bus_init = true;

    // ESP-IDF v5.5 parameter order: base_path, host, slot, mount, card.
    err = esp_vfs_fat_sdspi_mount(RECORDER_SD_MOUNT_POINT, &host,
                                  &slot_config, &mount_config, &s_card);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: sd mount");
        spi_bus_free(s_spi_host);
        s_bus_init = false;
        s_card = NULL;
        return err;
    }
    s_mounted = true;

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

    if (!s_mounted) {
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
                     "stage: rotate, result: error, reason: mkdir date, "
                     "errno: %d",
                     mkdir_errno);
            return ESP_FAIL;
        }
    }
    return ESP_OK;
}

esp_err_t sd_mount_ensure_quarantine_dir(void) {
    if (!s_mounted) {
        return ESP_ERR_INVALID_STATE;
    }
    if (mkdir(RECORDER_QUARANTINE_DIR, 0755) != 0) {
        int mkdir_errno = errno;
        if (mkdir_errno != EEXIST) {
            ESP_LOGE(TAG,
                     "stage: recover, result: error, reason: mkdir quarantine, "
                     "errno: %d",
                     mkdir_errno);
            return ESP_FAIL;
        }
    }
    return ESP_OK;
}

bool sd_mount_is_mounted(void) {
    return s_mounted;
}

void sd_mount_unmount(void) {
    if (!s_mounted) {
        return;
    }
    esp_vfs_fat_sdcard_unmount(RECORDER_SD_MOUNT_POINT, s_card);
    s_card = NULL;
    s_mounted = false;
    if (s_bus_init) {
        spi_bus_free(s_spi_host);
        s_bus_init = false;
    }
}

#else  // !ESP_PLATFORM — host/test build: linkable stubs, never mounted.

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

void sd_mount_unmount(void) {
}

#endif  // ESP_PLATFORM
