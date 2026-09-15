#pragma once

// Portable power-loss recovery / quarantine — Task #46 (IM-009).
//
// Spec #36 lifecycle: a power loss leaves `HHMMSS_<recordingId>.wav.part`
// files under `recordings/YYYY-MM-DD/` (or directly under `recordings/` for
// the legacy build-time fallback). At boot, after SD_MOUNT and before new
// capture opens, the firmware scans those residual `.wav.part` files and
// rebuilds each WAV header from the already-written PCM payload length:
//
// - payload = file_size - 44; an odd trailing byte is truncated to the
//   16bit sample boundary (only the tail is discarded, per Spec #36).
// - the header is rewritten with the fixed PoC format (16kHz / 16bit /
//   mono PCM) and the recovered payload size, flushed/synced, then the
//   file is renamed `.wav.part` -> `.wav` (PC syncs only `.wav`).
// - unrecoverable files (too small, unreadable, invalid RIFF/WAVE/PCM
//   header, format mismatch, or destination collision) are moved with
//   `rename()` into `/M5DAYLOG/quarantine/` and never auto-deleted.
// - recovery results are appended to `/M5DAYLOG/events.jsonl` as JSON
//   lines carrying only counts / sizes / result classes (no audio,
//   transcript, credential, or secret payload).
//
// Portable: stdio + dirent + sys/stat only, no ESP-IDF dependency. Works
// over ESP-IDF FATFS (VFS) and on the host for tests. Never deletes
// audio: only `rename()` is used, never `remove()` / `unlink()`.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "recorder_config.h"

#ifdef __cplusplus
extern "C" {
#endif

// Per-file recovery outcome. RECOVERED means the `.part` is now a valid
// `.wav`; QUARANTINED means the `.part` was isolated into quarantine/;
// ERROR means fail-loud I/O failure with nothing deleted (the `.part`
// remains where it was for a later boot to retry).
typedef enum {
    WAV_RECOVERY_RECOVERED = 0,
    WAV_RECOVERY_QUARANTINED = 1,
    WAV_RECOVERY_ERROR = 2
} wav_recovery_outcome_t;

// Aggregate counts for one boot scan. All fields are plain counts/sizes
// safe for serial + events.jsonl diagnostics.
typedef struct {
    uint32_t scanned;
    uint32_t recovered;
    uint32_t quarantined;
    uint32_t errors;
    uint32_t recovered_pcm_bytes;
    uint32_t quarantined_files;
} wav_recovery_stats_t;

// True when path ends in `.wav.part` (the only suffix recovery scans).
bool wav_recovery_is_part_path(const char *path);

// Validate a 44-byte header as a PoC `.wav.part` candidate: RIFF / WAVE /
// fmt␣ / PCM(1) / mono / 16kHz / byte_rate 32000 / align 2 / 16bit /
// data tags must match. Stored ChunkSize / Subchunk2Size are IGNORED
// (they are stale after a power loss and are recomputed from the file
// size). Returns false on NULL or any format mismatch.
bool wav_recovery_validate_header(const uint8_t header[RECORDER_WAV_HEADER_SIZE]);

// Build `<quarantine_dir>/<basename(part_path)>`. Returns false on bad
// input or truncation (out is NUL-terminated when size > 0).
bool wav_recovery_build_quarantine_path(const char *part_path,
                                        const char *quarantine_dir, char *out,
                                        size_t out_size);

// Recover one residual `.part` file:
// - part_path must end in `.wav.part`; wav_path must be part_path minus
//   the trailing `.part` (same correspondence rule as Task #45 finalize).
// - quarantine_dir receives unrecoverable files (basename join; a numeric
//   `_<n>` suffix is added when the first candidate already exists).
// - on success `out_pcm_bytes` (may be NULL) receives the recovered
//   payload size (after odd-tail truncation).
// Never overwrites: when wav_path already exists the `.part` is
// quarantined instead. Never deletes: failures leave the `.part` in
// place and return WAV_RECOVERY_ERROR.
wav_recovery_outcome_t wav_recovery_recover_file(const char *part_path,
                                                 const char *wav_path,
                                                 const char *quarantine_dir,
                                                 uint32_t *out_pcm_bytes);

// Ensure a directory exists (mkdir; EEXIST is success). Returns false on
// bad input or a real mkdir failure.
bool wav_recovery_ensure_dir(const char *dir);

// Boot scan: recover every `*.wav.part` directly under recordings_dir and
// one level deep (date subdirectories `YYYY-MM-DD/`). quarantine_dir is
// created when missing. When events_path is non-NULL, one JSON summary
// line plus one JSON line per file (basename + pcm_bytes + result) are
// appended to events_path; events I/O failure never fails the scan
// itself. out_stats (required) is zeroed then accumulated. Returns true
// when the directory walk completed (even with per-file quarantines or
// errors); false only on fatal input errors or when recordings_dir
// cannot be opened / quarantine_dir cannot be created.
bool wav_recovery_scan_recordings(const char *recordings_dir,
                                  const char *quarantine_dir,
                                  const char *events_path,
                                  wav_recovery_stats_t *out_stats);

// Append one recovery summary JSON line to events_path. Returns false on
// bad input or file I/O failure. The line carries only counts/sizes.
bool wav_recovery_append_summary_event(const char *events_path,
                                       const wav_recovery_stats_t *stats);

#ifdef __cplusplus
}
#endif
