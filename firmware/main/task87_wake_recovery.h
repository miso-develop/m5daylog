#pragma once

#include <stdbool.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Per-boot manual-WAKE recovery proof. This is intentionally volatile: the
// durable authorization boundary remains shutdown_armed. app_main initializes
// this context from the boot action on every fresh boot, so stale USB/session
// proof cannot survive a reset.
typedef struct {
    bool manual_resume_pending;
    bool device_recovery_complete;
    bool fresh_recording_started;
} task87_wake_recovery_t;

void task87_wake_recovery_init(task87_wake_recovery_t *recovery,
                               bool manual_resume);

bool task87_wake_recovery_pending(const task87_wake_recovery_t *recovery);

// Called only after Device FAT/VFS ownership is established and the normal
// recovery scan + manifest sync have completed. Task #50's pending RTC event is
// flushed first (when linked), then SHUTDOWN_ARMED is replaced by a durable
// WAKE_RECOVERY_PENDING gate. Failure leaves the per-boot and durable lifecycle
// fail-closed.
esp_err_t task87_wake_recovery_complete_device_recovery(
    task87_wake_recovery_t *recovery,
    const char *events_path);

// Called when the recorder has actually reached RECORDING. A manual-WAKE boot
// is accepted only if device recovery completed and main.c produced a non-empty
// fresh recordingId for this boot. Only then is the durable recovery gate
// cleared to NORMAL; until then reset and USB publication remain fail-closed.
esp_err_t task87_wake_recovery_note_recording_started(
    task87_wake_recovery_t *recovery,
    bool fresh_recording_id_present);

// A manual-WAKE boot remains a shutdown-on-start-failure path until a fresh
// recording session has been proven.
bool task87_wake_recovery_requires_shutdown(
    const task87_wake_recovery_t *recovery);

// Normal boots may publish USB after RECORDING as before. Manual-WAKE boots may
// not rearm USB until recovery + fresh recording proof have completed.
bool task87_wake_recovery_usb_rearm_allowed(
    const task87_wake_recovery_t *recovery);

#ifdef __cplusplus
}
#endif
