#pragma once

// M5Capsule v1.1 RGB status indication — Task #48 (IM-011).
//
// Decision #9 keeps RGB off during normal recording; ERROR and
// LOW_BATTERY_STOP are explicit exceptions. The v1.1 board powers the RGB
// rail through GPIO38 and drives the WS2812-compatible pixel from GPIO21.

#include "recorder_state.h"

#ifdef ESP_PLATFORM
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t recorder_status_led_init(void);
esp_err_t recorder_status_led_set_state(recorder_state_t state);
void recorder_status_led_deinit(void);

#ifdef __cplusplus
}
#endif
#endif  // ESP_PLATFORM
