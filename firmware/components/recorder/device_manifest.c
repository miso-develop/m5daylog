// Device manifest / integrity — Task #47 (IM-010). Stdio + dirent +
// sys/stat only; no ESP-IDF dependency.
//
// Manifest updates never truncate a committed manifest.json in place.
// Manifest durability protocol (logical crash-recovery candidate; physical
// FAT32/power-interruption qualification is a separate acceptance gate):
// - M1 is fully written to manifest.tmp, flushed, fsynced on the ESP
//   target, closed and reread/validated before success is reported.
// - A FAT target-exists rename failure preserves M0 under manifest.json.bak
//   before renaming the complete M1 into place. A first rename EIO or any
//   unqualified error never authorizes deletion of the previous final.
// - Recovery with both M1 and M0 validates their complete canonical shapes
//   and requires M1 to contain the exact M0 recording entry prefix. When M1
//   has not published, M0 is restored from backup. Ambiguous states stay
//   available as artifacts and cause a fail-closed boot, never an empty
//   manifest reset.
// - A previous validated M0 fallback must be retired before the next
//   append, with M1 proven to extend it. Only manifest artifacts (.tmp,
//   .bak) are removed by this module; no WAV file is ever deleted.
// - No algorithm can reconstruct physically destroyed FAT sectors. Exact
//   ESP-IDF v5.5.5 + FAT fault/power-cycle tests remain required.

#include "device_manifest.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#if defined(ESP_PLATFORM) || defined(DEVICE_MANIFEST_NATIVE_SYNC_TEST)
#include <unistd.h>
#endif

#include "device_identity.h"
#include "sha256.h"

static bool is_digit(char c) {
    return c >= '0' && c <= '9';
}

// --- Validation ----------------------------------------------------------

bool device_manifest_is_valid_sha256_hex(const char *s) {
    return sha256_is_valid_hex(s);
}

bool device_manifest_is_valid_iso8601_offset(const char *s) {
    // `YYYY-MM-DDTHH:MM:SS+HH:MM` (25 chars).
    unsigned i;
    int mon;
    int day;
    int hour;
    int min;
    int sec;
    int oh;
    int om;
    if (s == NULL || strlen(s) != 25u) {
        return false;
    }
    if (s[4] != '-' || s[7] != '-' || s[10] != 'T' || s[13] != ':' ||
        s[16] != ':' || (s[19] != '+' && s[19] != '-') || s[22] != ':') {
        return false;
    }
    for (i = 0; i < 25u; ++i) {
        if (i == 4u || i == 7u || i == 10u || i == 13u || i == 16u ||
            i == 19u || i == 22u) {
            continue;
        }
        if (!is_digit(s[i])) {
            return false;
        }
    }
    mon = (s[5] - '0') * 10 + (s[6] - '0');
    day = (s[8] - '0') * 10 + (s[9] - '0');
    hour = (s[11] - '0') * 10 + (s[12] - '0');
    min = (s[14] - '0') * 10 + (s[15] - '0');
    sec = (s[17] - '0') * 10 + (s[18] - '0');
    oh = (s[20] - '0') * 10 + (s[21] - '0');
    om = (s[23] - '0') * 10 + (s[24] - '0');
    if (mon < 1 || mon > 12 || day < 1 || day > 31) {
        return false;
    }
    if (hour > 23 || min > 59 || sec > 60) {
        return false;
    }
    if (oh > 14 || om > 59) {
        return false;
    }
    return true;
}

static void put2(char *out, unsigned v) {
    out[0] = (char)('0' + (v / 10u) % 10u);
    out[1] = (char)('0' + v % 10u);
}

bool device_manifest_format_iso8601_utc(int year, int mon, int mday, int hour,
                                        int min, int sec,
                                        char out[RECORDER_ISO8601_STR_LEN]) {
    char buf[RECORDER_ISO8601_STR_LEN];
    if (out == NULL) {
        return false;
    }
    memset(out, 0, RECORDER_ISO8601_STR_LEN);
    if (year < 1970 || year > 2100 || mon < 1 || mon > 12 || mday < 1 ||
        mday > 31 || hour < 0 || hour > 23 || min < 0 || min > 59 || sec < 0 ||
        sec > 60) {
        return false;
    }
    buf[0] = (char)('0' + (year / 1000) % 10);
    buf[1] = (char)('0' + (year / 100) % 10);
    buf[2] = (char)('0' + (year / 10) % 10);
    buf[3] = (char)('0' + year % 10);
    buf[4] = '-';
    put2(buf + 5, (unsigned)mon);
    buf[7] = '-';
    put2(buf + 8, (unsigned)mday);
    buf[10] = 'T';
    put2(buf + 11, (unsigned)hour);
    buf[13] = ':';
    put2(buf + 14, (unsigned)min);
    buf[16] = ':';
    put2(buf + 17, (unsigned)sec);
    buf[19] = '+';
    buf[20] = '0';
    buf[21] = '0';
    buf[22] = ':';
    buf[23] = '0';
    buf[24] = '0';
    buf[25] = '\0';
    memcpy(out, buf, RECORDER_ISO8601_STR_LEN);
    return device_manifest_is_valid_iso8601_offset(out);
}

uint32_t device_manifest_duration_ms(uint32_t pcm_bytes) {
    // 32000 payload bytes per second at 16kHz/16bit/mono.
    return pcm_bytes / 32u;
}

// --- Entry / filename ----------------------------------------------------

static bool safe_free_text(const char *s, size_t max_len) {
    size_t i;
    if (s == NULL || s[0] == '\0' || strlen(s) > max_len) {
        return false;
    }
    for (i = 0; s[i] != '\0'; ++i) {
        if (s[i] == '"' || s[i] == '\\' || (unsigned char)s[i] < 0x20u) {
            return false;
        }
    }
    return true;
}

static bool valid_filename(const char *name) {
    size_t len;
    size_t i;
    if (name == NULL) {
        return false;
    }
    len = strlen(name);
    if (len < 5u || len > 180u) {
        return false;
    }
    // Must be the M5DAYLOG-relative finalized path, never absolute and
    // never a `.part`.
    if (strncmp(name, "recordings/", 11) != 0) {
        return false;
    }
    if (len < 4u || strcmp(name + len - 4u, ".wav") != 0) {
        return false;
    }
    if (len >= 9u && strcmp(name + len - 9u, ".wav.part") == 0) {
        return false;
    }
    for (i = 0; i < len; ++i) {
        char c = name[i];
        if (c == '"' || c == '\\' || (unsigned char)c < 0x20u) {
            return false;
        }
    }
    return true;
}

bool device_manifest_build_filename(const char *date_yyyy_mm_dd,
                                    const char *time_hhmmss,
                                    const char *recording_id, char *out,
                                    size_t out_size) {
    int needed;
    size_t i;
    size_t len;
    if (out == NULL || out_size == 0u) {
        return false;
    }
    out[0] = '\0';
    if (date_yyyy_mm_dd == NULL || strlen(date_yyyy_mm_dd) != 10u ||
        date_yyyy_mm_dd[4] != '-' || date_yyyy_mm_dd[7] != '-') {
        return false;
    }
    for (i = 0; i < 10u; ++i) {
        if (i == 4u || i == 7u) {
            continue;
        }
        if (!is_digit(date_yyyy_mm_dd[i])) {
            return false;
        }
    }
    if (time_hhmmss == NULL || strlen(time_hhmmss) != 6u) {
        return false;
    }
    for (i = 0; i < 6u; ++i) {
        if (!is_digit(time_hhmmss[i])) {
            return false;
        }
    }
    if (recording_id == NULL || recording_id[0] == '\0') {
        return false;
    }
    len = strlen(recording_id);
    if (len == 0u || len > 64u) {
        return false;
    }
    for (i = 0; i < len; ++i) {
        char c = recording_id[i];
        if (c == '/' || c == '\\' || c == '"' || (unsigned char)c < 0x20u) {
            return false;
        }
    }
    needed = snprintf(out, out_size, "recordings/%s/%s_%s.wav",
                      date_yyyy_mm_dd, time_hhmmss, recording_id);
    if (needed < 0 || (size_t)needed >= out_size) {
        out[0] = '\0';
        return false;
    }
    return valid_filename(out);
}

