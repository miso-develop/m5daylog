#pragma once

#include <stdbool.h>

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
    SHUTDOWN_ARMED_BOOT_NORMAL = 0,
    SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN,
    SHUTDOWN_ARMED_BOOT_MANUAL_RESUME,
} shutdown_armed_boot_action_t;

// Persistent Strategy 2 lifecycle state uses one NVS byte:
//   0 = normal Device-owned lifecycle
//   1 = release quiesced and SHUTDOWN_ARMED
//   2 = host ownership/release unresolved
// A missing key means a normal first/legacy boot. Invalid or unreadable state
// fails closed. HOST_UNRESOLVED is never treated as an armed manual-resume
// state because explicit eject + quiescence has not been proven yet.
//
// This compatibility read exposes only the ARMED boolean. HOST_UNRESOLVED
// returns ESP_ERR_INVALID_STATE so a caller cannot accidentally collapse it
// into the normal false state.
esp_err_t shutdown_armed_read(bool *armed);

// Commit HOST_UNRESOLVED before APP -> USB publication is admitted. This state
// must survive reset/reboot until release quiescence durably transitions it to
// SHUTDOWN_ARMED.
esp_err_t shutdown_armed_mark_host_unresolved(void);

// Transition the durable lifecycle to SHUTDOWN_ARMED after host I/O is proven
// unreachable and deferred writes/storage release have completed.
esp_err_t shutdown_armed_commit(void);

// Clear SHUTDOWN_ARMED only after manual-WAKE Device ownership and pending
// recovery complete durably. HOST_UNRESOLVED must never be cleared this way.
esp_err_t shutdown_armed_clear(void);

// Classify this boot without mutating the persistent marker. An armed boot may
// resume only when the physical WAKE button is asserted. HOST_UNRESOLVED,
// RTC/reset/USB-power boots, corrupt state, and read failures stay fail-closed.
esp_err_t shutdown_armed_boot_action(bool manual_wake,
                                     shutdown_armed_boot_action_t *action);

#ifdef __cplusplus
}
#endif
