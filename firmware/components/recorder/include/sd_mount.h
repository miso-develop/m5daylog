#pragma once

// microSD SPI ownership + recording-directory bring-up.
//
// Task #49 makes the esp_tinyusb MSC storage object the single FAT/VFS owner
// switch. The SD card and SPI transport stay initialized across an APP -> USB
// -> APP cycle; only the filesystem registration moves. This prevents the
// recorder and the PC from mounting/writing the same filesystem concurrently.

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
// At that point no Device file operation is allowed, but physical unmount is
// left to esp_tinyusb's blocked MOUNT_START callback.
esp_err_t sd_mount_release_for_usb(void);
bool sd_mount_device_fs_released(void);

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