bool device_manifest_build_entry(const char *recording_id,
                                 const char *filename, const char *started_at,
                                 uint32_t duration_ms, uint32_t size_bytes,
                                 const char *sha256_hex, unsigned sample_rate_hz,
                                 unsigned bit_depth, unsigned channels,
                                 const char *state,
                                 const char *firmware_version, char *out,
                                 size_t out_size) {
    int needed;
    if (firmware_version == NULL) {
        firmware_version = RECORDER_FIRMWARE_VERSION;
    }
    if (out == NULL || out_size == 0u) {
        return false;
    }
    out[0] = '\0';
    // recordingId is the cross-system primary key: require a UUID so
    // pre-identity placeholder names can never pollute the manifest.
    if (!device_identity_is_valid_uuid(recording_id)) {
        return false;
    }
    if (!valid_filename(filename)) {
        return false;
    }
    if (!device_manifest_is_valid_iso8601_offset(started_at)) {
        return false;
    }
    if (!device_manifest_is_valid_sha256_hex(sha256_hex)) {
        return false;
    }
    if (state == NULL ||
        (strcmp(state, DEVICE_MANIFEST_STATE_FINALIZED) != 0 &&
         strcmp(state, DEVICE_MANIFEST_STATE_RECOVERED) != 0)) {
        return false;
    }
    if (!safe_free_text(firmware_version, 32u)) {
        return false;
    }
    // Fixed PoC audio contract: the entry must describe what was recorded.
    if (sample_rate_hz != RECORDER_SAMPLE_RATE_HZ ||
        bit_depth != RECORDER_BITS_PER_SAMPLE ||
        channels != RECORDER_CHANNELS) {
        return false;
    }
    if (size_bytes < RECORDER_WAV_HEADER_SIZE) {
        return false;
    }
    needed = snprintf(out, out_size,
                      "{\"recordingId\":\"%s\",\"filename\":\"%s\","
                      "\"startedAt\":\"%s\",\"durationMs\":%u,"
                      "\"sizeBytes\":%u,\"sha256\":\"%s\","
                      "\"sampleRate\":%u,\"bitDepth\":%u,\"channels\":%u,"
                      "\"state\":\"%s\",\"firmwareVersion\":\"%s\"}",
                      recording_id, filename, started_at,
                      (unsigned)duration_ms, (unsigned)size_bytes, sha256_hex,
                      sample_rate_hz, bit_depth, channels, state,
                      firmware_version);
    if (needed < 0 || (size_t)needed >= out_size) {
        out[0] = '\0';
        return false;
    }
    return true;
}

bool device_manifest_build_empty(const char *device_id, const char *updated_at,
                                 char *out, size_t out_size) {
    int needed;
    if (out == NULL || out_size == 0u) {
        return false;
    }
    out[0] = '\0';
    if (!device_identity_is_valid_uuid(device_id)) {
        return false;
    }
    if (!device_manifest_is_valid_iso8601_offset(updated_at)) {
        return false;
    }
    needed = snprintf(out, out_size,
                      "{\"schemaVersion\":%u,\"deviceId\":\"%s\","
                      "\"updatedAt\":\"%s\",\"recordings\":[]}",
                      (unsigned)RECORDER_METADATA_SCHEMA_VERSION, device_id,
                      updated_at);
    if (needed < 0 || (size_t)needed >= out_size) {
        out[0] = '\0';
        return false;
    }
    return true;
}

bool device_manifest_hash_wav_file(const char *wav_path,
                                   char out_hex[RECORDER_SHA256_HEX_LEN],
                                   uint32_t *out_size_bytes) {
    struct stat st;
    size_t len;
    if (out_hex == NULL) {
        return false;
    }
    out_hex[0] = '\0';
    if (out_size_bytes != NULL) {
        *out_size_bytes = 0;
    }
    if (wav_path == NULL || wav_path[0] == '\0') {
        return false;
    }
    len = strlen(wav_path);
    if (len < 5u || strcmp(wav_path + len - 4u, ".wav") != 0) {
        return false;
    }
    if (len >= 9u && strcmp(wav_path + len - 9u, ".wav.part") == 0) {
        // Only finalized `.wav` is hashed; `.part` files are never
        // entered into the manifest.
        return false;
    }
    if (stat(wav_path, &st) != 0 || st.st_size < 0) {
        return false;
    }
    if (!sha256_file_hex(wav_path, out_hex)) {
        return false;
    }
    if (out_size_bytes != NULL) {
        *out_size_bytes = (uint32_t)st.st_size;
    }
    return true;
}

bool device_manifest_extract_recording_id(const char *wav_path, char *out,
                                          size_t out_size) {
    const char *base;
    const char *us;
    const char *dot;
    size_t id_len;
    size_t i;
    if (out == NULL || out_size == 0u) {
        return false;
    }
    out[0] = '\0';
    if (wav_path == NULL || wav_path[0] == '\0') {
        return false;
    }
    base = strrchr(wav_path, '/');
    {
        const char *back = strrchr(wav_path, '\\');
        const char *start = wav_path;
        if (base != NULL && base + 1 > start) {
            start = base + 1;
        }
        if (back != NULL && back + 1 > start) {
            start = back + 1;
        }
        base = start;
    }
    {
        size_t blen = strlen(base);
        if (blen < 12u) {
            return false;
        }
        // Accept `.wav` or `.wav.part` tails; the id sits between the
        // first `_` after the 6-digit time and the `.wav` suffix.
        if (strstr(base, ".wav") == NULL) {
            return false;
        }
    }
    // Recording shape is `HHMMSS_<id>.wav[.part]`.
    if (strlen(base) < 7u) {
        return false;
    }
    for (i = 0; i < 6u; ++i) {
        if (!is_digit(base[i])) {
            return false;
        }
    }
    if (base[6] != '_') {
        return false;
    }
    us = base + 7u;
    dot = strstr(us, ".wav");
    if (dot == NULL || dot == us) {
        return false;
    }
    id_len = (size_t)(dot - us);
    if (id_len == 0u || id_len + 1u > out_size || id_len > 64u) {
        return false;
    }
    for (i = 0; i < id_len; ++i) {
        char c = us[i];
        if (c == '/' || c == '\\' || c == '"' || c == '\'' ||
            (unsigned char)c <= 0x20u) {
            return false;
        }
    }
    memcpy(out, us, id_len);
    out[id_len] = '\0';
    return true;
}

// --- Manifest file update ------------------------------------------------

