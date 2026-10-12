// Synthetic host-only dependencies for compiling the actual #52 C module.
// All filesystem operations are restricted to the pytest temporary cwd.
#include <ctype.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "device_identity.h"
#include "device_manifest.h"
#include "sd_mount.h"

static int s_sd_mounted = 1;
static int s_fail_fsync = 0;
static int s_fail_rename = 0;
static int s_fsync_calls = 0;
static int s_rename_calls = 0;

void ack_test_set_mounted(int v) { s_sd_mounted = v; }
void ack_test_fail_fsync(int v) { s_fail_fsync = v; }
void ack_test_fail_rename(int v) { s_fail_rename = v; }
int ack_test_fsync_calls(void) { return s_fsync_calls; }
int ack_test_rename_calls(void) { return s_rename_calls; }
void ack_test_reset_faults(void) {
    s_fail_fsync = 0; s_fail_rename = 0;
    s_fsync_calls = 0; s_rename_calls = 0;
    s_sd_mounted = 1;
}
bool sd_mount_is_mounted(void) { return s_sd_mounted != 0; }

bool device_identity_is_valid_uuid(const char *s) {
    if (s == NULL || strlen(s) != 36u) return false;
    for (size_t i = 0; i < 36u; ++i) {
        if (i == 8u || i == 13u || i == 18u || i == 23u) {
            if (s[i] != '-') return false;
        } else if (!isxdigit((unsigned char)s[i])) {
            return false;
        }
    }
    return true;
}

bool device_manifest_is_valid_sha256_hex(const char *s) {
    if (s == NULL || strlen(s) != 64u) return false;
    for (size_t i = 0; i < 64u; ++i) {
        if (!((s[i] >= '0' && s[i] <= '9') ||
              (s[i] >= 'a' && s[i] <= 'f'))) return false;
    }
    return true;
}
bool device_manifest_extract_recording_id(const char *path, char *out,
                                          size_t out_size) {
    const char *base, *sep;
    size_t n;
    if (path == NULL || out == NULL) return false;
    base = strrchr(path, '/');
    base = base == NULL ? path : base + 1;
    sep = strchr(base, '_');
    if (sep == NULL || (size_t)(sep-base) != 6u ||
        strlen(sep+1) != 40u || strcmp(sep+37, ".wav") != 0) {
        return false;
    }
    n = 36u;
    if (out_size <= n) return false;
    memcpy(out, sep+1, n);
    out[n] = '\0';
    return device_identity_is_valid_uuid(out);
}

int __real_fsync(int);
int __wrap_fsync(int fd) {
    ++s_fsync_calls;
    if (s_fail_fsync) {
        errno = EIO;
        return -1;
    }
    return __real_fsync(fd);
}
int __real_rename(const char *, const char *);
int __wrap_rename(const char *from, const char *to) {
    ++s_rename_calls;
    if (s_fail_rename) {
        errno = EIO;
        return -1;
    }
    return __real_rename(from, to);
}
