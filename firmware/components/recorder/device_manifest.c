// Device manifest / integrity — Task #47 (IM-010). Stdio + dirent +
// sys/stat only; no ESP-IDF dependency.
//
// Durability model (power-loss safe):
// - Updates never truncate `manifest.json` in place. The full new
//   document goes to `manifest.tmp` (fwrite + fflush + fsync on ESP-IDF,
//   fclose-checked) and only then is renamed over the destination.
//   A crash during the tmp write leaves the old manifest untouched.
// - On POSIX the rename overwrites atomically. On ESP-IDF FATFS a rename
//   over an existing name fails with EEXIST, so the module retries once
//   as remove(dest) + rename(tmp, dest): the complete new document is
//   already durable in tmp at that point, and a crash inside that window
//   is recovered at boot by promoting a valid tmp when the destination is
//   missing. `remove()` appears ONLY for this tmp/destination replace
//   (plus best-effort stale-tmp cleanup) — audio files (`.wav`,
//   `.wav.part`) are never removed, unlinked, or overwritten in place:
//   entry collisions report CONFLICT and preserve the on-card manifest.
// - Unknown `schemaVersion` majors and `deviceId` mismatches fail closed
//   without writing, preserving the existing manifest.

#include "device_manifest.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#ifdef ESP_PLATFORM
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
    fclose(fp);
    if (got != (size_t)st.st_size) {
        free(buf);
        return false;
    }
    buf[got] = '\0';
    *out_buf = buf;
    *out_len = got;
    return true;
}