static const char *memfind(const char *hay, size_t hay_len, const char *ndl) {
    size_t nlen;
    size_t i;
    if (hay == NULL || ndl == NULL || hay_len == 0u) {
        return NULL;
    }
    nlen = strlen(ndl);
    if (nlen == 0u || hay_len < nlen) {
        return NULL;
    }
    for (i = 0; i + nlen <= hay_len; ++i) {
        if (memcmp(hay + i, ndl, nlen) == 0) {
            return hay + i;
        }
    }
    return NULL;
}

// Extract `"key":"<value>"` string bounds (value without quotes) from a
// bounded buffer starting the search at `from`. Returns false when the
// key, colon, quotes, or terminator is missing.
static bool json_string_field(const char *doc, size_t doc_len,
                              const char *from_key, const char *start_after,
                              const char **v_begin, size_t *v_len) {
    const char *key_at;
    const char *p;
    const char *q;
    if (doc == NULL || from_key == NULL || v_begin == NULL || v_len == NULL) {
        return false;
    }
    key_at =
        memfind(start_after != NULL ? start_after : doc,
                start_after != NULL ? (size_t)((doc + doc_len) - start_after)
                                    : doc_len,
                from_key);
    if (key_at == NULL) {
        return false;
    }
    p = strchr(key_at, ':');
    if (p == NULL || (size_t)(p - doc) >= doc_len) {
        return false;
    }
    p++;
    while ((size_t)(p - doc) < doc_len && (*p == ' ' || *p == '\t')) {
        p++;
    }
    if ((size_t)(p - doc) >= doc_len || *p != '"') {
        return false;
    }
    p++;
    q = p;
    while ((size_t)(q - doc) < doc_len && *q != '"') {
        if (*q == '\\') {
            return false;
        }
        q++;
    }
    if ((size_t)(q - doc) >= doc_len) {
        return false;
    }
    *v_begin = p;
    *v_len = (size_t)(q - p);
    return true;
}

static bool read_file_bounded(const char *path, char **out_buf, size_t *out_len,
                              size_t max_bytes) {
    FILE *fp = NULL;
    struct stat st;
    char *buf = NULL;
    size_t got;
    if (out_buf == NULL || out_len == NULL) {
        return false;
    }
    *out_buf = NULL;
    *out_len = 0;
    if (path == NULL || path[0] == '\0' || max_bytes == 0u) {
        return false;
    }
    if (stat(path, &st) != 0 || st.st_size < 0 ||
        (size_t)st.st_size > max_bytes) {
        return false;
    }
    buf = (char *)malloc((size_t)st.st_size + 1u);
    if (buf == NULL) {
        return false;
    }
    fp = fopen(path, "rb");
    if (fp == NULL) {
        free(buf);
        return false;
    }
    got = fread(buf, 1, (size_t)st.st_size, fp);
    if (fclose(fp) != 0 || got != (size_t)st.st_size) {
        free(buf);
        return false;
    }
    buf[got] = '\0';
    *out_buf = buf;
    *out_len = got;
    return true;
}

// Check the complete writer-generated canonical document, not merely a
// matching deviceId. Device-owned manifests have a fixed field order and
// non-escaped strings. Unknown layouts fail closed pending explicit migration.
// Parse the *writer-emitted canonical v1 wire format* completely, with
// bounded decimal numbers and no escaped/control strings. Rejecting an
// alternative JSON encoding is intentional: any migration requires a
// separately accepted contract; a torn snapshot must never look healthy.
static bool manifest_expect(const char **cursor, const char *end,
                            const char *literal) {
    size_t n = strlen(literal);
    if (*cursor > end || (size_t)(end - *cursor) < n ||
        memcmp(*cursor, literal, n) != 0) {
        return false;
    }
    *cursor += n;
    return true;
}

static bool manifest_string(const char **cursor, const char *end,
                            const char **value, size_t *length) {
    const char *p = *cursor, *start;
    if (p >= end || *p != '"') {
        return false;
    }
    start = ++p;
    while (p < end && *p != '"') {
        if (*p == '\\' || (unsigned char)*p < 0x20u) {
            return false;
        }
        ++p;
    }
    if (p == end) {
        return false;
    }
    *value = start;
    *length = (size_t)(p - start);
    *cursor = p + 1;
    return true;
}

static bool manifest_uint32(const char **cursor, const char *end,
                            uint32_t *value) {
    const char *p = *cursor;
    uint32_t result = 0;
    if (p == end || !is_digit(*p)) {
        return false;
    }
    if (*p == '0' && p + 1 < end && is_digit(p[1])) {
        return false;
    }
    while (p < end && is_digit(*p)) {
        unsigned digit = (unsigned)(*p - '0');
        if (result > (UINT32_MAX - digit) / 10u) {
            return false;
        }
        result = result * 10u + digit;
        ++p;
    }
    *value = result;
    *cursor = p;
    return true;
}

static bool manifest_entry_size(const char *doc, size_t len,
                                uint32_t *value) {
    const char *key = memfind(doc, len, "\"sizeBytes\":");
    const char *p, *end = doc + len;
    if (key == NULL || value == NULL) {
        return false;
    }
    p = key + strlen("\"sizeBytes\":");
    return manifest_uint32(&p, end, value) && p < end &&
           (*p == ',' || *p == '}');
}

static bool manifest_string_equals(const char *value, size_t length,
                                   const char *expected) {
    return length == strlen(expected) &&
           memcmp(value, expected, length) == 0;
}

static bool manifest_iso8601(const char *value, size_t length) {
    char stamp[RECORDER_ISO8601_STR_LEN];
    if (length != 25u) {
        return false;
    }
    memcpy(stamp, value, length);
    stamp[length] = '\0';
    return device_manifest_is_valid_iso8601_offset(stamp);
}

