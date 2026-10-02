#pragma once

// Portable incremental SHA-256 — Task #47 (IM-010).
//
// Spec #35 integrity: the SHA-256 covers the WAV file bytes entire and is
// rendered as lowercase hex (64 chars). The writer hashes each finalized
// `.wav` incrementally (chunked file reads) so a full 16h day never needs
// to be buffered; the hex must match a PC-side recomputation byte for
// byte. Portable: stdint/stddef/stdio only, no ESP-IDF dependency, no
// allocation. Works over ESP-IDF FATFS (VFS) and on the host for tests.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint32_t state[8];
    uint64_t bitlen;
    uint8_t data[64];
    uint32_t datalen;
} sha256_ctx_t;

// Initialize a fresh hash context.
void sha256_init(sha256_ctx_t *ctx);

// Absorb `len` bytes (may be called any number of times, including zero).
void sha256_update(sha256_ctx_t *ctx, const uint8_t *data, size_t len);

// Finalize and write the 32-byte digest. The context is consumed.
void sha256_final(sha256_ctx_t *ctx, uint8_t hash[32]);

// Render a 32-byte digest as 64 lowercase hex chars plus NUL.
// `out_hex` must hold at least RECORDER_SHA256_HEX_LEN (65) bytes.
void sha256_to_hex(const uint8_t hash[32], char out_hex[65]);

// True when `s` is exactly 64 lowercase hex chars (Spec #35 file-hash
// shape). Uppercase or truncated values are rejected so a PC recomparison
// cannot silently pass on a reformatted digest.
bool sha256_is_valid_hex(const char *s);

// Incremental file hash: reads `path` in 1KB chunks through
// sha256_update and renders lowercase hex into `out_hex` (65 bytes).
// Returns false on NULL input, unreadable file, or read error; `out_hex`
// is NUL-terminated with an empty string on failure. The file content is
// never logged or retained.
bool sha256_file_hex(const char *path, char out_hex[65]);

#ifdef __cplusplus
}
#endif
