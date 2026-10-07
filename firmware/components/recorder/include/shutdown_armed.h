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
//   3 = manual-WAKE recovery completed but fresh recording is not yet proven
// A missing key means a normal first/legacy boot. Invalid or unreadable state
// fails closed. HOST_UNRESOLVED is never treated as a manual-resume state
// because explicit eject + quiescence has not been proven yet.
//
// This compatibility read reports both ARMED and WAKE_RECOVERY_PENDING as
// gated/armed=true. HOST_UNRESOLVED returns ESP_ERR_INVALID_STATE so a caller
// cannot accidentally collapse unresolved host ownership into normal state.
esp_err_t shutdown_armed_read(bool *armed);

// Commit HOST_UNRESOLVED before APP -> USB publication is admitted. This state
// must survive reset/reboot until release quiescence durably transitions it to
// SHUTDOWN_ARMED.
esp_err_t shutdown_armed_mark_host_unresolved(void);

// Transition the durable lifecycle to SHUTDOWN_ARMED after host I/O is proven
// unreachable and deferred writes/storage release have completed.
esp_err_t shutdown_armed_commit(void);

// After a manual-WAKE boot has reacquired Device ownership and completed all
// pending recovery, replace SHUTDOWN_ARMED with a durable fail-closed recovery
// marker. This is idempotent across a reset while the marker is already pending.
esp_err_t shutdown_armed_mark_wake_recovery_pending(void);

// Clear the durable recovery gate only after this boot has proven a fresh
// recording session. A reset before this succeeds remains fail-closed.
esp_err_t shutdown_armed_complete_wake_recovery(void);

// Legacy direct ARMED -> NORMAL transition. Strategy 2 manual-WAKE recovery
// must use the two functions above so no reboot window becomes NORMAL before
// fresh-recording proof.
esp_err_t shutdown_armed_clear(void);

// Classify this boot without mutating the persistent marker. ARMED and
// WAKE_RECOVERY_PENDING may resume only when the physical WAKE button is
// asserted. HOST_UNRESOLVED, non-manual reset/USB-power boots, corrupt state,
// and read failures stay fail-closed.
esp_err_t shutdown_armed_boot_action(bool manual_wake,
                                     shutdown_armed_boot_action_t *action);

#ifdef __cplusplus
}
#endif
