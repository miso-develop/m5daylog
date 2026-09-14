// Portable WAV rotation / finalize — Task #45 (IM-008). Stdio only.
//
// Gap note: rotation file ops (close + rename + new open) run in the
// writer task WITHOUT the pipeline lock and WITHOUT stopping the
// capture task, so PDM/DMA keeps filling the other 32KB slot meanwhile
// (1s of absorption at 32KB/s). The second slot is exactly what keeps
// the unintended rotation gap within the <=100ms acceptance bound.
//
// Idempotency note: the first finalize does close+rename; simultaneous
// USB/low-battery/stop duplicates return the cached result with no
// double close and no double rename.

#include "wav_rotation.h"

#include <stdio.h>
#include <string.h>

static bool is_digit(char c) {
    return c >= '0' && c <= '9';
}

static bool valid_date(const char *date) {
    size_t i;
    if (date == NULL) {
        return false;
    }
    if (strlen(date) != 10u) {
        return false;
    }
    // "YYYY-MM-DD": digits except '-' at index 4 and 7.
    for (i = 0; i < 10u; i++) {
        if (i == 4u || i == 7u) {
            if (date[i] != '-') {
                return false;
            }
        } else if (!is_digit(date[i])) {
            return false;
        }
    }
    return true;
}

static bool valid_time6(const char *time6) {
    size_t i;
    if (time6 == NULL) {
        return false;
    }
    if (strlen(time6) != 6u) {
        return false;
    }
    for (i = 0; i < 6u; i++) {
        if (!is_digit(time6[i])) {
            return false;
        }
    }
    return true;
}

static bool valid_recording_id(const char *id) {
    size_t i;
    size_t len;
    if (id == NULL) {
        return false;
    }
    len = strlen(id);
    if (len == 0u || len > 64u) {
        return false;
    }
    for (i = 0; i < len; i++) {
        if (id[i] == '/' || id[i] == '\\' || id[i] == '\0') {
            return false;
        }
    }
    return true;
}

static bool has_part_suffix_local(const char *path) {
    size_t len;
    size_t suffix;
    if (path == NULL) {
        return false;
    }
    len = strlen(path);
    suffix = strlen(RECORDER_PART_SUFFIX);
    if (len < suffix) {
        return false;
    }
    return strcmp(path + len - suffix, RECORDER_PART_SUFFIX) == 0;
}

static bool has_wav_suffix_local(const char *path) {
    size_t len;
    size_t suffix;
    if (path == NULL) {
        return false;
    }
    len = strlen(path);
    suffix = strlen(RECORDER_WAV_SUFFIX);
    if (len < suffix) {
        return false;
    }
    return strcmp(path + len - suffix, RECORDER_WAV_SUFFIX) == 0;
}

static bool path_exists(const char *path) {
    FILE *fp;
    if (path == NULL) {
        return false;
    }
    fp = fopen(path, "rb");
    if (fp == NULL) {
        return false;
    }
    fclose(fp);
    return true;
}

wav_rotate_reason_t wav_rotation_should_rotate(
    uint32_t elapsed_sec, uint32_t segment_bytes, bool date_changed,
    const wav_rotate_events_t *events) {
    if (events != NULL) {
        if (events->usb_connected) {
            return WAV_ROTATE_USB;
        }
        if (events->low_battery) {
            return WAV_ROTATE_LOW_BATTERY;
        }
        if (events->stop_requested) {
            return WAV_ROTATE_STOP_REQUEST;
        }
    }
    // Midnight crossing never mixes dates: a new file in the new date
    // directory wins over the periodic time trigger.
    if (date_changed) {
        return WAV_ROTATE_MIDNIGHT;
    }
    if (elapsed_sec >= RECORDER_ROTATION_INTERVAL_SEC ||
        segment_bytes >= RECORDER_ROTATION_PAYLOAD_BYTES) {
        return WAV_ROTATE_TIME_30MIN;
    }
    return WAV_ROTATE_NONE;
}

bool wav_rotation_build_part_path(char *out, size_t out_size,
                                  const char *date_yyyy_mm_dd,
                                  const char *time_hhmmss,
                                  const char *recording_id) {
    int needed;
    if (out == NULL || out_size == 0u) {
        return false;
    }
    out[0] = '\0';
    if (!valid_date(date_yyyy_mm_dd) || !valid_time6(time_hhmmss) ||
        !valid_recording_id(recording_id)) {
        return false;
    }
    needed = snprintf(out, out_size, "%s/%s/%s_%s%s",
                      RECORDER_RECORDINGS_DIR, date_yyyy_mm_dd, time_hhmmss,
                      recording_id, RECORDER_PART_SUFFIX);
    if (needed < 0 || (size_t)needed >= out_size) {
        out[0] = '\0';
        return false;
    }
    return true;
}

