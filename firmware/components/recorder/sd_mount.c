// microSD SPI ownership + recorder directory bring-up.
//
// Task #49 publishes the medium to USB only after recorder finalization.
// Task #87 Strategy 2 never remounts APP storage after host release: explicit
// eject is followed by USB teardown and deletion of the USB storage object,
// then the powered session ends in SHUTDOWN_ARMED.

#include "sd_mount.h"
#include "recorder_config.h"

#include <string.h>

#ifdef ESP_PLATFORM

#include <errno.h>
#include <stdio.h>
#include <sys/stat.h>
#if defined(CONFIG_M5DAYLOG_SDB11_DEV_INSTALL) && CONFIG_M5DAYLOG_SDB11_DEV_INSTALL
#include <dirent.h>
#include "dev_sdb11_media_readback.h"
#include "device_identity.h"
#include "esp_vfs_fat.h"
#endif

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
static bool s_msc_driver_init = false;
static bool s_mounted = false;
static bool s_usb_release_requested = false;
static bool s_device_fs_released = false;
static portMUX_TYPE s_owner_lock = portMUX_INITIALIZER_UNLOCKED;

#if defined(CONFIG_M5DAYLOG_SDB11_DEV_INSTALL) && CONFIG_M5DAYLOG_SDB11_DEV_INSTALL
static bool s_dev_preflight_passed;
static bool s_dev_first_boot_preflight;
static bool dev_sd_same_selected_card(const sdmmc_card_t *card,
                                      uint32_t serial, uint64_t sectors);
#endif

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

static void sd_mount_fill_storage_config(tinyusb_msc_storage_config_t *config,
                                         tinyusb_msc_mount_point_t mount_point) {
    esp_vfs_fat_mount_config_t mount_config = {
        .format_if_mount_failed = false,
        .max_files = 4,
        .allocation_unit_size = 16 * 1024,
    };

    memset(config, 0, sizeof(*config));
    config->medium.card = &s_card;
    config->fat_fs.base_path = RECORDER_SD_MOUNT_POINT;
    config->fat_fs.config = mount_config;
    config->fat_fs.do_not_format = true;
    config->mount_point = mount_point;
}

static esp_err_t sd_mount_install_msc_driver(void) {
    tinyusb_msc_driver_config_t config = {
        .user_flags = {
            .auto_mount_off = 1,
        },
        .callback = sd_mount_terminal_storage_event,
        .callback_arg = NULL,
    };
    esp_err_t err;

    if (s_msc_driver_init) {
        return ESP_OK;
    }
    err = tinyusb_msc_install_driver(&config);
    if (err == ESP_OK) {
        s_msc_driver_init = true;
    }
    return err;
}

