#pragma once
// #147 SDB-1.1 DEV-only *candidate* one-use boot-intent NVS transaction.
//
// This is not an installer, approval receipt, SD formatter, RTC proof or
// release authority. A caller must separately prove its selected exact
// hardware/APP/partition/SD baseline and consumed external owner grant.
// Never invoke on ordinary boot or infer authority from erased NVS.
#include <stdbool.h>
#ifdef ESP_PLATFORM
#include "esp_err.h"
#else
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_ERR_NOT_SUPPORTED 5
#endif
#ifdef __cplusplus
extern "C" {
#endif
// Explicitly pinned G, deviceId, and lower-case SHA-256 baseline binding
// must match durable NVS. Caller supplies a genuinely measured continuous
// 10s current-boot physical GPIO hold (not host-provided evidence).
// READY -> STAGED -> USED + BOOT_ACTIVE is single-attempt and fail-closed.
// Only a successful full readback sets a candidate RAM latch; another module
// must still prove RTC/recorder/ownership before any status pair/release.
esp_err_t dev_sdb11_consume_boot_intent(const char *expected_generation,
                                       const char *expected_device_id,
                                       const char *expected_binding_sha256,
                                       bool physical_hold_proven);
bool dev_sdb11_boot_intent_consumed_this_boot(void);
#ifdef __cplusplus
}
#endif