bool wav_rotation_build_wav_path(char *out, size_t out_size,
                                 const char *date_yyyy_mm_dd,
                                 const char *time_hhmmss,
                                 const char *recording_id) {
    int needed;
    if (out == NULL || out_size == 0u) {
        return false;
    }
    out[0] = '\0';
    if (!valid_date(date_yyyy_mm_dd) || !valid_time6(time_hhmmss) ||
        !valid_recording_id(recording_id)) {
        return false;
    }
    needed = snprintf(out, out_size, "%s/%s/%s_%s%s",
                      RECORDER_RECORDINGS_DIR, date_yyyy_mm_dd, time_hhmmss,
                      recording_id, RECORDER_WAV_SUFFIX);
    if (needed < 0 || (size_t)needed >= out_size) {
        out[0] = '\0';
        return false;
    }
    return true;
}

bool wav_rotation_part_to_wav(const char *part_path, char *wav_out,
                              size_t wav_size) {
    size_t len;
    size_t suffix;
    size_t base;
    if (part_path == NULL || wav_out == NULL || wav_size == 0u) {
        return false;
    }
    wav_out[0] = '\0';
    if (!has_part_suffix_local(part_path)) {
        return false;
    }
    // ".wav.part" -> ".wav": strip the trailing ".part" (5 chars),
    // keeping the ".wav" prefix intact. The input shape is already
    // enforced via the `.wav.part` suffix above; the output shape is
    // verified below so only `.wav` results are returned.
    len = strlen(part_path);
    suffix = strlen(".part");
    base = len - suffix;
    if (base + 1u > wav_size) {
        return false;
    }
    memcpy(wav_out, part_path, base);
    wav_out[base] = '\0';
    if (!has_wav_suffix_local(wav_out)) {
        wav_out[0] = '\0';
        return false;
    }
    return true;
}

bool wav_rotation_finalize_paths(const char *part_path,
                                 const char *wav_path) {
    bool part_present;
    bool wav_present;
    if (!has_part_suffix_local(part_path) ||
        !has_wav_suffix_local(wav_path)) {
        return false;
    }
    // Suffix discipline: the wav path must be the part path minus ".part"
    // so a finalize can never redirect an unrelated file.
    {
        char expect[RECORDER_MAX_PATH_LEN];
        if (!wav_rotation_part_to_wav(part_path, expect, sizeof(expect))) {
            return false;
        }
        if (strcmp(expect, wav_path) != 0) {
            return false;
        }
    }
    part_present = path_exists(part_path);
    wav_present = path_exists(wav_path);
    if (part_present) {
        // Collision guard: never overwrite an existing finalized file.
        if (wav_present) {
            return false;
        }
        if (rename(part_path, wav_path) != 0) {
            return false;
        }
        return true;
    }
    // No part left: success only when the finalized file already exists
    // (a duplicate stop/rotation event after a prior finalize). No
    // second rename is issued on this path.
    if (wav_present) {
        return true;
    }
    return false;
}

void wav_rotation_state_init(wav_rotation_state_t *state,
                             const char *part_path, const char *wav_path) {
    size_t part_len;
    size_t wav_len;
    if (state == NULL) {
        return;
    }
    memset(state->part_path, 0, sizeof(state->part_path));
    memset(state->wav_path, 0, sizeof(state->wav_path));
    state->finalize_called = false;
    state->finalized_ok = false;
    if (!has_part_suffix_local(part_path) ||
        !has_wav_suffix_local(wav_path)) {
        return;
    }
    part_len = strlen(part_path);
    wav_len = strlen(wav_path);
    if (part_len >= sizeof(state->part_path) ||
        wav_len >= sizeof(state->wav_path)) {
        return;
    }
    memcpy(state->part_path, part_path, part_len + 1u);
    memcpy(state->wav_path, wav_path, wav_len + 1u);
}

bool wav_rotation_finalize_once(wav_rotation_state_t *state,
                                sd_pcm_sink_t *sink) {
    bool close_ok;
    bool rename_ok;
    if (state == NULL) {
        return false;
    }
    // Idempotent close: simultaneous USB/low-battery/stop duplicates
    // return the cached result without a second close/rename.
    if (state->finalize_called) {
        return state->finalized_ok;
    }
    state->finalize_called = true;
    state->finalized_ok = false;
    if (state->part_path[0] == '\0' || state->wav_path[0] == '\0') {
        return false;
    }
    // Header finalize first (patch sizes + media sync), then atomic
    // rename. Close is itself idempotent on an already-closed sink.
    close_ok = sd_pcm_sink_close(sink);
    if (!close_ok) {
        return false;
    }
    rename_ok =
        wav_rotation_finalize_paths(state->part_path, state->wav_path);
    state->finalized_ok = rename_ok;
    return state->finalized_ok;
}
