// #52 / IM-015: Device portion of processed ACK / retention.
// Called exclusively during the manual-WAKE, Device-owned, mounted-SD
// recovery seam after WAV/manifest recovery, before a fresh recording starts.
// No USB_SYNC/RELEASE_QUIESCE/SHUTDOWN_ARMED file access is permitted.
#include "processed_ack.h"

#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "cJSON.h"
#include "device_identity.h"
#include "device_manifest.h"
#include "processed_ack_policy.h"
#include "recorder_config.h"
#include "sd_mount.h"

#ifdef ESP_PLATFORM
#include "esp_log.h"
#include "esp_vfs_fat.h"
#define ACK_LOGW(...) ESP_LOGW("processed_ack", __VA_ARGS__)
#else
#define ACK_LOGW(...) ((void)0)
#endif

#define ACK_MAX_BYTES (128u * 1024u)
#define ACK_MAX_IDS 1024u
#define ACK_MARKER_BYTES (RECORDER_UUID_STR_LEN)
// Test overrides affect only a host-compiled sandbox. Production builds
// always use the canonical recorder paths from recorder_config.h.
#ifndef ACK_FILE
#define ACK_FILE RECORDER_M5DAYLOG_DIR "/acks/processed.json"
#endif
#ifndef ACK_MARKER_DIR
#define ACK_MARKER_DIR RECORDER_M5DAYLOG_DIR "/processed-state"
#endif
#ifndef ACK_MANIFEST_PATH
#define ACK_MANIFEST_PATH RECORDER_MANIFEST_PATH
#endif
#ifndef ACK_EVENTS_PATH
#define ACK_EVENTS_PATH RECORDER_EVENTS_PATH
#endif

// Absence is legitimate; all other read errors fail closed.
static int read_bounded(const char *path, size_t max_len, char **out,
                        size_t *len) {
    struct stat st;
    FILE *fp;
    char *buf;
    size_t got;
    *out = NULL;
    *len = 0u;
    if (stat(path, &st) != 0) {
        return errno == ENOENT ? 0 : -1;
    }
    if (!S_ISREG(st.st_mode) || st.st_size < 0 ||
        (uint64_t)st.st_size > max_len) {
        return -1;
    }
    buf = (char *)malloc((size_t)st.st_size + 1u);
    if (buf == NULL) {
        return -1;
    }
    fp = fopen(path, "rb");
    if (fp == NULL) {
        free(buf);
        return -1;
    }
    got = fread(buf, 1u, (size_t)st.st_size, fp);
    {
        bool complete = !ferror(fp) && got == (size_t)st.st_size;
        int closed = fclose(fp);
        if (!complete || closed != 0) {
            free(buf);
            return -1;
        }
    }
    // The input is length-delimited JSON; embedded NUL may not truncate
    // validation and hide attacker-controlled trailing bytes.
    if (memchr(buf, '\0', got) != NULL) {
        free(buf);
        return -1;
    }
    buf[got] = '\0';
    *out = buf;
    *len = got;
    return 1;
}

static void diagnostic(const char *result, const processed_ack_stats_t *s) {
    FILE *fp;
    // Fixed internal diagnostic result identifiers only; neither untrusted
    // JSON nor any raw recording/audio content is ever copied into events.
    fp = fopen(ACK_EVENTS_PATH, "ab");
    if (fp != NULL) {
        (void)fprintf(fp,
                      "{\"type\":\"processed_ack\",\"result\":\"%s\","
                      "\"applied\":%lu,\"alreadyApplied\":%lu,"
                      "\"unknownIds\":%lu,\"invalidPayloads\":%lu,"
                      "\"retentionCandidates\":%lu,\"deletedRaw\":0}\n",
                      result, (unsigned long)s->applied,
                      (unsigned long)s->already_applied,
                      (unsigned long)s->unknown_ids,
                      (unsigned long)s->invalid_payloads,
                      (unsigned long)s->retention_candidates);
        (void)fflush(fp);
        (void)fclose(fp);
    }
    ACK_LOGW("result: %s, applied: %lu, unknown: %lu",
             result, (unsigned long)s->applied, (unsigned long)s->unknown_ids);
}

