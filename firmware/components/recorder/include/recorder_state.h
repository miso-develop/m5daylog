#pragma once

// Portable recorder lifecycle / failure-state contract — Task #48 (IM-011).
//
// Spec #36 defines the externally meaningful PoC states. This module keeps
// state transitions explicit and testable without ESP-IDF dependencies so
// firmware code cannot silently continue to report RECORDING after a fatal
// SD/mic/DMA failure. USB_* states are defined here for the lifecycle
// contract, while Task #49 owns actual USB detection/MSC ownership changes.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    RECORDER_STATE_BOOT = 0,
    RECORDER_STATE_RECOVER,
    RECORDER_STATE_RECORDING,
    RECORDER_STATE_USB_PREPARE,
    RECORDER_STATE_USB_SYNC,
    RECORDER_STATE_REMOUNT,
    RECORDER_STATE_LOW_BATTERY_STOP,
    RECORDER_STATE_ERROR,
} recorder_state_t;

typedef enum {
    RECORDER_REASON_NONE = 0,
    RECORDER_REASON_BOOT,
    RECORDER_REASON_RECOVERY,
    RECORDER_REASON_SD_MOUNT,
    RECORDER_REASON_SD_FULL,
    RECORDER_REASON_SD_WRITE,
    RECORDER_REASON_SD_FLUSH,
    RECORDER_REASON_MIC_INIT,
    RECORDER_REASON_I2S_READ,
    RECORDER_REASON_DMA_OVERRUN,
    RECORDER_REASON_BUFFER_OVERFLOW,
    RECORDER_REASON_LOW_BATTERY,
    RECORDER_REASON_MANIFEST,
    RECORDER_REASON_FINALIZE,
    RECORDER_REASON_INTERNAL,
    RECORDER_REASON_USB,
} recorder_reason_t;

typedef struct {
    recorder_state_t state;
    recorder_reason_t reason;
    uint32_t transition_count;
} recorder_state_machine_t;

void recorder_state_machine_init(recorder_state_machine_t *machine);

// Apply one legal lifecycle transition. ERROR is terminal and may be entered
// from any state except ERROR itself; this allows a finalize failure to
// supersede a previously selected LOW_BATTERY_STOP. Illegal transitions
// leave the machine untouched.
bool recorder_state_transition(recorder_state_machine_t *machine,
                               recorder_state_t next,
                               recorder_reason_t reason);

const char *recorder_state_str(recorder_state_t state);
const char *recorder_reason_str(recorder_reason_t reason);

// Format one diagnostics JSON line. `timestamp` should be an offset ISO-8601
// string when available; NULL/empty is encoded as null. `battery_mv` <= 0 is
// encoded as null. The output always ends with '\n' when successful.
bool recorder_state_format_event(char *out, size_t out_size,
                                 const char *timestamp,
                                 recorder_state_t from,
                                 recorder_state_t to,
                                 recorder_reason_t reason,
                                 int battery_mv);

// Best-effort append to events.jsonl. Never truncates or replaces existing
// diagnostics. Returns false when the file cannot be opened/written/flushed.
bool recorder_state_append_event(const char *path,
                                 const char *timestamp,
                                 recorder_state_t from,
                                 recorder_state_t to,
                                 recorder_reason_t reason,
                                 int battery_mv);

#ifdef __cplusplus
}
#endif