static bool manifest_snapshot_valid(const char *doc, size_t len,
                                    const char *device_id) {
    const char *p, *end;
    const char *v;
    size_t n;
    uint32_t ver, num;
    if (doc == NULL || device_id == NULL || len == 0u) {
        return false;
    }
    p = doc;
    end = doc + len;
    if (memchr(doc, '\0', len) != NULL ||
        !manifest_expect(&p, end, "{\"schemaVersion\":") ||
        !manifest_uint32(&p, end, &ver) ||
        ver != RECORDER_METADATA_SCHEMA_VERSION ||
        !manifest_expect(&p, end, ",\"deviceId\":") ||
        !manifest_string(&p, end, &v, &n) ||
        !manifest_string_equals(v, n, device_id) ||
        !manifest_expect(&p, end, ",\"updatedAt\":") ||
        !manifest_string(&p, end, &v, &n) ||
        !manifest_iso8601(v, n) ||
        !manifest_expect(&p, end, ",\"recordings\":[")) {
        return false;
    }
    while (p < end && *p != ']') {
        const char *id, *name, *sha, *state, *fw;
        size_t id_len, name_len, sha_len, state_len, fw_len;
        char uuid[RECORDER_UUID_STR_LEN];
        char filename[192];
        char hash[RECORDER_SHA256_HEX_LEN];
        char duplicate_needle[64];
        int printed;
        if (!manifest_expect(&p, end, "{\"recordingId\":") ||
            !manifest_string(&p, end, &id, &id_len) ||
            id_len != 36u ||
            !manifest_expect(&p, end, ",\"filename\":") ||
            !manifest_string(&p, end, &name, &name_len) ||
            name_len == 0u || name_len >= sizeof(filename) ||
            !manifest_expect(&p, end, ",\"startedAt\":") ||
            !manifest_string(&p, end, &v, &n) ||
            !manifest_iso8601(v, n) ||
            !manifest_expect(&p, end, ",\"durationMs\":") ||
            !manifest_uint32(&p, end, &num) ||
            !manifest_expect(&p, end, ",\"sizeBytes\":") ||
            !manifest_uint32(&p, end, &num) ||
            num < RECORDER_WAV_HEADER_SIZE ||
            !manifest_expect(&p, end, ",\"sha256\":") ||
            !manifest_string(&p, end, &sha, &sha_len) ||
            sha_len != 64u ||
            !manifest_expect(&p, end, ",\"sampleRate\":") ||
            !manifest_uint32(&p, end, &num) ||
            num != RECORDER_SAMPLE_RATE_HZ ||
            !manifest_expect(&p, end, ",\"bitDepth\":") ||
            !manifest_uint32(&p, end, &num) ||
            num != RECORDER_BITS_PER_SAMPLE ||
            !manifest_expect(&p, end, ",\"channels\":") ||
            !manifest_uint32(&p, end, &num) ||
            num != RECORDER_CHANNELS ||
            !manifest_expect(&p, end, ",\"state\":") ||
            !manifest_string(&p, end, &state, &state_len) ||
            !((manifest_string_equals(state, state_len, "finalized")) ||
              (manifest_string_equals(state, state_len, "recovered"))) ||
            !manifest_expect(&p, end, ",\"firmwareVersion\":") ||
            !manifest_string(&p, end, &fw, &fw_len) ||
            fw_len == 0u || fw_len > 32u ||
            !manifest_expect(&p, end, "}")) {
            return false;
        }
        memcpy(uuid, id, id_len);
        uuid[id_len] = '\0';
        memcpy(filename, name, name_len);
        filename[name_len] = '\0';
        memcpy(hash, sha, sha_len);
        hash[sha_len] = '\0';
        if (!device_identity_is_valid_uuid(uuid) ||
            !valid_filename(filename) ||
            !device_manifest_is_valid_sha256_hex(hash)) {
            return false;
        }
        printed = snprintf(duplicate_needle, sizeof(duplicate_needle),
                           "\"recordingId\":\"%s\"", uuid);
        if (printed < 0 || (size_t)printed >= sizeof(duplicate_needle) ||
            memfind(p, (size_t)(end - p), duplicate_needle) != NULL) {
            return false;
        }
        if (p < end && *p == ',') {
            ++p;
            if (p == end || *p != '{') {
                return false;
            }
        } else if (p < end && *p != ']') {
            return false;
        }
    }
    return manifest_expect(&p, end, "]}") && p == end;
}

static bool manifest_file_valid(const char *path, const char *device_id) {
    char *buf = NULL;
    size_t len = 0;
    bool ok;
    if (!read_file_bounded(path, &buf, &len,
                           RECORDER_MANIFEST_MAX_BYTES)) {
        return false;
    }
    ok = manifest_snapshot_valid(buf, len, device_id);
    free(buf);
    return ok;
}

// A new published snapshot may only extend the previous byte-exact entry
// prefix. No independently committed UUID, hash or metadata can disappear.
static bool manifest_file_extends(const char *candidate_path,
                                  const char *old_path,
                                  const char *device_id) {
    char *new_doc = NULL;
    char *old_doc = NULL;
    size_t new_len = 0, old_len = 0;
    const char *a, *b, *a_end, *b_end;
    bool ok = false;
    if (!read_file_bounded(candidate_path, &new_doc, &new_len,
                           RECORDER_MANIFEST_MAX_BYTES) ||
        !read_file_bounded(old_path, &old_doc, &old_len,
                           RECORDER_MANIFEST_MAX_BYTES)) {
        goto done;
    }
    if (!manifest_snapshot_valid(new_doc, new_len, device_id) ||
        !manifest_snapshot_valid(old_doc, old_len, device_id)) {
        goto done;
    }
    a = memfind(old_doc, old_len, "\"recordings\":[");
    b = memfind(new_doc, new_len, "\"recordings\":[");
    if (a == NULL || b == NULL) {
        goto done;
    }
    a = strchr(a, '[') + 1;
    b = strchr(b, '[') + 1;
    a_end = old_doc + old_len - 2;
    b_end = new_doc + new_len - 2;
    ok = a_end >= a && b_end >= b &&
         (size_t)(b_end - b) >= (size_t)(a_end - a) &&
         memcmp(a, b, (size_t)(a_end - a)) == 0 &&
         (a_end == a || b + (a_end - a) == b_end ||
          b[a_end - a] == ',');
done:
    free(new_doc);
    free(old_doc);
    return ok;
}

static bool manifest_backup_path(const char *manifest_path,
                                 char out[RECORDER_MAX_PATH_LEN]) {
    int n;
    if (manifest_path == NULL || out == NULL) {
        return false;
    }
    n = snprintf(out, RECORDER_MAX_PATH_LEN, "%s.bak", manifest_path);
    return n > 0 && (size_t)n < RECORDER_MAX_PATH_LEN;
}

static bool write_tmp_and_rename(const char *tmp_path,
                                 const char *manifest_path, const char *doc,
                                 size_t doc_len) {
    FILE *fp;
    size_t wrote;
    char backup[RECORDER_MAX_PATH_LEN];
    char doc_device_id[RECORDER_UUID_STR_LEN];
    struct stat st;
    bool had_final;
    if (!device_identity_parse_device_id(doc, doc_len, doc_device_id) ||
        !manifest_snapshot_valid(doc, doc_len, doc_device_id)) {
        return false;
    }
    if (!manifest_backup_path(manifest_path, backup)) {
        return false;
    }
    if (stat(backup, &st) == 0) {
        // The preceding successful append may have kept its independent
        // M0 fallback. Qualify M1 as a complete superset before retiring
        // that backup and starting the next append. An unresolved pair is
        // never overwritten or mistaken for free space.
        if (!manifest_file_extends(manifest_path, backup, doc_device_id) ||
            remove(backup) != 0) {
            return false;
        }
    } else if (errno != ENOENT) {
        return false;
    }
    had_final = stat(manifest_path, &st) == 0;
    if (!had_final && errno != ENOENT) {
        return false;
    }
    fp = fopen(tmp_path, "wb");
    if (fp == NULL) {
        return false;
    }
    wrote = fwrite(doc, 1, doc_len, fp);
    if (wrote != doc_len || fflush(fp) != 0) {
        (void)fclose(fp);
        (void)remove(tmp_path);
        return false;
    }
#if defined(ESP_PLATFORM) || defined(DEVICE_MANIFEST_NATIVE_SYNC_TEST)
    if (fileno(fp) < 0 || fsync(fileno(fp)) != 0) {
        (void)fclose(fp);
        (void)remove(tmp_path);
        return false;
    }
#endif
    if (fclose(fp) != 0) {
        (void)remove(tmp_path);
        return false;
    }
    if (rename(tmp_path, manifest_path) == 0) {
        return manifest_file_valid(manifest_path, doc_device_id);
    }
    // FAT cannot rename over an existing destination. Only a known
    // target-exists error may enter the two-snapshot recovery protocol.
    if (!had_final || errno != EEXIST) {
        return false;  // preserve M0 and complete tmp for diagnosis
    }
    if (rename(manifest_path, backup) != 0) {
        return false;  // original M0 remains authoritative
    }
    if (rename(tmp_path, manifest_path) != 0) {
        // Power loss here leaves M0 at .bak; the boot gate restores it.
        // Preserve .bak and tmp even if the best-effort rollback fails.
        if (stat(manifest_path, &st) != 0 && errno == ENOENT) {
            (void)rename(backup, manifest_path);
        }
        return false;
    }
    // Do not retire the backup unless the new published manifest is
    // readable, structurally valid, and still contains all M0 entries.
    // Validate again against M0 before reporting success. The backup is
    // intentionally left for a later recovery checkpoint.
    return manifest_file_extends(manifest_path, backup, doc_device_id);
}

