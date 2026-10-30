#pragma once

// #144: extra release-cycle authorization, independent of recording UUID.
// This does not authorize a RELEASE_STORAGE command on its own.
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

// Once per process boot, after physical WAKE sampling and lifecycle NVS init.
// Only a fresh durable SHUTDOWN_ARMED + previously ready (supervised) fence
// can consume one candidate permit. An error disables this boot's candidate,
// but the existing recorder/USB recovery policy remains independent.
esp_err_t release_cycle_fence_begin_boot(bool manual_wake);

// Called only after #87 manual-WAKE recovery has durably reached NORMAL,
// recorder reached RECORDING and RTC recovery checks succeeded. This does
// not authorize publication by itself.
esp_err_t release_cycle_fence_note_recorder_ready(void);

// RAM-only completed-boot origin witness. NOT sufficient for GET_STATUS: a separate finalized,
// durably indexed, current-boot nonzero-PCM manifest proof is also required.
bool release_cycle_fence_boot_qualified(void);

// Call once after PC-owned USB_SYNC and durable HOST_UNRESOLVED are proven.
// It cannot be invoked simply because a stale boot marker was found.
esp_err_t release_cycle_fence_note_host_owned(void);

// Call only AFTER #87 has completed release quiescence, committed
// SHUTDOWN_ARMED, and proven the preceding host release was accepted.
// A failed write never grants the following boot a new candidate.
esp_err_t release_cycle_fence_note_release_completed(void);

// There is intentionally NO unattended enrollment/reset API. A missing or
// invalid persistent key is unqualified pending a separately authorized
// supervised first-use/recovery protocol.
#ifdef __cplusplus
}
#endif
