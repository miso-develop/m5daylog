// Portable incremental SHA-256 — Task #47 (IM-010). Stdint/stdio only.
//
// Standard FIPS 180-4 SHA-256 (initial state = fractional parts of square
// roots of the first 8 primes; round constants = fractional parts of cube
// roots of the first 64 primes). No ESP-IDF dependency. File hashing reads
// in 1KB chunks so multi-megabyte WAV files hash incrementally without
// buffering the whole file or growing the writer task frame.

#include "sha256.h"

#include <stdio.h>
#include <string.h>

#define ROTRIGHT(a, b) (((a) >> (b)) | ((a) << (32 - (b))))
#define CH(x, y, z) (((x) & (y)) ^ (~(x) & (z)))
#define MAJ(x, y, z) (((x) & (y)) ^ ((x) & (z)) ^ ((y) & (z)))
#define EP0(x) (ROTRIGHT(x, 2) ^ ROTRIGHT(x, 13) ^ ROTRIGHT(x, 22))
#define EP1(x) (ROTRIGHT(x, 6) ^ ROTRIGHT(x, 11) ^ ROTRIGHT(x, 25))
#define SIG0(x) (ROTRIGHT(x, 7) ^ ROTRIGHT(x, 18) ^ ((x) >> 3))
#define SIG1(x) (ROTRIGHT(x, 17) ^ ROTRIGHT(x, 19) ^ ((x) >> 10))

static const uint32_t k[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu,
    0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u, 0xd807aa98u, 0x12835b01u,
    0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u,
    0xc19bf174u, 0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu,
    0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau, 0x983e5152u,
    0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u,
    0x06ca6351u, 0x14292967u, 0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu,
    0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
    0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u, 0xd192e819u,
    0xd6990624u, 0xf40e3585u, 0x106aa070u, 0x19a4c116u, 0x1e376c08u,
    0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu,
    0x682e6ff3u, 0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
    0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u,
};

static void sha256_transform(sha256_ctx_t *ctx, const uint8_t data[64]) {
    uint32_t a;
    uint32_t b;
    uint32_t c;
    uint32_t d;
    uint32_t e;
    uint32_t f;
    uint32_t g;
    uint32_t h;
    uint32_t i;
    uint32_t j;
    uint32_t t1;
    uint32_t t2;
    uint32_t m[64];

    for (i = 0, j = 0; i < 16; ++i, j += 4) {
        m[i] = ((uint32_t)data[j] << 24) | ((uint32_t)data[j + 1] << 16) |
               ((uint32_t)data[j + 2] << 8) | ((uint32_t)data[j + 3]);
    }
    for (; i < 64; ++i) {
        m[i] = SIG1(m[i - 2]) + m[i - 7] + SIG0(m[i - 15]) + m[i - 16];
    }

    a = ctx->state[0];
    b = ctx->state[1];
    c = ctx->state[2];
    d = ctx->state[3];
    e = ctx->state[4];
    f = ctx->state[5];
    g = ctx->state[6];
    h = ctx->state[7];

    for (i = 0; i < 64; ++i) {
        t1 = h + EP1(e) + CH(e, f, g) + k[i] + m[i];
        t2 = EP0(a) + MAJ(a, b, c);
        h = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = b;
        b = a;
        a = t1 + t2;
    }

    ctx->state[0] += a;
    ctx->state[1] += b;
    ctx->state[2] += c;
    ctx->state[3] += d;
    ctx->state[4] += e;
    ctx->state[5] += f;
    ctx->state[6] += g;
    ctx->state[7] += h;
}

void sha256_init(sha256_ctx_t *ctx) {
    if (ctx == NULL) {
        return;
    }
    ctx->datalen = 0;
    ctx->bitlen = 0;
    ctx->state[0] = 0x6a09e667u;
    ctx->state[1] = 0xbb67ae85u;
    ctx->state[2] = 0x3c6ef372u;
    ctx->state[3] = 0xa54ff53au;
    ctx->state[4] = 0x510e527fu;
    ctx->state[5] = 0x9b05688cu;
    ctx->state[6] = 0x1f83d9abu;
    ctx->state[7] = 0x5be0cd19u;
}

void sha256_update(sha256_ctx_t *ctx, const uint8_t *data, size_t len) {
    size_t i;
    if (ctx == NULL || (data == NULL && len != 0)) {
        return;
    }
    for (i = 0; i < len; ++i) {
        ctx->data[ctx->datalen] = data[i];
        ctx->datalen++;
        if (ctx->datalen == 64) {
            sha256_transform(ctx, ctx->data);
            ctx->bitlen += 512;
            ctx->datalen = 0;
        }
    }
}