static bool only_unique_keys(const cJSON *obj) {
    const cJSON *a;
    if (!cJSON_IsObject(obj)) {
        return false;
    }
    for (a = obj->child; a != NULL; a = a->next) {
        const cJSON *b;
        if (a->string == NULL) {
            return false;
        }
        for (b = a->next; b != NULL; b = b->next) {
            if (b->string == NULL || strcmp(a->string, b->string) == 0) {
                return false;
            }
        }
    }
    return true;
}

static bool json_version_one(const cJSON *root) {
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(root, "schemaVersion");
    return cJSON_IsNumber(v) && v->valuedouble == 1.0;
}

static int digit2(const char *p) {
    if (p[0] < '0' || p[0] > '9' || p[1] < '0' || p[1] > '9') {
        return -1;
    }
    return 10 * (p[0] - '0') + p[1] - '0';
}

static bool leap(int y) {
    return (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0);
}

// Conservative RFC3339 check for canonical #35 ACK processedAt. Z and offset,
// optionally fractional seconds, are accepted. Reject impossible dates.
static bool timestamp_epoch(const char *s, int64_t *epoch) {
    static const unsigned days[12] =
        {31,28,31,30,31,30,31,31,30,31,30,31};
    size_t len;
    int y, mon, d, hh, mm, ss, off_h = 0, off_m = 0, sign = 0;
    size_t p = 19u;
    int64_t sum = 0;
    int yr;
    unsigned m;
    if (s == NULL || epoch == NULL) {
        return false;
    }
    len = strlen(s);
    if (len < 20u || len > 64u ||
        s[4] != '-' || s[7] != '-' || s[10] != 'T' ||
        s[13] != ':' || s[16] != ':') {
        return false;
    }
    if (s[0] < '0' || s[0] > '9' || s[1] < '0' || s[1] > '9' ||
        s[2] < '0' || s[2] > '9' || s[3] < '0' || s[3] > '9') {
        return false;
    }
    y = (s[0]-'0')*1000 + (s[1]-'0')*100 +
        (s[2]-'0')*10 + s[3]-'0';
    mon = digit2(s+5);
    d = digit2(s+8);
    hh = digit2(s+11);
    mm = digit2(s+14);
    ss = digit2(s+17);
    if (y < 1970 || y > 2199 || mon < 1 || mon > 12 ||
        d < 1 || d > (int)(days[mon-1] + (mon == 2 && leap(y))) ||
        hh < 0 || hh > 23 || mm < 0 || mm > 59 ||
        ss < 0 || ss > 59) {
        return false;
    }
    if (p < len && s[p] == '.') {
        ++p;
        if (p >= len || s[p] < '0' || s[p] > '9') {
            return false;
        }
        while (p < len && s[p] >= '0' && s[p] <= '9') {
            ++p;
        }
    }
    if (p + 1u == len && s[p] == 'Z') {
        sign = 0;
    } else if (p + 6u == len && (s[p] == '+' || s[p] == '-') &&
               s[p+3] == ':') {
        sign = s[p] == '+' ? 1 : -1;
        off_h = digit2(s+p+1);
        off_m = digit2(s+p+4);
        if (off_h < 0 || off_h > 14 || off_m < 0 || off_m > 59 ||
            (off_h == 14 && off_m != 0)) {
            return false;
        }
    } else {
        return false;
    }
    for (yr = 1970; yr < y; ++yr) {
        sum += leap(yr) ? 366 : 365;
    }
    for (m = 1; m < (unsigned)mon; ++m) {
        sum += days[m-1] + (m == 2 && leap(y));
    }
    sum += d - 1;
    *epoch = sum * 86400 + hh * 3600 + mm * 60 + ss -
             sign * (off_h * 3600 + off_m * 60);
    return true;
}

static bool valid_ack(const cJSON *ack, const char *device_id) {
    const cJSON *id, *time, *ids;
    int64_t epoch;
    int n, i;
    if (!only_unique_keys(ack) || !json_version_one(ack)) {
        return false;
    }
    id = cJSON_GetObjectItemCaseSensitive(ack, "deviceId");
    time = cJSON_GetObjectItemCaseSensitive(ack, "processedAt");
    ids = cJSON_GetObjectItemCaseSensitive(ack, "recordingIds");
    if (!cJSON_IsString(id) ||
        !device_identity_is_valid_uuid(id->valuestring) ||
        strcmp(id->valuestring, device_id) != 0 ||
        !cJSON_IsString(time) ||
        !timestamp_epoch(time->valuestring, &epoch) || !cJSON_IsArray(ids)) {
        return false;
    }
    n = cJSON_GetArraySize(ids);
    if (n < 0 || n > (int)ACK_MAX_IDS) {
        return false;
    }
    for (i = 0; i < n; ++i) {
        const cJSON *r = cJSON_GetArrayItem(ids, i);
        if (!cJSON_IsString(r) ||
            !device_identity_is_valid_uuid(r->valuestring)) {
            return false;
        }
    }
    return true;
}

