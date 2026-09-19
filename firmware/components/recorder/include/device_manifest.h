#pragma once

// Device manifest / integrity — Task #47 (IM-010).
//
// Spec #35 (S-002) manifest contract: every finalized (rotation /
// safe-stop) or recovered (boot power-loss) `.wav` is reflected as one
// entry keyed by `recordingId` (the cross-system primary key; filename
// and timestamps are never keys). The SHA-256 covers the WAV file bytes
// entire (incremental chunked hash, lowercase hex 64 chars) and must match
// a PC-side recomputation. Timestamps are offset ISO-8601.
//
// Durability: updates write the full document to `manifest.tmp`
// (flush + fsync) then atomically rename to `manifest.json`. A crash
// mid-write leaves the existing `manifest.json` intact (or a complete
// `manifest.tmp` the next boot can promote); the old manifest is never
// truncated in place. Unknown `schemaVersion` majors and `deviceId`
// mismatches fail closed without writing. A duplicate `recordingId`
// carrying a different hash reports CONFLICT and never overwrites.
//
// Portable: stdio/dir/stat only, no ESP-IDF dependency. Never deletes
// audio: only rename() moves files (tmp -> manifest); no remove()/unlink()
// path exists in this module except best-effort stale-tmp cleanup.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "recorder_config.h"

