#pragma once

// Task #50 durable rtc_correction JSONL recovery.
//
// This helper is intentionally independent from RTC/NVS hardware. It proves a
// complete matching event is durable before invoking the caller's pending-clear
// callback, making event-durable-before-NVS-clear retries duplicate-safe.

#ifdef ESP_PLATFORM
#include "esp_err.h"
#else
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL 1
#define ESP_ERR_INVALID_ARG 2
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    const char *correction_id;
    const char *before;
    const char *after;
    const char *source;
} rtc_correction_event_t;

typedef esp_err_t (*rtc_correction_pending_clear_fn_t)(void *ctx);

esp_err_t rtc_correction_recover_event(
    const char *events_path,
    const rtc_correction_event_t *event,
    rtc_correction_pending_clear_fn_t clear_pending,
    void *clear_ctx);

#ifdef __cplusplus
}
#endif
