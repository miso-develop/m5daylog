#pragma once

// microSD SPI mount + Task #44 directory bring-up.
//
// Mounts the microSD at RECORDER_SD_MOUNT_POINT ("/sdcard") over the
// M5Capsule v1.1 SPI bus (CS 11 / MOSI 12 / CLK 14 / MISO 39, see
// recorder_config.h) and creates ONLY the directories required for live
// recording (`/sdcard/M5DAYLOG` + `/sdcard/M5DAYLOG/recordings`).
// Rotation/finalize, recovery, and retention bookkeeping are later
// Tasks and are never created here.
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
#define ESP_ERR_INVALID_STATE 3
#define ESP_ERR_NOT_SUPPORTED 5
#endif

#ifdef __cplusplus
extern "C" {
#endif

// Mount the card and ensure the Task #44 recording directories exist.
// Safe to call when already mounted (verifies directories, returns ESP_OK).
esp_err_t sd_mount_recordings(void);

// True after a successful sd_mount_recordings() until unmount.
bool sd_mount_is_mounted(void);

// Unmount when mounted; no-op otherwise. Recording-path teardown only —
// never deletes or renames audio files.
void sd_mount_unmount(void);

#ifdef __cplusplus
}
#endif