device_manifest_result_t device_manifest_upsert_file(
    const char *manifest_path, const char *tmp_path, const char *device_id,
    const char *updated_at, const char *entry_json) {
    char *doc = NULL;
    size_t doc_len = 0;
    bool have_doc = false;
    const char *id_begin = NULL;
    size_t id_len = 0;
    const char *sha_begin = NULL;
    size_t sha_len = 0;
    const char *fn_begin = NULL;
    size_t fn_len = 0;
    char entry_id[65];
    char entry_sha[65];
    char entry_fn[192];
    uint32_t entry_size = 0;
    unsigned version = 0;
    char stored_id[RECORDER_UUID_STR_LEN];
    device_manifest_result_t ret = DEVICE_MANIFEST_ERROR;

    if (manifest_path == NULL || manifest_path[0] == '\0' || tmp_path == NULL ||
        tmp_path[0] == '\0') {
        return DEVICE_MANIFEST_ERROR;
    }
    if (!device_identity_is_valid_uuid(device_id)) {
        return DEVICE_MANIFEST_ERROR;
    }
    if (!device_manifest_is_valid_iso8601_offset(updated_at)) {
        return DEVICE_MANIFEST_ERROR;
    }
    if (entry_json == NULL || entry_json[0] == '\0' ||
        strlen(entry_json) > RECORDER_MANIFEST_ENTRY_MAX) {
        return DEVICE_MANIFEST_ERROR;
    }
    // Entry must carry its key fields (fail-closed on malformed input).
    if (!json_string_field(entry_json, strlen(entry_json), "\"recordingId\"",
                           NULL, &id_begin, &id_len) ||
        id_len == 0u || id_len > 64u) {
        return DEVICE_MANIFEST_ERROR;
    }
    if (!json_string_field(entry_json, strlen(entry_json), "\"sha256\"",
                           NULL, &sha_begin, &sha_len) ||
        sha_len != 64u) {
        return DEVICE_MANIFEST_ERROR;
    }
    if (!json_string_field(entry_json, strlen(entry_json), "\"filename\"",
                           NULL, &fn_begin, &fn_len) ||
        fn_len == 0u || fn_len > 180u) {
        return DEVICE_MANIFEST_ERROR;
    }
    if (id_len >= sizeof(entry_id) || sha_len >= sizeof(entry_sha) ||
        fn_len >= sizeof(entry_fn)) {
        return DEVICE_MANIFEST_ERROR;
    }
    memcpy(entry_id, id_begin, id_len);
    entry_id[id_len] = '\0';
    memcpy(entry_sha, sha_begin, sha_len);
    entry_sha[sha_len] = '\0';
    memcpy(entry_fn, fn_begin, fn_len);
    entry_fn[fn_len] = '\0';
    if (!device_identity_is_valid_uuid(entry_id) ||
        !device_manifest_is_valid_sha256_hex(entry_sha) ||
        !valid_filename(entry_fn) ||
        !manifest_entry_size(entry_json, strlen(entry_json), &entry_size)) {
        return DEVICE_MANIFEST_ERROR;
    }

    have_doc = read_file_bounded(manifest_path, &doc, &doc_len,
                                 RECORDER_MANIFEST_MAX_BYTES);
    if (!have_doc) {
        // Missing manifest: create it with this single entry. The new
        // document is heap-allocated (bounded by
        // RECORDER_MANIFEST_MAX_BYTES): it must never live in the writer
        // task frame (see main.c stack-budget note).
        int needed;
        char *fresh = NULL;
        // Distinguish "missing" from "unreadable/too large": only ENOENT
        // creates; other errors fail closed to avoid clobbering.
        {
            struct stat st;
            if (stat(manifest_path, &st) == 0) {
                return DEVICE_MANIFEST_ERROR;
            }
            if (errno != ENOENT) {
                return DEVICE_MANIFEST_ERROR;
            }
        }
        needed = snprintf(NULL, 0u,
                          "{\"schemaVersion\":%u,\"deviceId\":\"%s\","
                          "\"updatedAt\":\"%s\",\"recordings\":[%s]}",
                          (unsigned)RECORDER_METADATA_SCHEMA_VERSION,
                          device_id, updated_at, entry_json);
        if (needed < 0 ||
            (size_t)needed >= RECORDER_MANIFEST_MAX_BYTES) {
            return DEVICE_MANIFEST_ERROR;
        }
        fresh = (char *)malloc((size_t)needed + 1u);
        if (fresh == NULL) {
            return DEVICE_MANIFEST_ERROR;
        }
        {
            int again = snprintf(fresh, (size_t)needed + 1u,
                                 "{\"schemaVersion\":%u,\"deviceId\":\"%s\","
                                 "\"updatedAt\":\"%s\",\"recordings\":[%s]}",
                                 (unsigned)RECORDER_METADATA_SCHEMA_VERSION,
                                 device_id, updated_at, entry_json);
            bool ok = false;
            if (again >= 0 && again == needed) {
                ok = write_tmp_and_rename(tmp_path, manifest_path, fresh,
                                          (size_t)needed);
            }
            free(fresh);
            return ok ? DEVICE_MANIFEST_OK : DEVICE_MANIFEST_ERROR;
        }
    }

    // A previously published file must be a complete canonical snapshot.
    // An arbitrary substring-match is insufficient before appending M1.
    if (!manifest_snapshot_valid(doc, doc_len, device_id)) {
        free(doc);
        return DEVICE_MANIFEST_ERROR;
    }

    // Present: validate identity + major before touching anything.
    if (!device_identity_parse_schema_version(doc, doc_len, &version) ||
        version != RECORDER_METADATA_SCHEMA_VERSION) {
        // Unknown major: fail closed, preserve the on-card manifest.
        free(doc);
        return DEVICE_MANIFEST_ERROR;
    }
    memset(stored_id, 0, sizeof(stored_id));
    if (!device_identity_parse_device_id(doc, doc_len, stored_id) ||
        strcmp(stored_id, device_id) != 0) {
        // deviceId fork: preserve, never auto-overwrite.
        free(doc);
        return DEVICE_MANIFEST_ERROR;
    }
    {
        // Idempotency / CONFLICT probe: exact `"recordingId":"<id>"`
        // (16 chars of framing + up to 64 id chars + NUL = 82 max).
        char needle[96];
        const char *hit;
        int n = snprintf(needle, sizeof(needle), "\"recordingId\":\"%s\"",
                         entry_id);
        if (n < 0 || (size_t)n >= sizeof(needle)) {
            free(doc);
            return DEVICE_MANIFEST_ERROR;
        }
        hit = memfind(doc, doc_len, needle);
        if (hit != NULL) {
            // Same id present: compare the stored hash + filename that
            // FOLLOW this occurrence (bounded to the next id or EOF).
            const char *scope_end;
            const char *next_id;
            const char *s_at;
            const char *f_at;
            char old_sha[65];
            char old_fn[192];
            uint32_t old_size = 0;
            size_t scope_len;
            next_id = memfind(hit + 1, (size_t)((doc + doc_len) - (hit + 1)),
                              "\"recordingId\"");
            scope_end = (next_id != NULL) ? next_id : (doc + doc_len);
            scope_len = (size_t)(scope_end - hit);
            // Temporary NUL-bounded scope for the field scanner.
            char *scope = (char *)malloc(scope_len + 1u);
            if (scope == NULL) {
                free(doc);
                return DEVICE_MANIFEST_ERROR;
            }
            memcpy(scope, hit, scope_len);
            scope[scope_len] = '\0';
            s_at = NULL;
            f_at = NULL;
            {
                const char *sb = NULL;
                size_t sl = 0;
                const char *fb = NULL;
                size_t fl = 0;
                bool ok_s = json_string_field(scope, scope_len, "\"sha256\"",
                                              NULL, &sb, &sl);
                bool ok_f = json_string_field(scope, scope_len,
                                              "\"filename\"", NULL, &fb, &fl);
                (void)s_at;
                (void)f_at;
                if (!ok_s || sl != 64u || !ok_f || fl == 0u ||
                    fl >= sizeof(old_fn) ||
                    !manifest_entry_size(scope, scope_len, &old_size)) {
                    free(scope);
                    free(doc);
                    return DEVICE_MANIFEST_ERROR;
                }
                memcpy(old_sha, sb, sl);
                old_sha[sl] = '\0';
                memcpy(old_fn, fb, fl);
                old_fn[fl] = '\0';
            }
            free(scope);
            if (strcmp(old_sha, entry_sha) == 0 &&
                strcmp(old_fn, entry_fn) == 0 &&
                old_size == entry_size) {
                // Exact duplicate (retry / duplicate sync): idempotent OK
                // with the file untouched.
                free(doc);
                return DEVICE_MANIFEST_OK;
            }
            // Same recordingId, different bytes or name: never overwrite.
            free(doc);
            ret = DEVICE_MANIFEST_CONFLICT;
            return ret;
        }
    }
    {
        // Append: replace updatedAt in place (same 25-char shape) and
        // insert [,]entry before the closing `]` of the recordings array.
        const char *upd_key;
        const char *arr_key;
        const char *arr_open;
        const char *doc_end;
        const char *arr_close = NULL;
        const char *ts_q0;
        const char *ts_q1;
        bool arr_empty = false;
        char *fresh = NULL;
        size_t fresh_len;
        size_t prefix_upd_len;
        size_t middle_len;
        size_t tail_len;
        size_t entry_len = strlen(entry_json);
        size_t pos = 0;
        upd_key = memfind(doc, doc_len, "\"updatedAt\"");
        arr_key = memfind(doc, doc_len, "\"recordings\"");
        if (upd_key == NULL || arr_key == NULL) {
            free(doc);
            return DEVICE_MANIFEST_ERROR;
        }
        ts_q0 = strchr(upd_key, ':');
        if (ts_q0 == NULL) {
            free(doc);
            return DEVICE_MANIFEST_ERROR;
        }
        ts_q0 = strchr(ts_q0, '"');
        if (ts_q0 == NULL) {
            free(doc);
            return DEVICE_MANIFEST_ERROR;
        }
        ts_q0++;
        ts_q1 = strchr(ts_q0, '"');
        if (ts_q1 == NULL || ts_q1 <= ts_q0) {
            free(doc);
            return DEVICE_MANIFEST_ERROR;
        }
        arr_open = strchr(arr_key, '[');
        if (arr_open == NULL) {
            free(doc);
            return DEVICE_MANIFEST_ERROR;
        }
        // Canonical shape only (`updatedAt` precedes the recordings
        // array, as emitted by this module): any other layout fails
        // closed rather than risking a mis-spliced document.
        if (!(ts_q1 < arr_open)) {
            free(doc);
            return DEVICE_MANIFEST_ERROR;
        }
        // Array close = last `]` before the trailing `}` (tolerate one
        // trailing newline after `}`).
        doc_end = doc + doc_len;
        while (doc_end > doc &&
               (*(doc_end - 1) == '\n' || *(doc_end - 1) == '\r' ||
                *(doc_end - 1) == ' ' || *(doc_end - 1) == '\t')) {
            doc_end--;
        }
        if (doc_end - doc < 2 || *(doc_end - 1) != '}' ||
            *(doc_end - 2) != ']') {
            free(doc);
            return DEVICE_MANIFEST_ERROR;
        }
        arr_close = doc_end - 2;
        if (arr_close <= arr_open) {
            free(doc);
            return DEVICE_MANIFEST_ERROR;
        }
        {
            const char *c = arr_open + 1;
            while (c < arr_close &&
                   (*c == ' ' || *c == '\t' || *c == '\n' || *c == '\r')) {
                c++;
            }
            arr_empty = (c == arr_close);
        }
        // New length: old + updatedAt delta + comma? + entry.
        {
            size_t old_ts_len = (size_t)(ts_q1 - ts_q0);
            size_t new_ts_len = strlen(updated_at);
            size_t extra = entry_len + (arr_empty ? 0u : 1u);
            if (new_ts_len != 25u) {
                free(doc);
                return DEVICE_MANIFEST_ERROR;
            }
            if (doc_len - old_ts_len + new_ts_len + extra >
                RECORDER_MANIFEST_MAX_BYTES) {
                free(doc);
                return DEVICE_MANIFEST_ERROR;
            }
            fresh_len = doc_len - old_ts_len + new_ts_len + extra;
        }
        fresh = (char *)malloc(fresh_len + 1u);
        if (fresh == NULL) {
            free(doc);
            return DEVICE_MANIFEST_ERROR;
        }
        // Prefix through the opening updatedAt quote.
        prefix_upd_len = (size_t)(ts_q0 - doc);
        memcpy(fresh + pos, doc, prefix_upd_len);
        pos += prefix_upd_len;
        memcpy(fresh + pos, updated_at, strlen(updated_at));
        pos += strlen(updated_at);
        // Middle: from the closing updatedAt quote up to the array close.
        middle_len = (size_t)(arr_close - ts_q1);
        memcpy(fresh + pos, ts_q1, middle_len);
        pos += middle_len;
        // Insert the entry.
        if (!arr_empty) {
            fresh[pos++] = ',';
        }
        memcpy(fresh + pos, entry_json, entry_len);
        pos += entry_len;
        // Tail: `]}` plus any tolerated trailing whitespace/newline.
        tail_len = (size_t)((doc + doc_len) - arr_close);
        memcpy(fresh + pos, arr_close, tail_len);
        pos += tail_len;
        fresh[pos] = '\0';
        if (pos != fresh_len) {
            free(fresh);
            free(doc);
            return DEVICE_MANIFEST_ERROR;
        }
        if (!write_tmp_and_rename(tmp_path, manifest_path, fresh, fresh_len)) {
            free(fresh);
            free(doc);
            return DEVICE_MANIFEST_ERROR;
        }
        free(fresh);
        free(doc);
        return DEVICE_MANIFEST_OK;
    }
}