static bool write_tmp_and_rename(const char *tmp_path,
                                 const char *manifest_path, const char *doc,
                                 size_t doc_len) {
    FILE *fp = NULL;
    size_t wrote;
    fp = fopen(tmp_path, "wb");
    if (fp == NULL) {
        return false;
    }
    wrote = fwrite(doc, 1, doc_len, fp);
    if (wrote != doc_len) {
        fclose(fp);
        remove(tmp_path);
        return false;
    }
    if (fflush(fp) != 0) {
        fclose(fp);
        remove(tmp_path);
        return false;
    }
#ifdef ESP_PLATFORM
    {
        int fd = fileno(fp);
        if (fd >= 0) {
            (void)fsync(fd);
        } else {
            fclose(fp);
            remove(tmp_path);
            return false;
        }
    }
#else
    // Host builds: fflush durability is sufficient for contract tests;
    // fsync is an ESP-IDF media concern (see wav_part.c).
    (void)0;
#endif
    if (fclose(fp) != 0) {
        remove(tmp_path);
        return false;
    }
    // Atomic publish: POSIX rename overwrites atomically. ESP-IDF FATFS
    // rename fails when the destination exists, so retry once as
    // remove+rename with the complete tmp already durable (a crash inside
    // that window is recovered at boot by promoting a valid tmp when the
    // destination is missing).
    if (rename(tmp_path, manifest_path) == 0) {
        return true;
    }
#ifdef ESP_PLATFORM
    (void)remove(manifest_path);
    if (rename(tmp_path, manifest_path) == 0) {
        return true;
    }
#else
    if (errno == EEXIST) {
        (void)remove(manifest_path);
        if (rename(tmp_path, manifest_path) == 0) {
            return true;
        }
    }
#endif
    remove(tmp_path);
    return false;
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
        !valid_filename(entry_fn)) {
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
                    fl >= sizeof(old_fn)) {
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
                strcmp(old_fn, entry_fn) == 0) {
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
    struct stat st_dest;
    struct stat st_tmp;
    bool dest_exists;
    bool tmp_exists;
    char *tmp_doc = NULL;
    size_t tmp_len = 0;
    unsigned version = 0;
    char stored[RECORDER_UUID_STR_LEN];
    if (manifest_path == NULL || tmp_path == NULL || device_id == NULL) {
        return false;
    }
    if (!device_identity_is_valid_uuid(device_id)) {
        return false;
    }
    dest_exists = (stat(manifest_path, &st_dest) == 0);
    tmp_exists = (stat(tmp_path, &st_tmp) == 0);
    if (dest_exists) {
        // Destination healthy: drop any stale tmp from an interrupted
        // update (its content, if newer, is re-derivable via the next
        // idempotent upsert).
        if (tmp_exists) {
            (void)remove(tmp_path);
        }
        return true;
    }
    if (!tmp_exists) {
        return false;
    }
    if (!read_file_bounded(tmp_path, &tmp_doc, &tmp_len,
                            RECORDER_MANIFEST_MAX_BYTES)) {
        return false;
    }
    if (!device_identity_parse_schema_version(tmp_doc, tmp_len, &version) ||
        version != RECORDER_METADATA_SCHEMA_VERSION) {
        free(tmp_doc);
        return false;
    }
    memset(stored, 0, sizeof(stored));
    if (!device_identity_parse_device_id(tmp_doc, tmp_len, stored) ||
        strcmp(stored, device_id) != 0) {
        free(tmp_doc);
        return false;
    }
    if (memfind(tmp_doc, tmp_len, "\"recordings\"") == NULL) {
        free(tmp_doc);
        return false;
    }
    free(tmp_doc);
    return rename(tmp_path, manifest_path) == 0;
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
    if (!device_manifest_hash_wav_file(wav_path, sha, &size_bytes)) {
        if (skipped != NULL) {
            (*skipped)++;
        }
        return true;
    }
    if (size_bytes < RECORDER_WAV_HEADER_SIZE) {
        if (skipped != NULL) {
            (*skipped)++;
        }
        return true;
    }
    pcm_bytes = size_bytes - RECORDER_WAV_HEADER_SIZE;
    pcm_bytes -= (pcm_bytes % RECORDER_BYTES_PER_SAMPLE);
    duration = device_manifest_duration_ms(pcm_bytes);
    entry = (char *)malloc(RECORDER_MANIFEST_ENTRY_MAX);
    if (entry == NULL) {
        if (skipped != NULL) {
            (*skipped)++;
        }
        return true;
    }
    if (!device_manifest_build_entry(rec_id, rel_filename, started, duration,
                                     size_bytes, sha, RECORDER_SAMPLE_RATE_HZ,
                                     RECORDER_BITS_PER_SAMPLE,
                                     RECORDER_CHANNELS, state, NULL, entry,
                                     RECORDER_MANIFEST_ENTRY_MAX)) {
        free(entry);
        if (skipped != NULL) {
            (*skipped)++;
        }
        return true;
    }
    r = device_manifest_upsert_file(manifest_path, tmp_path, device_id,
                                    updated_at, entry);
    free(entry);
    if (r == DEVICE_MANIFEST_OK) {
        if (added != NULL) {
            (*added)++;
        }
        return true;
    }
    if (skipped != NULL) {
        (*skipped)++;
    }
    // CONFLICT and ERROR both preserve the manifest; a conflicting or
    // unreadable file never fails the whole boot sync.
    return true;
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
        return false;
    }
    while ((ent = readdir(dir)) != NULL) {
        char child[RECORDER_MAX_PATH_LEN];
        int needed;
        if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) {
            continue;
        }
        needed = snprintf(child, sizeof(child), "%s/%s", recordings_dir,
                          ent->d_name);
        if (needed < 0 || (size_t)needed >= sizeof(child)) {
            skipped++;
            continue;
        }
        if (path_is_dir_local(child)) {
            DIR *sub = NULL;
            struct dirent *subent = NULL;
            if (!valid_date_dir(ent->d_name)) {
                continue;
            }
            sub = opendir(child);
            if (sub == NULL) {
                skipped++;
                continue;
            }
            while ((subent = readdir(sub)) != NULL) {
                char wav[RECORDER_MAX_PATH_LEN];
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
                    skipped++;
                    continue;
                }
                nr = snprintf(rel, sizeof(rel), "recordings/%s/%s",
                              ent->d_name, subent->d_name);
                if (nr < 0 || (size_t)nr >= sizeof(rel)) {
                    skipped++;
                    continue;
                }
                ensure_one_wav(wav, rel, ent->d_name, subent->d_name,
                               manifest_path, tmp_path, device_id, updated_at,
                               state, &added, &skipped);
            }
            closedir(sub);
        } else {
            // Top-level `.wav` without a date directory: skipped (no
            // trustworthy startedAt source; never fabricate timestamps).
            if (audience_wav_name(ent->d_name)) {
                skipped++;
            }
        }
    }
    closedir(dir);
    if (out_added != NULL) {
        *out_added = added;
    }
    if (out_skipped != NULL) {
        *out_skipped = skipped;
    }
    return true;
}
