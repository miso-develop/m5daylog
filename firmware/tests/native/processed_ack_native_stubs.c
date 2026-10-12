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

// This is a separate host-only model of internal NVS, NOT a file on MSC.
// Tests may forge SD markers without acquiring this ledger's authority.
#define ACK_TEST_MAX_STATES 128u
static char s_internal_applied[ACK_TEST_MAX_STATES][37];
static size_t s_internal_count = 0u;
static int s_fail_state_commit = 0;

int ack_test_state_status(const char *id) {
    size_t i;
    for (i = 0u; i < s_internal_count; ++i) {
        if (strcmp(s_internal_applied[i], id) == 0) return 1;
    }
    return 0;
}
bool ack_test_state_commit(const char *id) {
    if (s_fail_state_commit) return false;
    if (ack_test_state_status(id) == 1) return true;
    if (s_internal_count == ACK_TEST_MAX_STATES || strlen(id) != 36u) {
        return false;
    }
    memcpy(s_internal_applied[s_internal_count++], id, 37u);
    return true;
}
void ack_test_fail_state_commit(int v) { s_fail_state_commit = v; }

static unsigned s_usage_percent = 101u;
unsigned ack_test_usage_percent(void) { return s_usage_percent; }
void ack_test_set_usage_percent(unsigned v) { s_usage_percent = v; }
static int s_sd_mounted = 1;
static int s_fail_fsync = 0;
static int s_fail_open = 0;
static int s_fail_write = 0;
static int s_fail_flush = 0;
static int s_fail_close = 0;
static FILE *s_marker_fp = NULL;
static int s_fail_rename = 0;
static int s_fsync_calls = 0;
static int s_rename_calls = 0;

void ack_test_set_mounted(int v) { s_sd_mounted = v; }
void ack_test_fail_fsync(int v) { s_fail_fsync = v; }
void ack_test_fail_open(int v) { s_fail_open = v; }
void ack_test_fail_write(int v) { s_fail_write = v; }
void ack_test_fail_flush(int v) { s_fail_flush = v; }
void ack_test_fail_close(int v) { s_fail_close = v; }
void ack_test_fail_rename(int v) { s_fail_rename = v; }
int ack_test_fsync_calls(void) { return s_fsync_calls; }
int ack_test_rename_calls(void) { return s_rename_calls; }
void ack_test_reset_faults(void) {
    s_fail_fsync = 0; s_fail_rename = 0;
    s_fail_open = 0; s_fail_write = 0;
    s_fail_flush = 0; s_fail_close = 0;
    s_marker_fp = NULL;
    s_fsync_calls = 0; s_rename_calls = 0;
    s_sd_mounted = 1;
    s_internal_count = 0u;
    s_fail_state_commit = 0;
    s_usage_percent = 101u;
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


// Interpose only the temporary marker file. The production read path and
// events diagnostics remain normal. A failed temporary write/flush/close
// must never promote a partial applied marker.
FILE *__real_fopen(const char *, const char *);
FILE *__wrap_fopen(const char *path, const char *mode) {
    bool marker_write = path != NULL && mode != NULL &&
                        strstr(path, "processed-state/") != NULL &&
                        strchr(mode, 'w') != NULL;
    FILE *fp;
    if (marker_write && s_fail_open) {
        errno = EIO;
        return NULL;
    }
    fp = __real_fopen(path, mode);
    if (marker_write) s_marker_fp = fp;
    return fp;
}

size_t __real_fwrite(const void *, size_t, size_t, FILE *);
size_t __wrap_fwrite(const void *ptr, size_t sz, size_t n, FILE *fp) {
    if (s_fail_write && fp == s_marker_fp) {
        errno = EIO;
        return 0u;
    }
    return __real_fwrite(ptr, sz, n, fp);
}
int __real_fflush(FILE *);
int __wrap_fflush(FILE *fp) {
    if (s_fail_flush && fp == s_marker_fp) {
        errno = EIO;
        return EOF;
    }
    return __real_fflush(fp);
}
int __real_fclose(FILE *);
int __wrap_fclose(FILE *fp) {
    bool marker = fp != NULL && fp == s_marker_fp;
    int rc = __real_fclose(fp);
    if (marker) {
        s_marker_fp = NULL;
        if (s_fail_close) {
            errno = EIO;
            return EOF;
        }
    }
    return rc;
}