static bool json_uint(const cJSON *value, uint32_t minimum) {
    double n;
    if (!cJSON_IsNumber(value)) {
        return false;
    }
    n = value->valuedouble;
    // Reject fraction/negative/overflow/NaN before any integer conversion.
    return n >= (double)minimum && n <= (double)UINT32_MAX &&
           n == (double)(uint32_t)n;
}

static bool valid_manifest_entry(const cJSON *entry) {
    const cJSON *rid = cJSON_GetObjectItemCaseSensitive(entry, "recordingId");
    const cJSON *filename = cJSON_GetObjectItemCaseSensitive(entry, "filename");
    const cJSON *started = cJSON_GetObjectItemCaseSensitive(entry, "startedAt");
    const cJSON *duration = cJSON_GetObjectItemCaseSensitive(entry, "durationMs");
    const cJSON *size = cJSON_GetObjectItemCaseSensitive(entry, "sizeBytes");
    const cJSON *sha = cJSON_GetObjectItemCaseSensitive(entry, "sha256");
    const cJSON *sample_rate = cJSON_GetObjectItemCaseSensitive(entry, "sampleRate");
    const cJSON *bit_depth = cJSON_GetObjectItemCaseSensitive(entry, "bitDepth");
    const cJSON *channels = cJSON_GetObjectItemCaseSensitive(entry, "channels");
    const cJSON *state = cJSON_GetObjectItemCaseSensitive(entry, "state");
    const cJSON *fw = cJSON_GetObjectItemCaseSensitive(entry, "firmwareVersion");
    char extracted_id[65];
    int64_t started_epoch;
    if (!only_unique_keys(entry) ||
        !cJSON_IsString(rid) ||
        !device_identity_is_valid_uuid(rid->valuestring) ||
        !cJSON_IsString(filename) || filename->valuestring == NULL ||
        strncmp(filename->valuestring, "recordings/", 11u) != 0 ||
        strstr(filename->valuestring, "..") != NULL ||
        strchr(filename->valuestring, '\\') != NULL ||
        !device_manifest_extract_recording_id(filename->valuestring,
                                               extracted_id, sizeof(extracted_id)) ||
        strcmp(extracted_id, rid->valuestring) != 0 ||
        !cJSON_IsString(started) ||
        !timestamp_epoch(started->valuestring, &started_epoch) ||
        !json_uint(duration, 0u) || !json_uint(size, RECORDER_WAV_HEADER_SIZE) ||
        !cJSON_IsString(sha) ||
        !device_manifest_is_valid_sha256_hex(sha->valuestring) ||
        !json_uint(sample_rate, RECORDER_SAMPLE_RATE_HZ) ||
        sample_rate->valuedouble != RECORDER_SAMPLE_RATE_HZ ||
        !json_uint(bit_depth, RECORDER_BITS_PER_SAMPLE) ||
        bit_depth->valuedouble != RECORDER_BITS_PER_SAMPLE ||
        !json_uint(channels, RECORDER_CHANNELS) ||
        channels->valuedouble != RECORDER_CHANNELS ||
        !cJSON_IsString(state) ||
        (strcmp(state->valuestring, "finalized") != 0 &&
         strcmp(state->valuestring, "recovered") != 0) ||
        !cJSON_IsString(fw) || fw->valuestring[0] == '\0') {
        return false;
    }
    return true;
}

