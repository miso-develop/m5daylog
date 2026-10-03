#pragma once

// M5Capsule v1.1 power/battery monitor and Strategy 2 shutdown controls.

#ifdef ESP_PLATFORM
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t recorder_power_enable_hold(void);
esp_err_t recorder_power_release_hold(void);
esp_err_t recorder_power_manual_wake_asserted(bool *asserted);
esp_err_t recorder_power_init(void);
esp_err_t recorder_power_read_battery_mv(int *battery_mv);
void recorder_power_deinit(void);

// Functional shutdown while USB power remains. HOLD is expected to have been
// released first; no automatic wake source is armed here.
void recorder_power_enter_shutdown_sleep(void);

#ifdef __cplusplus
}
#endif
#endif  // ESP_PLATFORM
