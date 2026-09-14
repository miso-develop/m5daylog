#pragma once

// Portable WAV rotation / finalize — Task #45 (IM-008).
//
// Spec #36 lifecycle: while capturing, audio lives in
// `recordings/YYYY-MM-DD/HHMMSS_<recordingId>.wav.part`; rotation
// header-finalizes, flushes/closes, then renames to `.wav` (PC syncs
// only `.wav`). Triggers: 30 minutes elapsed, midnight date change,
// USB connection, low battery, safe-stop request. Close is idempotent
// across simultaneous events: the first finalize does I/O, later calls
// return the cached result without a second close/rename.
//
// Portable: stdio + string only, no ESP-IDF dependency. Works over
// ESP-IDF FATFS and on the host for tests. Never deletes audio: only
// `rename()` is used, never `remove()`.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "recorder_config.h"
#include "sd_pcm_sink.h"

#ifdef __cplusplus
extern "C" {
#endif

// Rotation reason. Priority for simultaneous events (logging only —
// finalize itself is idempotent so order changes no I/O):
// USB > LOW_BATTERY > STOP_REQUEST > MIDNIGHT > TIME_30MIN.
typedef enum {
    WAV_ROTATE_NONE = 0,
    WAV_ROTATE_TIME_30MIN,
    WAV_ROTATE_MIDNIGHT,
    WAV_ROTATE_USB,
    WAV_ROTATE_LOW_BATTERY,
    WAV_ROTATE_STOP_REQUEST
} wav_rotate_reason_t;

// External stop-event inputs for the pure rotation decision below.
// Time/midnight inputs arrive as elapsed/bytes/date_changed; these
// flags cover the three terminal stop families that all funnel through
// the same idempotent finalize (later USB/state tasks call the same
// finalize entry point).
typedef struct {
    bool usb_connected;
    bool low_battery;
    bool stop_requested;
} wav_rotate_events_t;

// Pure rotation decision: no I/O, no clock read.
// - elapsed_sec: current segment age in seconds.
// - segment_bytes: current segment PCM payload bytes.
// - date_changed: true when the wall-clock date differs from the
//   segment start date (midnight crossing, never mixed into one file).
// - events: terminal stop inputs (may be NULL == no stop events).
// Returns WAV_ROTATE_NONE when the segment continues.
wav_rotate_reason_t wav_rotation_should_rotate(uint32_t elapsed_sec,
                                               uint32_t segment_bytes,
                                               bool date_changed,
                                               const wav_rotate_events_t *events);

// Build `recordings/YYYY-MM-DD/HHMMSS_<recordingId>.wav.part`.
// date must be "YYYY-MM-DD", time6 must be "HHMMSS" digits,
// recording_id must be non-empty without '/' or '\\' (opaque caller
// placeholder until later Tasks own full identity; no credential or
// private content is encoded here). Returns false on bad input or
// truncation (out is left NUL-terminated when size > 0).
bool wav_rotation_build_part_path(char *out, size_t out_size,
                                  const char *date_yyyy_mm_dd,
                                  const char *time_hhmmss,
                                  const char *recording_id);

// Build the matching finalized `.wav` path for the same segment.
bool wav_rotation_build_wav_path(char *out, size_t out_size,
                                 const char *date_yyyy_mm_dd,
                                 const char *time_hhmmss,
                                 const char *recording_id);

// Map a `.wav.part` path to its `.wav` path by stripping the trailing
// `.part` (".wav.part" -> ".wav"). Returns false when the input lacks
// the suffix or the output is too small.
bool wav_rotation_part_to_wav(const char *part_path, char *wav_out,
                              size_t wav_size);

// Filesystem-level idempotent rename: header must already be finalized
// and closed by the caller (sd_pcm_sink_close).
// - part exists -> rename to wav; missing wav required (collision of
//   both existing fails loud false, never overwrites).
// - part missing + wav present -> already finalized -> true, no I/O
//   beyond existence probes (no double rename).
// - neither exists -> false (nothing to finalize, fail-loud).
// Never deletes: no remove() path exists in this module.
bool wav_rotation_finalize_paths(const char *part_path,
                                 const char *wav_path);

// One-segment idempotent finalize state. The writer task owns one
// instance per open segment: the first finalize closes the sink
// (header patch + media sync) then renames; later calls — including
// simultaneous USB/low-battery/stop duplicates — return the cached
// result without further close/rename.
typedef struct {
    char part_path[RECORDER_MAX_PATH_LEN];
    char wav_path[RECORDER_MAX_PATH_LEN];
    bool finalize_called;
    bool finalized_ok;
} wav_rotation_state_t;

// Copy paths into state and clear the once-guard. Bad input (NULL state,
// part without `.wav.part` suffix, empty wav path, truncation) leaves
// empty paths so the later finalize fails loud instead of renaming the
// wrong file.
void wav_rotation_state_init(wav_rotation_state_t *state,
                             const char *part_path, const char *wav_path);

// Idempotent close+rename for the state's segment. Safe to call any
// number of times from the single writer task (rotation + terminal
// stop share this entry point). The sink may already be closed: close
// is itself idempotent. Returns true only when the `.wav` is durably
// present via this or a prior call.
bool wav_rotation_finalize_once(wav_rotation_state_t *state,
                                sd_pcm_sink_t *sink);

#ifdef __cplusplus
}
#endif