static esp_err_t sd_mount_create_storage(tinyusb_msc_mount_point_t mount_point) {
    tinyusb_msc_storage_config_t storage_cfg;
    esp_err_t err;

    if (!s_msc_driver_init || s_storage != NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    sd_mount_fill_storage_config(&storage_cfg, mount_point);
    err = tinyusb_msc_new_storage_sdmmc(&storage_cfg, &s_storage);
    if (err != ESP_OK) {
        s_storage = NULL;
        return err;
    }
    return ESP_OK;
}

esp_err_t sd_mount_recordings(void) {
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
    esp_err_t err;

    if (sd_mount_is_mounted()) {
        return ensure_recording_dirs();
    }
    if (s_storage != NULL) {
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

#if defined(CONFIG_M5DAYLOG_SDB11_DEV_INSTALL) && CONFIG_M5DAYLOG_SDB11_DEV_INSTALL && \
    (!defined(CONFIG_M5DAYLOG_SDB11_INSTALLER_MODE) || !CONFIG_M5DAYLOG_SDB11_INSTALLER_MODE)
    // Recheck the *live* SPI card after the read-only preflight and before
    // creating APP storage, mounting FAT or creating any directories. This
    // prevents swapping a different card between the GPIO proof and recording.
    if (!s_dev_preflight_passed ||
        !dev_sd_same_selected_card(&s_card,
             (uint32_t)CONFIG_M5DAYLOG_SDB11_EXPECTED_CID_SERIAL,
             (uint64_t)CONFIG_M5DAYLOG_SDB11_EXPECTED_SECTOR_COUNT)) {
        sd_mount_raw_cleanup();
        return ESP_ERR_INVALID_STATE;
    }
#endif

    err = sd_mount_install_msc_driver();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: msc driver");
        sd_mount_raw_cleanup();
        return err;
    }
    err = sd_mount_create_storage(TINYUSB_MSC_STORAGE_MOUNT_APP);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: sd mount");
        (void)tinyusb_msc_uninstall_driver();
        s_msc_driver_init = false;
        sd_mount_raw_cleanup();
        return err;
    }

#if defined(CONFIG_M5DAYLOG_SDB11_DEV_INSTALL) && CONFIG_M5DAYLOG_SDB11_DEV_INSTALL && \
    (!defined(CONFIG_M5DAYLOG_SDB11_INSTALLER_MODE) || !CONFIG_M5DAYLOG_SDB11_INSTALLER_MODE)
    // FAT may be mounted, but no directory or WAV may be
    // created until installed generation readback passes against actual APP card.
    if (!dev_sdb11_media_readback_matches(
            CONFIG_M5DAYLOG_SDB11_EXPECTED_GENERATION,
            CONFIG_M5DAYLOG_SDB11_EXPECTED_DEVICE_ID,
            CONFIG_M5DAYLOG_SDB11_EXPECTED_BASELINE_SHA256,
            s_dev_first_boot_preflight)) {
        (void)tinyusb_msc_delete_storage(s_storage);
        s_storage = NULL;
        (void)tinyusb_msc_uninstall_driver();
        s_msc_driver_init = false;
        sd_mount_raw_cleanup();
        return ESP_ERR_INVALID_STATE;
    }
#endif

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

// Dedicated diagnostic image: one read-only SD SPI probe before any NVS,
// FAT, TinyUSB or recorder startup. No media block read/write and no auto
// retry. Production/installer builds link a fail-closed stub instead.
#if defined(CONFIG_M5DAYLOG_SDB11_SD_ID_PROBE_MODE) && CONFIG_M5DAYLOG_SDB11_SD_ID_PROBE_MODE
static bool s_dev_sd_probe_attempted;

esp_err_t sd_mount_dev_observe_identity(uint32_t *cid_serial,
                                        uint64_t *sector_count) {
    sdmmc_host_t host = SDSPI_HOST_DEFAULT();
    sdspi_device_config_t dev = SDSPI_DEVICE_CONFIG_DEFAULT();
    spi_bus_config_t bus = {
        .mosi_io_num = RECORDER_SD_MOSI_PIN,
        .miso_io_num = RECORDER_SD_MISO_PIN,
        .sclk_io_num = RECORDER_SD_CLK_PIN,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4096,
    };
    const spi_host_device_t spi_host = SDSPI_DEFAULT_HOST;
    sdspi_dev_handle_t handle = -1;
    sdmmc_card_t card = {0};
    bool bus_ready = false;
    bool host_ready = false;
    bool device_ready = false;
    uint32_t observed_serial = 0;
    uint64_t observed_sectors = 0;
    esp_err_t err;

    if (cid_serial == NULL || sector_count == NULL) return ESP_ERR_INVALID_ARG;
    *cid_serial = 0;
    *sector_count = 0;
    if (s_dev_sd_probe_attempted || s_storage != NULL || s_mounted ||
        s_bus_init || s_sdspi_init || s_device_init || s_msc_driver_init) {
        return ESP_ERR_INVALID_STATE;
    }
    s_dev_sd_probe_attempted = true;

    err = spi_bus_initialize(spi_host, &bus, SDSPI_DEFAULT_DMA);
    if (err != ESP_OK) goto cleanup;
    bus_ready = true;
    err = sdspi_host_init();
    if (err != ESP_OK) goto cleanup;
    host_ready = true;
    dev.host_id = spi_host;
    dev.gpio_cs = RECORDER_SD_CS_PIN;
    err = sdspi_host_init_device(&dev, &handle);
    if (err != ESP_OK) goto cleanup;
    device_ready = true;
    host.slot = handle;
    err = sdmmc_card_init(&host, &card);
    if (err != ESP_OK) goto cleanup;
    observed_serial = card.cid.serial;
    observed_sectors = (uint64_t)card.csd.capacity;
    // Kconfig's sector-count upper bound is signed 32-bit. An empty/default
    // CID or an unrepresentable capacity is not a flashable target pin.
    if (observed_serial == 0u || observed_sectors == 0u ||
        observed_sectors > INT32_MAX) err = ESP_ERR_INVALID_RESPONSE;

cleanup:
    // ESP-IDF v5.5.5 has no sdmmc_card_deinit(). In SPI mode card_init
    // does not allocate the SDMMC-only aligned buffer; removing the SDSPI
    // device/host/bus releases every resource used by this identity probe.
    if (device_ready) {
        esp_err_t done = sdspi_host_remove_device(handle);
        if (err == ESP_OK && done != ESP_OK) err = done;
    }
    if (host_ready) {
        esp_err_t done = sdspi_host_deinit();
        if (err == ESP_OK && done != ESP_OK) err = done;
    }
    if (bus_ready) {
        esp_err_t done = spi_bus_free(spi_host);
        if (err == ESP_OK && done != ESP_OK) err = done;
    }
    if (err == ESP_OK) {
        *cid_serial = observed_serial;
        *sector_count = observed_sectors;
    }
    return err;
}
#else
esp_err_t sd_mount_dev_observe_identity(uint32_t *cid_serial,
                                        uint64_t *sector_count) {
    if (cid_serial != NULL) *cid_serial = 0;
    if (sector_count != NULL) *sector_count = 0;
    return ESP_ERR_NOT_SUPPORTED;
}
#endif

// SDB-1.1 intentionally destructive installer primitive. The source-only
// entrypoint is NOT reachable from ordinary app_main or any CDC/MSC command.
// Only the exact already-mounted, Device-owned SPI card is targeted; a GPIO,
// ROM/APP/image witness and the external one-use grant belong to a separate
// operator-approved installer (not this helper).
#if defined(CONFIG_M5DAYLOG_SDB11_DEV_INSTALL) && CONFIG_M5DAYLOG_SDB11_DEV_INSTALL
static bool dev_sd_empty_root(void) {
    DIR *root = opendir(RECORDER_SD_MOUNT_POINT);
    struct dirent *ent;
    if (root == NULL) return false;
    errno = 0;
    while ((ent = readdir(root)) != NULL) {
        if (strcmp(ent->d_name, ".") != 0 &&
            strcmp(ent->d_name, "..") != 0) {
            (void)closedir(root);
            return false;
        }
        errno = 0;
    }
    const int scan_errno = errno;
    const bool closed = closedir(root) == 0;
    return scan_errno == 0 && closed;
}

static bool dev_sd_same_selected_card(const sdmmc_card_t *card,
                                      uint32_t serial, uint64_t sectors) {
    // CID+capacity is a collision/target-denial guard, not authenticated
    // physical origin. The exact physical media/ROM must be proven separately.
    return card != NULL && card->cid.serial == serial &&
           (uint64_t)card->csd.capacity == sectors;
}


// PRE-INTENT read-only media verification. No TinyUSB storage is created;
// format_if_mount_failed=false forbids implicit FAT reinitialization.
// Source-only proof: platform-level power-loss and driver write denial still
// need independent physical qualification before an authorized install.
esp_err_t sd_mount_dev_verify_selected_baseline(uint32_t expected_cid_serial,
                                                 uint64_t expected_sector_count,
                                                 const char *generation,
                                                 const char *device_id,
                                                 const char *binding,
                                                 bool first_boot) {
    sdmmc_host_t host = SDSPI_HOST_DEFAULT();
    sdspi_device_config_t dev = SDSPI_DEVICE_CONFIG_DEFAULT();
    spi_bus_config_t bus = {
        .mosi_io_num = RECORDER_SD_MOSI_PIN,
        .miso_io_num = RECORDER_SD_MISO_PIN,
        .sclk_io_num = RECORDER_SD_CLK_PIN,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4096,
    };
    esp_vfs_fat_mount_config_t fs = {
        .format_if_mount_failed = false,
        .max_files = 4,
        .allocation_unit_size = 16 * 1024,
    };
    sdmmc_card_t *card = NULL;
    bool mounted = false;
    esp_err_t err, cleanup_err;
    if (s_dev_preflight_passed || expected_sector_count == 0u ||
        s_storage != NULL || sd_mount_is_mounted() ||
        s_bus_init || s_sdspi_init || s_device_init || s_msc_driver_init ||
        !device_identity_is_valid_uuid(generation) ||
        !device_identity_is_valid_uuid(device_id) ||
        strlen(binding) != 64u) return ESP_ERR_INVALID_STATE;

    err = spi_bus_initialize(host.slot, &bus, SDSPI_DEFAULT_DMA);
    if (err != ESP_OK) return err;
    dev.host_id = host.slot;
    dev.gpio_cs = RECORDER_SD_CS_PIN;
    err = esp_vfs_fat_sdspi_mount(RECORDER_SD_MOUNT_POINT,
                                 &host, &dev, &fs, &card);
    if (err == ESP_OK) {
        mounted = true;
        if (!dev_sd_same_selected_card(card, expected_cid_serial,
                                        expected_sector_count) ||
            !dev_sdb11_media_readback_matches(generation, device_id, binding,
                                      first_boot)) {
            err = ESP_ERR_INVALID_STATE;
        }
    }
    if (mounted) {
        cleanup_err = esp_vfs_fat_sdcard_unmount(RECORDER_SD_MOUNT_POINT, card);
        if (err == ESP_OK && cleanup_err != ESP_OK) err = cleanup_err;
    }
    cleanup_err = spi_bus_free(host.slot);
    if (err == ESP_OK && cleanup_err != ESP_OK) err = cleanup_err;
    if (err == ESP_OK) {
        s_dev_first_boot_preflight = first_boot;
        s_dev_preflight_passed = true;
    }
    return err;
}

bool sd_mount_dev_card_matches(uint32_t expected_cid_serial,
                               uint64_t expected_sector_count) {
    return sd_mount_is_mounted() && !s_usb_release_requested &&
           expected_sector_count > 0u &&
           dev_sd_same_selected_card(&s_card, expected_cid_serial,
                                     expected_sector_count);
}

esp_err_t sd_mount_dev_rebaseline_selected(bool owner_grant_consumed,
                                           uint32_t expected_cid_serial,
                                           uint64_t expected_sector_count) {
    sdmmc_host_t host = SDSPI_HOST_DEFAULT();
    sdspi_device_config_t dev = SDSPI_DEVICE_CONFIG_DEFAULT();
    spi_bus_config_t bus = {
        .mosi_io_num = RECORDER_SD_MOSI_PIN,
        .miso_io_num = RECORDER_SD_MISO_PIN,
        .sclk_io_num = RECORDER_SD_CLK_PIN,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4096,
    };
    esp_vfs_fat_mount_config_t fs = {
        .format_if_mount_failed = false, // absolutely no implicit erase
        .max_files = 4,
        .allocation_unit_size = 16 * 1024,
    };
    sdmmc_card_t *card = NULL;
    bool mounted = false;
    esp_err_t err, cleanup_err;

    if (!owner_grant_consumed || expected_sector_count == 0u ||
        sd_mount_is_mounted() || s_storage != NULL || s_bus_init ||
        s_sdspi_init || s_device_init || s_msc_driver_init) {
        return ESP_ERR_INVALID_STATE;
    }
    // Do NOT create a TinyUSB MSC driver/storage or publish a USB LUN here.
    err = spi_bus_initialize(host.slot, &bus, SDSPI_DEFAULT_DMA);
    if (err != ESP_OK) return err;
    dev.host_id = host.slot;
    dev.gpio_cs = RECORDER_SD_CS_PIN;
    err = esp_vfs_fat_sdspi_mount(RECORDER_SD_MOUNT_POINT,
                                 &host, &dev, &fs, &card);
    if (err != ESP_OK) goto cleanup;
    mounted = true;

    if (!dev_sd_same_selected_card(card, expected_cid_serial,
                                   expected_sector_count)) {
        err = ESP_ERR_INVALID_STATE;
        goto cleanup;
    }
    // Explicit erase, never a fallback for ordinary mount/recovery failure.
    err = esp_vfs_fat_sdcard_format(RECORDER_SD_MOUNT_POINT, card);
    if (err != ESP_OK) goto cleanup;
    if (!dev_sd_empty_root()) {
        err = ESP_ERR_INVALID_STATE;
        goto cleanup;
    }
    // Tear down the FAT/VFS view; a second independent registration/readback
    // checks that the empty filesystem actually survives close/remount.
    err = esp_vfs_fat_sdcard_unmount(RECORDER_SD_MOUNT_POINT, card);
    if (err != ESP_OK) goto cleanup;
    mounted = false;
    card = NULL;
    err = esp_vfs_fat_sdspi_mount(RECORDER_SD_MOUNT_POINT,
                                 &host, &dev, &fs, &card);
    if (err != ESP_OK) goto cleanup;
    mounted = true;
    if (!dev_sd_same_selected_card(card, expected_cid_serial,
                                   expected_sector_count) ||
        !dev_sd_empty_root()) err = ESP_ERR_INVALID_STATE;

cleanup:
    if (mounted) {
        cleanup_err = esp_vfs_fat_sdcard_unmount(RECORDER_SD_MOUNT_POINT, card);
        if (err == ESP_OK && cleanup_err != ESP_OK) err = cleanup_err;
    }
    cleanup_err = spi_bus_free(host.slot);
    if (err == ESP_OK && cleanup_err != ESP_OK) err = cleanup_err;
    // Success proves only an empty reformatted FAT selected by this helper.
    // Canonical identity/index, generation/NVS and RTC readback are
    // separate required steps before DEV_READY_UNSPENT may be persisted.
    return err;
}
#else
esp_err_t sd_mount_dev_verify_selected_baseline(uint32_t serial,
                                                 uint64_t sectors,
                                                 const char *generation,
                                                 const char *device_id,
                                                 const char *binding,
                                                 bool first_boot) {
    (void)serial; (void)sectors; (void)generation; (void)device_id;
    (void)binding; (void)first_boot;
    return ESP_ERR_NOT_SUPPORTED;
}
bool sd_mount_dev_card_matches(uint32_t expected_cid_serial,
                               uint64_t expected_sector_count) {
    (void)expected_cid_serial; (void)expected_sector_count;
    return false;
}
esp_err_t sd_mount_dev_rebaseline_selected(bool owner_grant_consumed,
                                           uint32_t expected_cid_serial,
                                           uint64_t expected_sector_count) {
    (void)owner_grant_consumed;
    (void)expected_cid_serial;
    (void)expected_sector_count;
    return ESP_ERR_NOT_SUPPORTED;
}
#endif

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
    for (i = 0; i < 10u; ++i) {
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

esp_err_t sd_mount_transfer_to_usb(void) {
    bool transfer_ready;
    bool release_ready;
    esp_err_t err;

    // Recorder finalization and Device filesystem access release are already
    // proven before this detached ownership switch. The esp_tinyusb storage
    // object still carries the APP mount until the synchronous mount-point
    // transition below retires FAT/VFS and emits MOUNT_COMPLETE.
    portENTER_CRITICAL(&s_owner_lock);
    transfer_ready = s_mounted && s_usb_release_requested &&
                     s_device_fs_released;
    portEXIT_CRITICAL(&s_owner_lock);
    if (!transfer_ready || s_storage == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    err = tinyusb_msc_set_storage_mount_point(
        s_storage, TINYUSB_MSC_STORAGE_MOUNT_USB);
    if (err != ESP_OK) {
        return err;
    }

    // Accept the transfer only after MOUNT_COMPLETE has retired the remaining
    // APP mount. Recorder release proof is a precondition, not a callback side
    // effect, so detached publication cannot bypass it.
    portENTER_CRITICAL(&s_owner_lock);
    release_ready = !s_mounted && s_usb_release_requested &&
                    s_device_fs_released;
    portEXIT_CRITICAL(&s_owner_lock);
    if (!release_ready) {
        return ESP_ERR_INVALID_STATE;
    }
    return ESP_OK;
}

esp_err_t sd_mount_release_usb_storage(void) {
    bool release_ready;
    esp_err_t err;

    portENTER_CRITICAL(&s_owner_lock);
    release_ready = !s_mounted && s_usb_release_requested &&
                    s_device_fs_released;
    portEXIT_CRITICAL(&s_owner_lock);
    if (!release_ready || s_storage == NULL || !s_msc_driver_init) {
        return ESP_ERR_INVALID_STATE;
    }

    // tinyusb_msc_delete_storage() refuses deletion while deferred host writes
    // remain. Success both destroys the USB LUN/backend and proves the pending
    // write count is zero. Strategy 2 deliberately does not create APP storage.
    err = tinyusb_msc_delete_storage(s_storage);
    if (err != ESP_OK) {
        ESP_LOGE(TAG,
                 "stage: usb, result: error, reason: deferred host io");
        return err;
    }
    s_storage = NULL;

    err = tinyusb_msc_uninstall_driver();
    if (err != ESP_OK) {
        ESP_LOGE(TAG,
                 "stage: usb, result: error, reason: msc driver release");
        return err;
    }
    s_msc_driver_init = false;
    ESP_LOGI(TAG,
             "stage: usb, result: storage-released, owner: none, mount: none");
    return ESP_OK;
}

void sd_mount_note_usb_owned(void) {
    portENTER_CRITICAL(&s_owner_lock);
    if (s_usb_release_requested && s_device_fs_released) {
        s_mounted = false;
    }
    portEXIT_CRITICAL(&s_owner_lock);
}

void sd_mount_unmount(void) {
    bool usb_release;
    esp_err_t err;

    portENTER_CRITICAL(&s_owner_lock);
    usb_release = s_usb_release_requested;
    if (usb_release) {
        s_device_fs_released = true;
    }
    portEXIT_CRITICAL(&s_owner_lock);
    if (usb_release) {
        ESP_LOGI(TAG,
                 "stage: usb, result: device-fs-released, mount: app");
        return;
    }

    if (s_storage != NULL) {
        (void)tinyusb_msc_set_storage_callback(sd_mount_terminal_storage_event,
                                               NULL);
        err = tinyusb_msc_delete_storage(s_storage);
        if (err != ESP_OK) {
            ESP_LOGE(TAG,
                     "stage: record, result: error, reason: sd storage delete");
            return;
        }
        s_storage = NULL;
    }
    if (s_msc_driver_init) {
        err = tinyusb_msc_uninstall_driver();
        if (err != ESP_OK) {
            ESP_LOGE(TAG,
                     "stage: record, result: error, reason: msc driver delete");
            return;
        }
        s_msc_driver_init = false;
    }
    portENTER_CRITICAL(&s_owner_lock);
    s_mounted = false;
    s_device_fs_released = true;
    portEXIT_CRITICAL(&s_owner_lock);
    sd_mount_raw_cleanup();
}

#else

esp_err_t sd_mount_recordings(void) { return ESP_ERR_NOT_SUPPORTED; }
esp_err_t sd_mount_ensure_date_dir(const char *date_yyyy_mm_dd) {
    (void)date_yyyy_mm_dd;
    return ESP_ERR_NOT_SUPPORTED;
}
esp_err_t sd_mount_ensure_quarantine_dir(void) { return ESP_ERR_NOT_SUPPORTED; }
bool sd_mount_is_mounted(void) { return false; }
esp_err_t sd_mount_release_for_usb(void) { return ESP_ERR_NOT_SUPPORTED; }
bool sd_mount_device_fs_released(void) { return false; }
esp_err_t sd_mount_transfer_to_usb(void) { return ESP_ERR_NOT_SUPPORTED; }
esp_err_t sd_mount_release_usb_storage(void) { return ESP_ERR_NOT_SUPPORTED; }
void sd_mount_note_usb_owned(void) {}
void sd_mount_unmount(void) {}

#endif