static const cJSON *manifest_recordings(const cJSON *manifest,
                                         const char *device_id) {
    const cJSON *id, *entries, *updated;
    const cJSON *a;
    int64_t updated_epoch;
    if (!only_unique_keys(manifest) || !json_version_one(manifest)) {
        return NULL;
    }
    id = cJSON_GetObjectItemCaseSensitive(manifest, "deviceId");
    entries = cJSON_GetObjectItemCaseSensitive(manifest, "recordings");
    updated = cJSON_GetObjectItemCaseSensitive(manifest, "updatedAt");
    if (!cJSON_IsString(id) || strcmp(id->valuestring, device_id) != 0 ||
        !cJSON_IsString(updated) ||
        !timestamp_epoch(updated->valuestring, &updated_epoch) ||
        !cJSON_IsArray(entries)) {
        return NULL;
    }
    for (a = entries->child; a != NULL; a = a->next) {
        const cJSON *rid;
        const cJSON *b;
        if (!valid_manifest_entry(a)) {
            return NULL;
        }
        rid = cJSON_GetObjectItemCaseSensitive(a, "recordingId");
        for (b = a->next; b != NULL; b = b->next) {
            const cJSON *other = cJSON_GetObjectItemCaseSensitive(b, "recordingId");
            if (!cJSON_IsString(other) ||
                strcmp(rid->valuestring, other->valuestring) == 0) {
                return NULL;
            }
        }
    }
    return entries;
}

static const cJSON *lookup_record(const cJSON *entries, const char *id) {
    const cJSON *entry;
    for (entry = entries->child; entry != NULL; entry = entry->next) {
        const cJSON *candidate = cJSON_GetObjectItemCaseSensitive(entry, "recordingId");
        if (cJSON_IsString(candidate) &&
            strcmp(candidate->valuestring, id) == 0) {
            return entry;
        }
    }
    return NULL;
}

static bool marker_path(const char *id, const char *suffix,
                        char *out, size_t out_size) {
    int n;
    if (!device_identity_is_valid_uuid(id)) {
        return false;
    }
    n = snprintf(out, out_size, ACK_MARKER_DIR "/%s.%s", id, suffix);
    return n > 0 && (size_t)n < out_size;
}

// 0 absent, 1 valid, -1 corrupt/I/O failure. Corrupt markers are never
// overwritten or interpreted as processed; the caller fails closed.
static int marker_status(const char *id) {
    char path[RECORDER_MAX_PATH_LEN], *doc = NULL;
    size_t len = 0;
    int r;
    if (!marker_path(id, "applied", path, sizeof(path))) {
        return -1;
    }
    r = read_bounded(path, 64u, &doc, &len);
    if (r == 1) {
        bool valid = len == ACK_MARKER_BYTES &&
                     memcmp(doc, id, RECORDER_UUID_STR_LEN-1) == 0 &&
                     doc[RECORDER_UUID_STR_LEN-1] == '\n';
        free(doc);
        return valid ? 1 : -1;
    }
    return r;
}

static bool ensure_marker_dir(void) {
    struct stat st;
    if (mkdir(ACK_MARKER_DIR, 0775) == 0) {
        return true;
    }
    return errno == EEXIST &&
           stat(ACK_MARKER_DIR, &st) == 0 && S_ISDIR(st.st_mode);
}

static bool commit_marker(const char *id) {
    char path[RECORDER_MAX_PATH_LEN], tmp[RECORDER_MAX_PATH_LEN];
    FILE *fp;
    int status = marker_status(id);
    if (status == 1) {
        return true;
    }
    if (status != 0 || !marker_path(id, "applied", path, sizeof(path)) ||
        !marker_path(id, "tmp", tmp, sizeof(tmp))) {
        return false;
    }
    fp = fopen(tmp, "wb");
    if (fp == NULL) {
        return false;
    }
    if (fwrite(id, 1u, RECORDER_UUID_STR_LEN-1, fp) !=
            RECORDER_UUID_STR_LEN-1 ||
        fwrite("\n", 1u, 1u, fp) != 1u ||
        fflush(fp) != 0 || fsync(fileno(fp)) != 0) {
        (void)fclose(fp);
        return false;
    }
    if (fclose(fp) != 0) {
        return false;
    }
    if (rename(tmp, path) == 0) {
        // Verify the authoritative final after rename. Never unlink it.
        return marker_status(id) == 1;
    }
    // FAT has no need for replace: each recordingId's final is write-once.
    // A duplicate final may have been created just before the rename.
    return marker_status(id) == 1;
}

static unsigned device_usage_percent(void) {
#ifdef ESP_PLATFORM
    uint64_t total = 0, free_bytes = 0;
    if (esp_vfs_fat_info(RECORDER_SD_MOUNT_POINT, &total, &free_bytes) != ESP_OK ||
        total == 0 || free_bytes > total) {
        return 101u;  // unknown: never assert retention eligibility
    }
    return (unsigned)((total - free_bytes) * 100u / total);
#else
    return 101u;
#endif
}