void sha256_final(sha256_ctx_t *ctx, uint8_t hash[32]) {
    uint32_t i;
    if (ctx == NULL || hash == NULL) {
        return;
    }
    i = ctx->datalen;

    // Pad whatever data is left.
    if (ctx->datalen < 56) {
        ctx->data[i++] = 0x80;
        while (i < 56) {
            ctx->data[i++] = 0x00;
        }
    } else {
        ctx->data[i++] = 0x80;
        while (i < 64) {
            ctx->data[i++] = 0x00;
        }
        sha256_transform(ctx, ctx->data);
        memset(ctx->data, 0, 56);
    }

    // Append the total message length in bits (big-endian) and transform.
    ctx->bitlen += (uint64_t)ctx->datalen * 8u;
    ctx->data[63] = (uint8_t)(ctx->bitlen);
    ctx->data[62] = (uint8_t)(ctx->bitlen >> 8);
    ctx->data[61] = (uint8_t)(ctx->bitlen >> 16);
    ctx->data[60] = (uint8_t)(ctx->bitlen >> 24);
    ctx->data[59] = (uint8_t)(ctx->bitlen >> 32);
    ctx->data[58] = (uint8_t)(ctx->bitlen >> 40);
    ctx->data[57] = (uint8_t)(ctx->bitlen >> 48);
    ctx->data[56] = (uint8_t)(ctx->bitlen >> 56);
    sha256_transform(ctx, ctx->data);

    // Convert state to big-endian digest. Reverse loop with explicit
    // termination (no unsigned wraparound idiom).
    for (i = 0; i < 8; ++i) {
        hash[i * 4 + 0] = (uint8_t)(ctx->state[i] >> 24);
        hash[i * 4 + 1] = (uint8_t)(ctx->state[i] >> 16);
        hash[i * 4 + 2] = (uint8_t)(ctx->state[i] >> 8);
        hash[i * 4 + 3] = (uint8_t)(ctx->state[i]);
    }
}

static char nibble_to_lower_hex(uint8_t v) {
    v &= 0x0Fu;
    return (char)(v < 10u ? ('0' + v) : ('a' + (v - 10u)));
}

void sha256_to_hex(const uint8_t hash[32], char out_hex[65]) {
    uint32_t i;
    if (out_hex == NULL) {
        return;
    }
    out_hex[0] = '\0';
    if (hash == NULL) {
        return;
    }
    for (i = 0; i < 32; ++i) {
        out_hex[i * 2 + 0] = nibble_to_lower_hex((uint8_t)(hash[i] >> 4));
        out_hex[i * 2 + 1] = nibble_to_lower_hex(hash[i]);
    }
    out_hex[64] = '\0';
}

bool sha256_is_valid_hex(const char *s) {
    size_t i;
    if (s == NULL) {
        return false;
    }
    if (strlen(s) != 64u) {
        return false;
    }
    for (i = 0; i < 64u; ++i) {
        char c = s[i];
        bool digit = (c >= '0' && c <= '9');
        bool lower = (c >= 'a' && c <= 'f');
        if (!digit && !lower) {
            return false;
        }
    }
    return true;
}

bool sha256_file_hex(const char *path, char out_hex[65]) {
    FILE *fp = NULL;
    sha256_ctx_t ctx;
    uint8_t digest[32];
    // 1KB chunks: incremental hashing without growing the writer task
    // frame (see main.c stack-budget note).
    uint8_t buf[1024];
    size_t got;
    if (out_hex == NULL) {
        return false;
    }
    out_hex[0] = '\0';
    if (path == NULL || path[0] == '\0') {
        return false;
    }
    fp = fopen(path, "rb");
    if (fp == NULL) {
        return false;
    }
    sha256_init(&ctx);
    do {
        got = fread(buf, 1, sizeof(buf), fp);
        if (got > 0) {
            sha256_update(&ctx, buf, got);
        }
        if (got < sizeof(buf)) {
            if (ferror(fp)) {
                fclose(fp);
                out_hex[0] = '\0';
                return false;
            }
            break;
        }
    } while (true);
    fclose(fp);
    sha256_final(&ctx, digest);
    sha256_to_hex(digest, out_hex);
    memset(&ctx, 0, sizeof(ctx));
    memset(digest, 0, sizeof(digest));
    return true;
}
