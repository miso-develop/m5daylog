#pragma once

// M5Capsule v1.1 battery monitor — Task #48 (IM-011).
//
// The board exposes VBAT through a 2:1 divider to GPIO6 / ADC1. Task #48
// uses this native ESP-IDF wrapper instead of pulling the full M5Unified
// dependency into the recorder. The low-battery threshold itself lives in
// recorder_config.h so it can be recalibrated after the PoC discharge curve
// is measured (Decision #30).

#ifdef ESP_PLATFORM
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t recorder_power_init(void);
esp_err_t recorder_power_read_battery_mv(int *battery_mv);
void recorder_power_deinit(void);

#ifdef __cplusplus
}
#endif
#endif  // ESP_PLATFORM
