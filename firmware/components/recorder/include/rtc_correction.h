#pragma once

// Task #50: RTC correction transaction used by USB CDC SET_TIME.
//
// The RTC/system clock mutation and the NVS pending record form one logical
// operation. microSD is deliberately never touched by rtc_correction_apply();
// the pending record is flushed to events.jsonl only from #87's supported
// manual-WAKE Device-owned recovery boundary after SD mount.

#include <stdbool.h>
#include <stddef.h>

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

typedef enum {
    RTC_CORRECTION_OK = 0,
    RTC_CORRECTION_INVALID_ARGS,
    RTC_CORRECTION_RANGE_ERROR,
    RTC_CORRECTION_BUSY,
    RTC_CORRECTION_INTERNAL_ERROR,
} rtc_correction_result_t;

// Initialize/refresh only the durable pending-NVS state. This intentionally
// performs no BM8563/I2C access so recorder -> TinyUSB publication remains
// independent of live RTC transport readiness.
esp_err_t rtc_correction_init(void);

// True when one SET_TIME correction is waiting for durable events.jsonl
// persistence. A second SET_TIME must return BUSY while this is true.
bool rtc_correction_is_pending(void);

// Validate an offset-bearing ISO-8601 instant, normalize it to UTC seconds,
// write BM8563 + system time, then commit one pending correction to NVS.
// `normalized` receives `YYYY-MM-DDTHH:MM:SS+00:00` on success.
//
// IMPORTANT: once hardware time has been changed, any later error is an
// indeterminate mutation and is reported INTERNAL_ERROR. Callers must not
// blindly retry it.
rtc_correction_result_t rtc_correction_apply(
    const char *requested_time,
    char *normalized,
    size_t normalized_size);

// Manual-WAKE Device-owned filesystem recovery only. Best-effort restore the
// process clock from BM8563 using a transient I2C session before fresh
// recording. If a pending correction exists, append exactly one durable
// rtc_correction JSONL event and only then clear its NVS record. Replaying
// after a crash is duplicate-safe by correctionId.
esp_err_t rtc_correction_flush_pending_event(const char *events_path);

#ifdef __cplusplus
}
#endif