bool device_manifest_recover_tmp(const char *manifest_path,
                                 const char *tmp_path, const char *device_id) {
    char backup[RECORDER_MAX_PATH_LEN];
    struct stat st;
    bool final_exists, tmp_exists, backup_exists;
    if (manifest_path == NULL || tmp_path == NULL ||
        !device_identity_is_valid_uuid(device_id) ||
        !manifest_backup_path(manifest_path, backup)) {
        return false;
    }
    final_exists = stat(manifest_path, &st) == 0;
    tmp_exists = stat(tmp_path, &st) == 0;
    backup_exists = stat(backup, &st) == 0;

    if (backup_exists) {
        if (!manifest_file_valid(backup, device_id)) {
            return false;  // preserve evidence; no inferred healthy boot
        }
        if (!final_exists) {
            // Transaction interrupted after M0 moved to backup.
            if (rename(backup, manifest_path) != 0) {
                return false;
            }
            final_exists = true;
        } else if (!manifest_file_extends(manifest_path, backup, device_id)) {
            return false;  // torn M1, missing M0 entry or identity fork
        } else if (remove(backup) != 0) {
            return false;
        }
    }
    if (final_exists) {
        if (!manifest_file_valid(manifest_path, device_id)) {
            return false;
        }
        // A complete old/final M0 is authoritative; a temp from an
        // interrupted pre-publish attempt must not displace it.
        if (tmp_exists && remove(tmp_path) != 0) {
            return false;
        }
        return true;
    }
    if (!tmp_exists || !manifest_file_valid(tmp_path, device_id)) {
        return false;
    }
    // No M0 was present (first creation). Only a complete canonical tmp
    // may be promoted. A partial or mismatched tmp is left untouched.
    return rename(tmp_path, manifest_path) == 0 &&
           manifest_file_valid(manifest_path, device_id);
}