static void report_retention(const cJSON *entries, processed_ack_stats_t *s) {
    const cJSON *entry;
    const unsigned usage = device_usage_percent();
    const time_t now = time(NULL);
    // Decision #19 expressly permits deletion disabled for the PoC.
    // This pass evaluates eligibility but NEVER removes WAV/manifest/markers.
    // Any future delete-enable change requires a crash-safe manifest transaction.
    for (entry = entries->child; entry != NULL; entry = entry->next) {
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(entry, "recordingId");
        const cJSON *started = cJSON_GetObjectItemCaseSensitive(entry, "startedAt");
        int64_t stamp = -1;
        if (!cJSON_IsString(id) || marker_status(id->valuestring) != 1 ||
            !cJSON_IsString(started) ||
            !timestamp_epoch(started->valuestring, &stamp)) {
            continue;
        }
        if (processed_ack_retention_eligible(
                true, stamp, (int64_t)now, usage)) {
            s->retention_candidates++;
        }
    }
}

bool processed_ack_apply_manual_wake(const char *device_id,
                                     processed_ack_stats_t *out) {
    processed_ack_stats_t stats = {0};
    char *ack_doc = NULL, *man_doc = NULL;
    size_t ack_len = 0, man_len = 0;
    cJSON *ack = NULL, *manifest = NULL;
    const cJSON *entries, *ids;
    int ack_read, man_read, i;
    bool ok = false;
    if (out != NULL) {
        *out = stats;
    }
    if (!sd_mount_is_mounted() ||
        !device_identity_is_valid_uuid(device_id)) {
        return false;
    }
    ack_read = read_bounded(ACK_FILE, ACK_MAX_BYTES, &ack_doc, &ack_len);
    if (ack_read < 0) {
        // Oversize/unreadable files are invalid/untrusted: apply no IDs.
        stats.invalid_payloads = 1;
        diagnostic("invalid-payload", &stats);
        if (out != NULL) *out = stats;
        return true;
    }
    if (ack_read > 0) {
        ack = cJSON_ParseWithLengthOpts(ack_doc, ack_len + 1u, NULL, true);
        free(ack_doc);
        ack_doc = NULL;
        if (ack == NULL || !valid_ack(ack, device_id)) {
            stats.invalid_payloads = 1;
            diagnostic("invalid-payload", &stats);
            ok = true;  // invalid ACK does not prevent new raw recording
            goto done;
        }
    }
    // An absent authoritative ACK is normal. Still re-evaluate durable
    // processed markers during each manual-WAKE recovery, not only on ACK
    // delivery boots. The non-authoritative .tmp is never read.
    man_read = read_bounded(ACK_MANIFEST_PATH,
                            RECORDER_MANIFEST_MAX_BYTES, &man_doc, &man_len);
    if (man_read != 1) {
        diagnostic("manifest-read-error", &stats);
        goto done;
    }
    manifest = cJSON_ParseWithLengthOpts(man_doc, man_len + 1u, NULL, true);
    free(man_doc);
    man_doc = NULL;
    entries = manifest_recordings(manifest, device_id);
    if (entries == NULL) {
        diagnostic("invalid-manifest", &stats);
        goto done;
    }
    if (ack != NULL && !ensure_marker_dir()) {
        diagnostic("marker-directory-error", &stats);
        goto done;
    }
    ids = ack != NULL ?
          cJSON_GetObjectItemCaseSensitive(ack, "recordingIds") : NULL;
    for (i = 0; ids != NULL && i < cJSON_GetArraySize(ids); ++i) {
        const cJSON *item = cJSON_GetArrayItem(ids, i);
        const char *id = item->valuestring;
        int prior;
        if (lookup_record(entries, id) == NULL) {
            stats.unknown_ids++;
            continue;
        }
        prior = marker_status(id);
        if (prior == 1) {
            stats.already_applied++;
            continue;
        }
        if (prior != 0 || !commit_marker(id)) {
            diagnostic("marker-commit-error", &stats);
            goto done;
        }
        stats.applied++;
    }
    report_retention(entries, &stats);
    diagnostic(ack_read == 0 ? "absent" : "ok", &stats);
    ok = true;
done:
    free(man_doc);
    free(ack_doc);
    cJSON_Delete(manifest);
    cJSON_Delete(ack);
    if (out != NULL) {
        *out = stats;
    }
    return ok;
}
