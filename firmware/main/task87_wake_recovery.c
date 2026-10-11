#include "task87_wake_recovery.h"

#include <stddef.h>

#include "shutdown_armed.h"

// Task #50 supplies this symbol when integrated. The weak hook keeps Task #87
// independent while preserving pending-RTC recovery before the durable
// wake-recovery transition.
extern esp_err_t rtc_correction_flush_pending_event(const char *events_path)
    __attribute__((weak));

void task87_wake_recovery_init(task87_wake_recovery_t *recovery,
                               bool manual_resume) {
    if (recovery == NULL) {
        return;
    }
    recovery->manual_resume_pending = manual_resume;
    recovery->device_recovery_complete = false;
    recovery->fresh_recording_started = false;
}

bool task87_wake_recovery_pending(const task87_wake_recovery_t *recovery) {
    return recovery != NULL && recovery->manual_resume_pending;
}

esp_err_t task87_wake_recovery_complete_device_recovery(
    task87_wake_recovery_t *recovery,
    const char *events_path) {
    esp_err_t err;

    if (recovery == NULL || events_path == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!recovery->manual_resume_pending ||
        recovery->device_recovery_complete) {
        return ESP_ERR_INVALID_STATE;
    }

    if (rtc_correction_flush_pending_event != NULL) {
        err = rtc_correction_flush_pending_event(events_path);
        if (err != ESP_OK) {
            return err;
        }
    }

    err = shutdown_armed_mark_wake_recovery_pending();
    if (err != ESP_OK) {
        return err;
    }

    recovery->device_recovery_complete = true;
    return ESP_OK;
}

esp_err_t task87_wake_recovery_note_recording_started(
    task87_wake_recovery_t *recovery,
    bool fresh_recording_id_present) {
    esp_err_t err;

    if (recovery == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!recovery->manual_resume_pending) {
        return ESP_OK;
    }
    if (!recovery->device_recovery_complete ||
        !fresh_recording_id_present) {
        return ESP_ERR_INVALID_STATE;
    }
    err = shutdown_armed_complete_wake_recovery();
    if (err != ESP_OK) {
        return err;
    }

    recovery->fresh_recording_started = true;
    recovery->manual_resume_pending = false;
    return ESP_OK;
}

bool task87_wake_recovery_requires_shutdown(
    const task87_wake_recovery_t *recovery) {
    return recovery != NULL && recovery->manual_resume_pending;
}

bool task87_wake_recovery_usb_rearm_allowed(
    const task87_wake_recovery_t *recovery) {
    return recovery != NULL && !recovery->manual_resume_pending;
}
