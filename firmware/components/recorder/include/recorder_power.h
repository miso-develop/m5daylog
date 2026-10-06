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

// Enter Strategy-2 functional shutdown. This first disables all ESP sleep
// wake sources, then releases battery HOLD and enters deep sleep. Preparation
// failure returns an error and deliberately does not sleep; on hardware a
// successful esp_deep_sleep_start() does not return.
esp_err_t recorder_power_shutdown(void);

#ifdef __cplusplus
}
#endif
#endif  // ESP_PLATFORM
