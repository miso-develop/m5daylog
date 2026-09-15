#pragma once

// microSD SPI mount + Tasks #44/#45/#46 directory bring-up.
//
// Mounts the microSD at RECORDER_SD_MOUNT_POINT ("/sdcard") over the
// M5Capsule v1.1 SPI bus (CS 11 / MOSI 12 / CLK 14 / MISO 39, see
// recorder_config.h) and creates the directories required for live
// recording (`/sdcard/M5DAYLOG` + `/sdcard/M5DAYLOG/recordings`) plus,
// via sd_mount_ensure_date_dir(), per-date subdirectories
// (`recordings/YYYY-MM-DD/`) for Task #45 midnight rotation, and the
// Task #46 quarantine directory (`/sdcard/M5DAYLOG/quarantine/`) for
// power-loss recovery isolation. Later retention state is never
// created here.
//
// Fail-loud: every failure returns non-ESP_OK so the caller enters ERROR
// instead of a silent "recording" state (Spec #36). The card is never
// formatted on mount failure — unacknowledged audio must not be destroyed.

#include <stdbool.h>

#ifdef ESP_PLATFORM
#include "esp_err.h"
#else
// Host/test builds: minimal stand-ins so headers stay includable.
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

// Mount the card and ensure the Tasks #44/#45/#46 directories exist
// (recordings + quarantine). Safe to call when already mounted
// (verifies directories, returns ESP_OK).
esp_err_t sd_mount_recordings(void);

// Ensure the per-date subdirectory `recordings/YYYY-MM-DD/` exists for
// Task #45 rotation (midnight never mixes dates into one file).
// date_yyyy_mm_dd must be "YYYY-MM-DD" (10 chars). Requires a prior
// successful sd_mount_recordings(); otherwise ESP_ERR_INVALID_STATE.
// Fail-loud: mkdir errors (other than EEXIST) return ESP_FAIL so the
// caller enters ERROR instead of recording without a directory.
esp_err_t sd_mount_ensure_date_dir(const char *date_yyyy_mm_dd);

// Ensure the Task #46 quarantine directory
// (`/sdcard/M5DAYLOG/quarantine/`) exists for power-loss recovery
// isolation. Requires a prior successful sd_mount_recordings();
// otherwise ESP_ERR_INVALID_STATE. Quarantine files are never
// auto-deleted. Fail-loud like the date-dir helper.
esp_err_t sd_mount_ensure_quarantine_dir(void);

// True after a successful sd_mount_recordings() until unmount.
bool sd_mount_is_mounted(void);

// Unmount when mounted; no-op otherwise. Recording-path teardown only —
// never deletes audio files (finalize renames via wav_rotation only).
void sd_mount_unmount(void);

#ifdef __cplusplus
}
#endif