bool device_manifest_has_append_room(const char *manifest_path,
                                     const char *device_id) {
    char *doc = NULL;
    size_t len = 0;
    bool ok = false;
    // Two worst-case entries plus fixed-format delimiters are reserved:
    // the segment being admitted and one recovery/rotation margin.
    // This is metadata admission only; physical free clusters and the
    // full replacement temp are independently checked before recording.
    const size_t reserve = 2u * RECORDER_MANIFEST_ENTRY_MAX + 64u;
    if (manifest_path == NULL || device_id == NULL ||
        !read_file_bounded(manifest_path, &doc, &len,
                           RECORDER_MANIFEST_MAX_BYTES)) {
        return false;
    }
    ok = manifest_snapshot_valid(doc, len, device_id) &&
         len <= RECORDER_MANIFEST_MAX_BYTES &&
         reserve <= RECORDER_MANIFEST_MAX_BYTES - len;
    free(doc);
    return ok;
}

static device_manifest_scan_error_t s_scan_error = DEVICE_MANIFEST_SCAN_OK;

device_manifest_scan_error_t device_manifest_last_scan_error(void) {
    return s_scan_error;
}

const char *device_manifest_scan_error_name(device_manifest_scan_error_t e) {
    switch (e) {
        case DEVICE_MANIFEST_SCAN_OK: return "OK";
        case DEVICE_MANIFEST_SCAN_INVALID_INPUT: return "INVALID_INPUT";
        case DEVICE_MANIFEST_SCAN_DIRECTORY_ERROR: return "DIRECTORY_ERROR";
        case DEVICE_MANIFEST_SCAN_WAV_UNREADABLE: return "WAV_UNREADABLE";
        case DEVICE_MANIFEST_SCAN_INDEX_CONFLICT: return "INDEX_CONFLICT";
        case DEVICE_MANIFEST_SCAN_INDEX_WRITE_ERROR: return "INDEX_WRITE_ERROR";
        default: return "UNKNOWN";
    }
}

// --- Boot sync of finalized WAVs ------------------------------------------

static bool path_is_dir_local(const char *path) {
    struct stat st;
    return path != NULL && stat(path, &st) == 0 && S_ISDIR(st.st_mode);
}

static bool audience_wav_name(const char *name) {
    size_t len;
    if (name == NULL) {
        return false;
    }
    len = strlen(name);
    if (len < 5u || strcmp(name + len - 4u, ".wav") != 0) {
        return false;
    }
    if (len >= 9u && strcmp(name + len - 9u, ".wav.part") == 0) {
        return false;
    }
    return true;
}

static bool split_wav_name(const char *base, char time6[7]) {
    unsigned i;
    if (base == NULL || time6 == NULL) {
        return false;
    }
    if (strlen(base) < 11u) {
        return false;
    }
    for (i = 0; i < 6u; ++i) {
        if (base[i] < '0' || base[i] > '9') {
            return false;
        }
    }
    if (base[6] != '_') {
        return false;
    }
    memcpy(time6, base, 6u);
    time6[6] = '\0';
    return true;
}

static bool valid_date_dir(const char *name) {
    unsigned i;
    if (name == NULL || strlen(name) != 10u) {
        return false;
    }
    if (name[4] != '-' || name[7] != '-') {
        return false;
    }
    for (i = 0; i < 10u; ++i) {
        if (i == 4u || i == 7u) {
            continue;
        }
        if (name[i] < '0' || name[i] > '9') {
            return false;
        }
    }
    return true;
}

// Ensure one `.wav` has a manifest entry. Missing entries are appended;
// present ids are left untouched (idempotent); unparseable names,
// hash failures, or non-UUID ids are skipped (not fatal).
static bool ensure_one_wav(const char *wav_path, const char *rel_filename,
                           const char *date_dir_or_null, const char *base,
                           const char *manifest_path, const char *tmp_path,
                           const char *device_id, const char *updated_at,
                           const char *state, uint32_t *added,
                           uint32_t *skipped) {
    char rec_id[65];
    char sha[RECORDER_SHA256_HEX_LEN];
    uint32_t size_bytes = 0;
    uint32_t pcm_bytes = 0;
    uint32_t duration = 0;
    char started[RECORDER_ISO8601_STR_LEN];
    char time6[7];
    char date_part[11];
    // Heap-allocated entry (1KB must not live in the writer task frame;
    // see main.c stack-budget note).
    char *entry = NULL;
    device_manifest_result_t r;
    if (!device_manifest_extract_recording_id(base, rec_id, sizeof(rec_id))) {
        if (skipped != NULL) {
            (*skipped)++;
        }
        return true;
    }
    // Only UUID identities enter the manifest (primary-key rule).
    if (!device_identity_is_valid_uuid(rec_id)) {
        if (skipped != NULL) {
            (*skipped)++;
        }
        return true;
    }
    if (date_dir_or_null != NULL) {
        if (!valid_date_dir(date_dir_or_null) ||
            !split_wav_name(base, time6)) {
            if (skipped != NULL) {
                (*skipped)++;
            }
            return true;
        }
        memcpy(date_part, date_dir_or_null, 11u);
    } else {
        // Top-level legacy file without a date directory: derive the
        // date from the only remaining source — reject, since startedAt
        // would be fabricated. Never invent timestamps.
        if (skipped != NULL) {
            (*skipped)++;
        }
        return true;
    }
    {
        // startedAt `YYYY-MM-DDTHH:MM:SS+00:00` from the date directory
        // plus the `HHMMSS` name prefix (UTC; the wall clock was the
        // source of both at capture time).
        int n = snprintf(started, sizeof(started),
                         "%.4s-%.2s-%.2sT%.2s:%.2s:%.2s+00:00", date_part,
                         date_part + 5, date_part + 8, time6, time6 + 2,
                         time6 + 4);
        if (n < 0 || (size_t)n >= sizeof(started) ||
            !device_manifest_is_valid_iso8601_offset(started)) {
            if (skipped != NULL) {
                (*skipped)++;
            }
            return true;
        }
    }
    if (!device_manifest_hash_wav_file(wav_path, sha, &size_bytes) ||
        size_bytes < RECORDER_WAV_HEADER_SIZE) {
        // A canonical UUID WAV that could not be hashed/parsed cannot be
        // ignored as "legacy": the PC would otherwise sync a partial tree.
        s_scan_error = DEVICE_MANIFEST_SCAN_WAV_UNREADABLE;
        if (skipped != NULL) {
            (*skipped)++;
        }
        return false;
    }
    pcm_bytes = size_bytes - RECORDER_WAV_HEADER_SIZE;
    pcm_bytes -= (pcm_bytes % RECORDER_BYTES_PER_SAMPLE);
    duration = device_manifest_duration_ms(pcm_bytes);
    entry = (char *)malloc(RECORDER_MANIFEST_ENTRY_MAX);
    if (entry == NULL) {
        s_scan_error = DEVICE_MANIFEST_SCAN_INDEX_WRITE_ERROR;
        if (skipped != NULL) {
            (*skipped)++;
        }
        return false;
    }
    if (!device_manifest_build_entry(rec_id, rel_filename, started, duration,
                                     size_bytes, sha, RECORDER_SAMPLE_RATE_HZ,
                                     RECORDER_BITS_PER_SAMPLE,
                                     RECORDER_CHANNELS, state, NULL, entry,
                                     RECORDER_MANIFEST_ENTRY_MAX)) {
        free(entry);
        s_scan_error = DEVICE_MANIFEST_SCAN_INDEX_WRITE_ERROR;
        if (skipped != NULL) {
            (*skipped)++;
        }
        return false;
    }
    r = device_manifest_upsert_file(manifest_path, tmp_path, device_id,
                                    updated_at, entry);
    free(entry);
    if (r != DEVICE_MANIFEST_OK) {
        s_scan_error = (r == DEVICE_MANIFEST_CONFLICT)
            ? DEVICE_MANIFEST_SCAN_INDEX_CONFLICT
            : DEVICE_MANIFEST_SCAN_INDEX_WRITE_ERROR;
    }
    if (r == DEVICE_MANIFEST_OK) {
        if (added != NULL) {
            (*added)++;
        }
        return true;
    }
    if (skipped != NULL) {
        (*skipped)++;
    }
    // CONFLICT/ERROR means a valid finalized/recovered WAV is not
    // provably indexed. Preserve source RAW, but stop recording and
    // prohibit a healthy USB publication until repaired.
    return false;
}

