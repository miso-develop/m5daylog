#pragma once

// microSD SPI ownership + recording-directory bring-up.
//
// Task #49 makes the esp_tinyusb MSC storage object the single FAT/VFS owner
// switch. Task #87 disables esp_tinyusb automatic ownership switching so a
// suspend/bus-loss observation can only trigger an explicit quiescence barrier.

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

// Arm an APP -> USB handoff. The recorder keeps VFS access until its normal
// terminal path calls sd_mount_unmount() after finalize + manifest commit.
// At that point no Device file operation is allowed, but the storage object is
// kept alive until the explicit ownership transfer runs.
esp_err_t sd_mount_release_for_usb(void);
bool sd_mount_device_fs_released(void);

// Task #87 explicit ownership transitions. APP -> USB is allowed only after
// the recorder has logically released all Device filesystem access. USB -> APP
// is allowed only after the USB device stack is quiesced; it deletes the old
// USB-owned storage object first, so esp_tinyusb's deferred-write guard must
// prove there is no queued host write before a new APP-owned storage is built.
esp_err_t sd_mount_transfer_to_usb(void);
esp_err_t sd_mount_transfer_to_app(void);

// Called only from the esp_tinyusb storage callback after its physical
// unmount/remount completes.
void sd_mount_note_usb_owned(void);
void sd_mount_note_app_owned(void);

// Verify the post-USB application mount and recreate required directories.
esp_err_t sd_mount_remount_after_usb(void);

// Terminal teardown. During an armed USB handoff this becomes the recorder's
// logical release point instead of physically destroying the SD transport.
void sd_mount_unmount(void);

#ifdef __cplusplus
}
#endif
