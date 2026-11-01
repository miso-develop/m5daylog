#pragma once
// #147 SDB-1.1: one-board/one-original-card DEV-only, destructive INSTALLER.
// This is not a release/USB protocol and must NOT be callable from normal
// recording. The operator's verified exact ROM/image/partition/card target,
// cutoff of old PC writers/MSC, approval and independently SPENT one-use
// installation grant are prerequisites OUTSIDE this firmware.
#include <stdbool.h>
#include <stdint.h>
#ifdef ESP_PLATFORM
#include "esp_err.h"
#else
typedef int esp_err_t;
#endif
#ifdef __cplusplus
extern "C" {
#endif
// Only the separately compiled installer image invokes this after an
// ISR-backed current-boot continuous GPIO42 10-second hold. Never retries.
// Format/metadata/NVS failures leave the new generation NOT READY.
esp_err_t dev_sdb11_install_baseline(const char *generation,
                                     const char *device_id,
                                     const char *baseline_sha256,
                                     uint32_t expected_cid_serial,
                                     uint64_t expected_sector_count,
                                     bool owner_grant_consumed);
#ifdef __cplusplus
}
#endif