bool device_manifest_sync_wav_dir(const char *recordings_dir,
                                  const char *manifest_path,
                                  const char *tmp_path, const char *device_id,
                                  const char *updated_at, const char *state,
                                  uint32_t *out_added, uint32_t *out_skipped) {
    DIR *dir = NULL;
    struct dirent *ent = NULL;
    uint32_t added = 0;
    uint32_t skipped = 0;
    s_scan_error = DEVICE_MANIFEST_SCAN_INVALID_INPUT;
    if (out_added != NULL) {
        *out_added = 0;
    }
    if (out_skipped != NULL) {
        *out_skipped = 0;
    }
    if (recordings_dir == NULL || recordings_dir[0] == '\0' ||
        manifest_path == NULL || manifest_path[0] == '\0' || tmp_path == NULL ||
        tmp_path[0] == '\0') {
        return false;
    }
    if (!device_identity_is_valid_uuid(device_id) ||
        !device_manifest_is_valid_iso8601_offset(updated_at)) {
        return false;
    }
    if (state == NULL || (strcmp(state, DEVICE_MANIFEST_STATE_FINALIZED) != 0 &&
                          strcmp(state, DEVICE_MANIFEST_STATE_RECOVERED) != 0)) {
        return false;
    }
    dir = opendir(recordings_dir);
    if (dir == NULL) {
        s_scan_error = DEVICE_MANIFEST_SCAN_DIRECTORY_ERROR;
        return false;
    }
    s_scan_error = DEVICE_MANIFEST_SCAN_OK;
    for (;;) {
        char child[RECORDER_MAX_PATH_LEN];
        int needed;
        errno = 0;
        ent = readdir(dir);
        if (ent == NULL) {
            if (errno != 0) {
                (void)closedir(dir);
                return false;  // FAT enumeration/I/O fault
            }
            break;
        }
        if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) {
            continue;
        }
        needed = snprintf(child, sizeof(child), "%s/%s", recordings_dir,
                          ent->d_name);
        if (needed < 0 || (size_t)needed >= sizeof(child)) {
            // Unknown directory content cannot be classified as excluded;
            // there may be a canonical UUID WAV hidden by path truncation.
            (void)closedir(dir);
            return false;
        }
        if (valid_date_dir(ent->d_name) && !path_is_dir_local(child)) {
            // A canonical date subtree must be enumerable. A stat failure,
            // reparse error or unexpected file type forbids healthy sync.
            (void)closedir(dir);
            return false;
        }
        if (path_is_dir_local(child)) {
            DIR *sub = NULL;
            struct dirent *subent = NULL;
            if (!valid_date_dir(ent->d_name)) {
                continue;
            }
            sub = opendir(child);
            if (sub == NULL) {
                (void)closedir(dir);
                return false;  // eligible date subtree inaccessible
            }
            for (;;) {
                char wav[RECORDER_MAX_PATH_LEN];
                errno = 0;
                subent = readdir(sub);
                if (subent == NULL) {
                    if (errno != 0) {
                        (void)closedir(sub);
                        (void)closedir(dir);
                        return false;
                    }
                    break;
                }
                char rel[192];
                int n2;
                int nr;
                if (strcmp(subent->d_name, ".") == 0 ||
                    strcmp(subent->d_name, "..") == 0) {
                    continue;
                }
                if (!audience_wav_name(subent->d_name)) {
                    continue;
                }
                n2 = snprintf(wav, sizeof(wav), "%s/%s", child,
                              subent->d_name);
                if (n2 < 0 || (size_t)n2 >= sizeof(wav)) {
                    (void)closedir(sub);
                    (void)closedir(dir);
                    return false;
                }
                nr = snprintf(rel, sizeof(rel), "recordings/%s/%s",
                              ent->d_name, subent->d_name);
                if (nr < 0 || (size_t)nr >= sizeof(rel)) {
                    (void)closedir(sub);
                    (void)closedir(dir);
                    return false;
                }
                if (!ensure_one_wav(wav, rel, ent->d_name, subent->d_name,
                                    manifest_path, tmp_path, device_id,
                                    updated_at, state, &added, &skipped)) {
                    // Existing RAW and manifest stay on SD for recovery.
                    closedir(sub);
                    closedir(dir);
                    if (out_added != NULL) {
                        *out_added = added;
                    }
                    if (out_skipped != NULL) {
                        *out_skipped = skipped;
                    }
                    return false;
                }
            }
            if (closedir(sub) != 0) {
                (void)closedir(dir);
                return false;
            }
        } else {
            // Top-level `.wav` without a date directory: skipped (no
            // trustworthy startedAt source; never fabricate timestamps).
            if (audience_wav_name(ent->d_name)) {
                skipped++;
            }
        }
    }
    if (closedir(dir) != 0) {
        s_scan_error = DEVICE_MANIFEST_SCAN_DIRECTORY_ERROR;
        return false;
    }
    if (out_added != NULL) {
        *out_added = added;
    }
    if (out_skipped != NULL) {
        *out_skipped = skipped;
    }
    return true;
}
