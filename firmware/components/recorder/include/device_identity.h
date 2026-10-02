#pragma once

// Device identity — Task #47 (IM-010).
//
// First-boot `deviceId` (UUIDv4) is generated once and persisted in NVS
// (`m5daylog/device_id`); later boots reuse the stored value so one
// physical device keeps one identity and recordingIds never collide with
// a regenerated device. `device.json` (Spec #35 S-002) surfaces that
// identity plus the fixed PoC audio capabilities for the PC to validate.
//
// Portable helpers (UUID validate/format, device.json build/ensure) are
// stdio only and host-testable. Only `device_identity_ensure_device_id()`
// touches NVS and is ESP-IDF only (`ESP_PLATFORM`); host builds return
// `ESP_ERR_NOT_SUPPORTED`.
//
// Security: UUIDs are random opaque identifiers (no credential, audio,
// transcript, or schedule content). A `device.json` whose embedded
// deviceId disagrees with NVS is never auto-overwritten (fail-closed,
// caller enters ERROR) so two identities can never fork silently.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "recorder_config.h"

#ifdef ESP_PLATFORM
#include "esp_err.h"
#else
// Host/test builds: minimal stand-ins so headers stay includable.
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL 1
#define ESP_ERR_INVALID_ARG 2
#define ESP_ERR_INVALID_STATE 3
#define ESP_ERR_NO_MEM 4
#define ESP_ERR_NOT_SUPPORTED 5
#endif

#ifdef __cplusplus
extern "C" {
#endif

// True when `s` is a canonical UUID `8-4-4-4-12` of hex digits (accepts
// either case on read so a PC-written file still validates; generation
// always emits lowercase).
bool device_identity_is_valid_uuid(const char *s);

// Format 16 random bytes as a UUIDv4 string (`out` needs
// RECORDER_UUID_STR_LEN bytes). Applies the RFC 4122 version (4) and
// variant (10xx) bits, then renders lowercase hex with hyphens.
// Returns false on NULL input.
bool device_identity_format_uuid_v4(const uint8_t rand16[16],
                                    char out[RECORDER_UUID_STR_LEN]);

// Build the canonical `device.json` document (no trailing newline) into
// `out`: `{"schemaVersion":1,"deviceId":"...","model":"...",
// "firmwareVersion":"...","audioCapabilities":{"sampleRate":16000,
// "bitDepth":16,"channels":1,"format":"pcm"}}`.
// `model`/`firmware_version` default to RECORDER_MODEL /
// RECORDER_FIRMWARE_VERSION when NULL. Returns false on bad input
// (invalid UUID, empty model/version, `"` or `\` in free text) or
// truncation.
bool device_identity_build_device_json(const char *device_id,
                                       const char *model,
                                       const char *firmware_version, char *out,
                                       size_t out_size);

// Ensure `path` holds a `device.json` for `device_id`:
// - missing file -> create with the canonical document (+ fsync), true.
// - present file with the same deviceId (and schemaVersion 1) -> true,
//   no rewrite (idempotent, no wear).
// - present file with a DIFFERENT deviceId, unreadable content, or an
//   unknown schemaVersion -> false with the existing file preserved
//   (never auto-overwritten, fail-closed).
// `model`/`firmware_version` follow the builder defaults when NULL.
bool device_identity_ensure_device_json(const char *path,
                                        const char *device_id,
                                        const char *model,
                                        const char *firmware_version);

// Extract the `deviceId` string from a `device.json` document in memory.
// Writes the NUL-terminated value into `out` (needs
// RECORDER_UUID_STR_LEN bytes). Returns false when the document has no
// well-formed `"deviceId":"<uuid>"` field or the value is not a UUID.
bool device_identity_parse_device_id(const char *json, size_t json_len,
                                     char out[RECORDER_UUID_STR_LEN]);

// Extract the top-level integer `schemaVersion` from a JSON document in
// memory. Returns false when the field is missing or not a plain
// non-negative integer.
bool device_identity_parse_schema_version(const char *json, size_t json_len,
                                          unsigned *out_version);

// First-boot-persistent device identity. Reads NVS `m5daylog/device_id`;
// when absent, generates a fresh UUIDv4 from `esp_random()`, stores it,
// and returns it. The same value is returned on every later boot.
// ESP-IDF only; host builds return ESP_ERR_NOT_SUPPORTED.
// Fail-loud: NVS errors or a corrupt stored value return non-ESP_OK so
// the caller enters ERROR instead of recording under a forked identity.
esp_err_t device_identity_ensure_device_id(char out[RECORDER_UUID_STR_LEN]);

#ifdef __cplusplus
}
#endif
