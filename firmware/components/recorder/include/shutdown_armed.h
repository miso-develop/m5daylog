#pragma once

#include <stdbool.h>

#ifdef ESP_PLATFORM
#include "esp_err.h"
#else
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL 1
#define ESP_ERR_INVALID_ARG 2
#define ESP_ERR_NOT_SUPPORTED 5
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    SHUTDOWN_ARMED_BOOT_NORMAL = 0,
    SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN,
    SHUTDOWN_ARMED_BOOT_MANUAL_RESUME,
} shutdown_armed_boot_action_t;

// Persistent Strategy 2 shutdown intent. A missing key means a normal
// first/legacy boot. Invalid or unreadable persistent state fails closed.
esp_err_t shutdown_armed_read(bool *armed);
esp_err_t shutdown_armed_commit(void);
esp_err_t shutdown_armed_clear(void);

// Classify this boot without mutating the persistent marker. An armed boot may
// resume only when the physical WAKE button is asserted; RTC/reset/USB-power
// boots stay functionally shut down. The caller clears the marker only after
// Device ownership and pending recovery have completed durably.
esp_err_t shutdown_armed_boot_action(bool manual_wake,
                                     shutdown_armed_boot_action_t *action);

#ifdef __cplusplus
}
#endif
