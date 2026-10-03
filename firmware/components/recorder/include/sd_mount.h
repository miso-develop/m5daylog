#pragma once

// microSD SPI ownership + recording-directory bring-up.
//
// Task #49 uses esp_tinyusb's storage object as the one FAT/VFS owner switch.
// Task #87 Strategy 2 allows APP -> USB only after recorder publication proof;
// reverse release destroys the USB storage after quiescence and deliberately
// leaves Device FAT/VFS unmounted until a later manual-WAKE fresh boot.

#include <stdbool.h>

#ifdef ESP_PLATFORM
#include "esp_err.h"
#else
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL 1
#define ESP_ERR_INVALID_ARG 2
#define ESP_ERR_INVALID_STATE 3
#define ESP_ERR_NOT_SUPPORTED 5
#endif

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t sd_mount_recordings(void);
esp_err_t sd_mount_ensure_date_dir(const char *date_yyyy_mm_dd);
esp_err_t sd_mount_ensure_quarantine_dir(void);
bool sd_mount_is_mounted(void);

esp_err_t sd_mount_release_for_usb(void);
bool sd_mount_device_fs_released(void);
esp_err_t sd_mount_transfer_to_usb(void);

// Called only after TinyUSB device teardown. Deletes the USB-owned MSC storage
// object; esp_tinyusb refuses the delete while deferred host writes remain.
// Never creates APP storage and never remounts FAT/VFS.
esp_err_t sd_mount_release_usb_storage(void);

void sd_mount_note_usb_owned(void);
void sd_mount_unmount(void);

#ifdef __cplusplus
}
#endif
