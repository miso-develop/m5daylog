#pragma once

// M5Capsule v1.1 power/battery monitor — Task #48 (IM-011).
//
// HOLD G46 must be asserted after wake to keep battery-powered execution
// alive. VBAT is exposed through a 2:1 divider to GPIO6 / ADC1. Task #48
// uses this native ESP-IDF wrapper instead of pulling full M5Unified into
// the recorder. The low-battery threshold remains configurable for PoC
// discharge-curve recalibration (Decision #30).

#ifdef ESP_PLATFORM
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t recorder_power_enable_hold(void);
esp_err_t recorder_power_init(void);
esp_err_t recorder_power_read_battery_mv(int *battery_mv);
void recorder_power_deinit(void);

#ifdef __cplusplus
}
#endif
#endif  // ESP_PLATFORM
