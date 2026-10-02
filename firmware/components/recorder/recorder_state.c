#include "recorder_state.h"

#include <stdio.h>
#include <string.h>

static bool recorder_state_is_terminal(recorder_state_t state) {
    return state == RECORDER_STATE_LOW_BATTERY_STOP ||
           state == RECORDER_STATE_ERROR;
}

static bool recorder_state_transition_allowed(recorder_state_t from,
                                              recorder_state_t to) {
    // ERROR is terminal and the first recorded terminal cause is immutable.
    // Re-entering ERROR must not be accepted as a same-state reason update.
    if (from == RECORDER_STATE_ERROR) {
        return false;
    }
    if (from == to) {
        return true;
    }
    // ERROR is the absolute terminal state. A failure while completing a
    // previously selected LOW_BATTERY_STOP (for example finalize failure)
    // must supersede the safe-stop state instead of falsely reporting that
    // the WAV was closed safely.
    if (to == RECORDER_STATE_ERROR) {
        return true;
    }
    if (recorder_state_is_terminal(from)) {
        return false;
    }
    switch (from) {
        case RECORDER_STATE_BOOT:
            return to == RECORDER_STATE_RECOVER;
        case RECORDER_STATE_RECOVER:
            return to == RECORDER_STATE_RECORDING;
        case RECORDER_STATE_RECORDING:
            return to == RECORDER_STATE_USB_PREPARE ||
                   to == RECORDER_STATE_LOW_BATTERY_STOP;
        case RECORDER_STATE_USB_PREPARE:
            return to == RECORDER_STATE_USB_SYNC;
        case RECORDER_STATE_USB_SYNC:
            return to == RECORDER_STATE_REMOUNT;
        case RECORDER_STATE_REMOUNT:
            return to == RECORDER_STATE_RECOVER;
        case RECORDER_STATE_LOW_BATTERY_STOP:
        case RECORDER_STATE_ERROR:
        default:
            return false;
    }
}

void recorder_state_machine_init(recorder_state_machine_t *machine) {
    if (machine == NULL) {
        return;
    }
    machine->state = RECORDER_STATE_BOOT;
    machine->reason = RECORDER_REASON_BOOT;
    machine->transition_count = 0;
}

bool recorder_state_transition(recorder_state_machine_t *machine,
                               recorder_state_t next,
                               recorder_reason_t reason) {
    if (machine == NULL ||
        !recorder_state_transition_allowed(machine->state, next)) {
        return false;
    }
    if (machine->state != next) {
        machine->state = next;
        machine->transition_count++;
    }
    machine->reason = reason;
    return true;
}

const char *recorder_state_str(recorder_state_t state) {
    switch (state) {
        case RECORDER_STATE_BOOT:
            return "BOOT";
        case RECORDER_STATE_RECOVER:
            return "RECOVER";
        case RECORDER_STATE_RECORDING:
            return "RECORDING";
        case RECORDER_STATE_USB_PREPARE:
            return "USB_PREPARE";
        case RECORDER_STATE_USB_SYNC:
            return "USB_SYNC";
        case RECORDER_STATE_REMOUNT:
            return "REMOUNT";
        case RECORDER_STATE_LOW_BATTERY_STOP:
            return "LOW_BATTERY_STOP";
        case RECORDER_STATE_ERROR:
            return "ERROR";
        default:
            return "UNKNOWN";
    }
}

const char *recorder_reason_str(recorder_reason_t reason) {
    switch (reason) {
        case RECORDER_REASON_NONE:
            return "none";
        case RECORDER_REASON_BOOT:
            return "boot";
        case RECORDER_REASON_RECOVERY:
            return "recovery";
        case RECORDER_REASON_SD_MOUNT:
            return "sd-mount";
        case RECORDER_REASON_SD_WRITE:
            return "sd-write";
        case RECORDER_REASON_SD_FLUSH:
            return "sd-flush";
        case RECORDER_REASON_MIC_INIT:
            return "mic-init";
        case RECORDER_REASON_I2S_READ:
            return "i2s-read";
        case RECORDER_REASON_DMA_OVERRUN:
            return "dma-overrun";
        case RECORDER_REASON_BUFFER_OVERFLOW:
            return "buffer-overflow";
        case RECORDER_REASON_LOW_BATTERY:
            return "low-battery";
        case RECORDER_REASON_MANIFEST:
            return "manifest";
        case RECORDER_REASON_FINALIZE:
            return "finalize";
        case RECORDER_REASON_INTERNAL:
            return "internal";
        case RECORDER_REASON_USB:
            return "usb";
        default:
            return "unknown";
    }
}

bool recorder_state_format_event(char *out, size_t out_size,
                                 const char *timestamp,
                                 recorder_state_t from,
                                 recorder_state_t to,
                                 recorder_reason_t reason,
                                 int battery_mv) {
    int n;
    bool have_ts;
    if (out == NULL || out_size == 0) {
        return false;
    }
    out[0] = '\0';
    have_ts = timestamp != NULL && timestamp[0] != '\0';
    if (have_ts && battery_mv > 0) {
        n = snprintf(out, out_size,
                     "{\"event\":\"state\",\"timestamp\":\"%s\","
                     "\"from\":\"%s\",\"to\":\"%s\","
                     "\"reason\":\"%s\",\"batteryMv\":%d}\n",
                     timestamp, recorder_state_str(from),
                     recorder_state_str(to), recorder_reason_str(reason),
                     battery_mv);
    } else if (have_ts) {
        n = snprintf(out, out_size,
                     "{\"event\":\"state\",\"timestamp\":\"%s\","
                     "\"from\":\"%s\",\"to\":\"%s\","
                     "\"reason\":\"%s\",\"batteryMv\":null}\n",
                     timestamp, recorder_state_str(from),
                     recorder_state_str(to), recorder_reason_str(reason));
    } else if (battery_mv > 0) {
        n = snprintf(out, out_size,
                     "{\"event\":\"state\",\"timestamp\":null,"
                     "\"from\":\"%s\",\"to\":\"%s\","
                     "\"reason\":\"%s\",\"batteryMv\":%d}\n",
                     recorder_state_str(from), recorder_state_str(to),
                     recorder_reason_str(reason), battery_mv);
    } else {
        n = snprintf(out, out_size,
                     "{\"event\":\"state\",\"timestamp\":null,"
                     "\"from\":\"%s\",\"to\":\"%s\","
                     "\"reason\":\"%s\",\"batteryMv\":null}\n",
                     recorder_state_str(from), recorder_state_str(to),
                     recorder_reason_str(reason));
    }
    if (n < 0 || (size_t)n >= out_size) {
        out[0] = '\0';
        return false;
    }
    return true;
}

bool recorder_state_append_event(const char *path,
                                 const char *timestamp,
                                 recorder_state_t from,
                                 recorder_state_t to,
                                 recorder_reason_t reason,
                                 int battery_mv) {
    char line[320];
    FILE *f;
    size_t len;
    bool ok = false;
    if (path == NULL || path[0] == '\0' ||
        !recorder_state_format_event(line, sizeof(line), timestamp, from, to,
                                     reason, battery_mv)) {
        return false;
    }
    f = fopen(path, "ab");
    if (f == NULL) {
        return false;
    }
    len = strlen(line);
    ok = fwrite(line, 1, len, f) == len && fflush(f) == 0;
    if (fclose(f) != 0) {
        ok = false;
    }
    return ok;
}