#ifdef __cplusplus
extern "C" {
#endif

// Upsert outcome. CONFLICT means the manifest already holds the same
// recordingId with a different hash (or filename): the on-card manifest
// is preserved and nothing is written.
typedef enum {
    DEVICE_MANIFEST_OK = 0,
    DEVICE_MANIFEST_CONFLICT = 1,
    DEVICE_MANIFEST_ERROR = 2
} device_manifest_result_t;

// Recording finalization state recorded in the entry. Only these two
// states are PC sync candidates (Spec #35); `.wav.part` files are never
// entered.
#define DEVICE_MANIFEST_STATE_FINALIZED "finalized"
#define DEVICE_MANIFEST_STATE_RECOVERED "recovered"

// True when `s` is exactly 64 lowercase hex chars (Spec #35 hash shape).
bool device_manifest_is_valid_sha256_hex(const char *s);

// True when `s` matches `YYYY-MM-DDTHH:MM:SS+HH:MM` (25 chars, digits
// with the fixed separators). The device emits UTC (`+00:00`); the check
// accepts any numeric offset so PC-regenerated fixtures still validate.
bool device_manifest_is_valid_iso8601_offset(const char *s);

// Format UTC offset ISO-8601 `YYYY-MM-DDTHH:MM:SS+00:00` from broken-down
// fields (range-checked; out needs RECORDER_ISO8601_STR_LEN bytes).
// Returns false on out-of-range input or truncation.
bool device_manifest_format_iso8601_utc(int year, int mon, int mday, int hour,
                                        int min, int sec,
                                        char out[RECORDER_ISO8601_STR_LEN]);

// Build one recording entry object (no trailing newline) into `out`.
// `filename` is the M5DAYLOG-relative `.wav` path
// (`recordings/YYYY-MM-DD/HHMMSS_<recordingId>.wav`); `state` is one of
// the DEVICE_MANIFEST_STATE_* strings; `firmware_version` defaults to
// RECORDER_FIRMWARE_VERSION when NULL. Fixed audio contract
// (16000/16/1) is asserted against recorder_config.h values supplied in
// `sample_rate_hz`/`bit_depth`/`channels`. Returns false on bad input
// (non-UUID ids, bad filename/state/time/hash, `"` or `\` in free text)
// or truncation. `out` should hold RECORDER_MANIFEST_ENTRY_MAX bytes.
bool device_manifest_build_entry(const char *recording_id,
                                 const char *filename, const char *started_at,
                                 uint32_t duration_ms, uint32_t size_bytes,
                                 const char *sha256_hex, unsigned sample_rate_hz,
                                 unsigned bit_depth, unsigned channels,
                                 const char *state,
                                 const char *firmware_version, char *out,
                                 size_t out_size);

// Build an empty manifest document for `device_id` with `updated_at`
// (no trailing newline). Returns false on bad input or truncation.
bool device_manifest_build_empty(const char *device_id, const char *updated_at,
                                 char *out, size_t out_size);

// Hash the WAV file bytes entire incrementally (4KB reads) into
// lowercase hex (`out_hex` needs RECORDER_SHA256_HEX_LEN bytes) and
// report the total file size in `out_size_bytes` (may be NULL).
// Returns false on NULL input, missing/unreadable file, read error, or
// invalid (non-`.wav`) suffix. The file content is never logged.
bool device_manifest_hash_wav_file(const char *wav_path,
                                   char out_hex[RECORDER_SHA256_HEX_LEN],
                                   uint32_t *out_size_bytes);

// Extract the `<recordingId>` from a `HHMMSS_<recordingId>.wav[.part]`
// basename or full path (`out` needs 65 bytes to cover the 64-char
// rotation bound; UUIDs are 36). Returns false when the name lacks the
// `.wav` suffix, has no `_` separator, or the id is empty/oversized or
// contains `/`, `\`, `"`, or whitespace.
bool device_manifest_extract_recording_id(const char *wav_path, char *out,
                                          size_t out_size);

// Build the M5DAYLOG-relative finalized filename
// `recordings/YYYY-MM-DD/HHMMSS_<recordingId>.wav` (what the manifest
// stores; the PC joins it under the mount root). Returns false on bad
// input or truncation.
bool device_manifest_build_filename(const char *date_yyyy_mm_dd,
                                    const char *time_hhmmss,
                                    const char *recording_id, char *out,
                                    size_t out_size);

// Duration helper: PCM payload bytes -> milliseconds at the fixed
// 16kHz/16bit/mono byte rate (32000 B/s). Whole-millisecond truncation.
uint32_t device_manifest_duration_ms(uint32_t pcm_bytes);

// Atomic manifest upsert. `entry_json` is one object built by
// device_manifest_build_entry. On success the manifest holds exactly one
// entry per recordingId (idempotent re-upsert of the same id+hash is OK
// without a second logical change; CONFLICT on same id with a different
// hash or filename). `updated_at` refreshes the top-level timestamp on a
// real append; an idempotent duplicate leaves the file untouched.
// Missing manifest is created. Returns CONFLICT/ERROR with the existing
// manifest preserved (ERROR also preserves it on tmp-write failure).
device_manifest_result_t device_manifest_upsert_file(
    const char *manifest_path, const char *tmp_path, const char *device_id,
    const char *updated_at, const char *entry_json);

// Boot promotion: when `manifest_path` is missing but `tmp_path` holds a
// complete document for `device_id` (schemaVersion 1, parseable), rename
// it into place and return true. When the destination already exists,
// validates nothing and returns true after best-effort stale-tmp removal.
// Returns false when there is nothing promotable or the tmp content is
// invalid (both files are then left untouched, except an invalid tmp
// alone is left for inspection, never promoted).
bool device_manifest_recover_tmp(const char *manifest_path,
                                 const char *tmp_path, const char *device_id);

// Scan `recordings_dir` (top level plus one-level `YYYY-MM-DD/`
// subdirectories, mirroring the recovery scan) for finalized `.wav`
// files missing from the manifest and append them with `state`
// (`finalized` or `recovered`). Each appended entry derives
// recordingId from the filename, startedAt from the date directory +
// `HHMMSS` prefix (UTC), duration from the file size, and hash from an
// incremental file read. Files whose names cannot yield an id, or whose
// hash fails, are skipped without failing the scan (counted in
// `out_skipped`). `out_added`/`out_skipped` may be NULL. Returns false
// only on fatal input errors or when the manifest cannot be read or
// written (nothing is deleted in any path).
bool device_manifest_sync_wav_dir(const char *recordings_dir,
                                  const char *manifest_path,
                                  const char *tmp_path, const char *device_id,
                                  const char *updated_at, const char *state,
                                  uint32_t *out_added, uint32_t *out_skipped);

#ifdef __cplusplus
}
#endif
