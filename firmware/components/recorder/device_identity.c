// Device identity — Task #47 (IM-010). Stdio only except the NVS
// ensure below (`ESP_PLATFORM`).
//
// Durability: new `device.json` files use fflush() plus fsync(fileno())
// on ESP-IDF (same FatFs f_sync path as wav_part.c); failures are
// fail-loud with the file left for a later boot to retry. An existing
// `device.json` with a mismatched deviceId or unknown schemaVersion is
// never overwritten (fail-closed identity preservation, Spec #35).
//
// Never deletes: no remove()/unlink() path exists in this module.

#include "device_identity.h"

#include <stdio.h>
#include <string.h>

#ifdef ESP_PLATFORM
#include <unistd.h>

#include "esp_random.h"
#include "esp_system.h"
#include "nvs.h"
#include "nvs_flash.h"
#endif

static bool is_hex(char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
           (c >= 'A' && c <= 'F');
}

bool device_identity_is_valid_uuid(const char *s) {
    size_t i;
    if (s == NULL || strlen(s) != 36u) {
        return false;
    }
    for (i = 0; i < 36u; ++i) {
        if (i == 8u || i == 13u || i == 18u || i == 23u) {
            if (s[i] != '-') {
                return false;
            }
        } else if (!is_hex(s[i])) {
            return false;
        }
    }
    return true;
}

bool device_identity_format_uuid_v4(const uint8_t rand16[16],
                                    char out[RECORDER_UUID_STR_LEN]) {
    uint8_t b[16];
    static const char *hexd = "0123456789abcdef";
    unsigned pos = 0;
    unsigned i;
    if (rand16 == NULL || out == NULL) {
        return false;
    }
    memcpy(b, rand16, sizeof(b));
    // RFC 4122 version 4 + variant 10xx bits.
    b[6] = (uint8_t)((b[6] & 0x0Fu) | 0x40u);
    b[8] = (uint8_t)((b[8] & 0x3Fu) | 0x80u);
    memset(out, 0, RECORDER_UUID_STR_LEN);
    for (i = 0; i < 16u; ++i) {
        if (i == 4u || i == 6u || i == 8u || i == 10u) {
            out[pos++] = '-';
        }
        out[pos++] = hexd[(b[i] >> 4) & 0x0Fu];
        out[pos++] = hexd[b[i] & 0x0Fu];
    }
    out[36] = '\0';
    return pos == 36u && device_identity_is_valid_uuid(out);
}

static bool safe_token(const char *s) {
    size_t i;
    if (s == NULL || s[0] == '\0' || strlen(s) > 64u) {
        return false;
    }
    for (i = 0; s[i] != '\0'; ++i) {
        if (s[i] == '"' || s[i] == '\\' || (unsigned char)s[i] < 0x20u) {
            return false;
        }
    }
    return true;
}

bool device_identity_build_device_json(const char *device_id,
                                       const char *model,
                                       const char *firmware_version, char *out,
                                       size_t out_size) {
    int needed;
    if (model == NULL) {
        model = RECORDER_MODEL;
    }
    if (firmware_version == NULL) {
        firmware_version = RECORDER_FIRMWARE_VERSION;
    }
    if (out == NULL || out_size == 0u) {
        return false;
    }
    out[0] = '\0';
    if (!device_identity_is_valid_uuid(device_id)) {
        return false;
    }
    if (!safe_token(model) || !safe_token(firmware_version)) {
        return false;
    }
    needed = snprintf(out, out_size,
                      "{\"schemaVersion\":%u,\"deviceId\":\"%s\","
                      "\"model\":\"%s\",\"firmwareVersion\":\"%s\","
                      "\"audioCapabilities\":{\"sampleRate\":%u,"
                      "\"bitDepth\":%u,\"channels\":%u,\"format\":\"pcm\"}}",
                      (unsigned)RECORDER_METADATA_SCHEMA_VERSION, device_id,
                      model, firmware_version,
                      (unsigned)RECORDER_SAMPLE_RATE_HZ,
                      (unsigned)RECORDER_BITS_PER_SAMPLE,
                      (unsigned)RECORDER_CHANNELS);
    if (needed < 0 || (size_t)needed >= out_size) {
        out[0] = '\0';
        return false;
    }
    return true;
}

bool device_identity_parse_schema_version(const char *json, size_t json_len,
                                          unsigned *out_version) {
    const char *key = "\"schemaVersion\"";
    const char *at;
    const char *p;
    unsigned value = 0;
    bool any = false;
    if (json == NULL || json_len == 0u || out_version == NULL) {
        return false;
    }
    // Bounded memmem over the in-memory document.
    at = NULL;
    {
        size_t klen = strlen(key);
        size_t i;
        if (klen == 0u || json_len < klen) {
            return false;
        }
        for (i = 0; i + klen <= json_len; ++i) {
            if (memcmp(json + i, key, klen) == 0) {
                at = json + i;
                break;
            }
        }
    }
    if (at == NULL) {
        return false;
    }
    p = strchr(at, ':');
    if (p == NULL || (size_t)(p - json) >= json_len) {
        return false;
    }
    p++;
    while ((size_t)(p - json) < json_len && (*p == ' ' || *p == '\t')) {
        p++;
    }
    while ((size_t)(p - json) < json_len && *p >= '0' && *p <= '9') {
        any = true;
        value = value * 10u + (unsigned)(*p - '0');
        if (value > 1000000u) {
            return false;
        }
        p++;
    }
    if (!any) {
        return false;
    }
    *out_version = value;
    return true;
}

bool device_identity_parse_device_id(const char *json, size_t json_len,
                                     char out[RECORDER_UUID_STR_LEN]) {
    const char *key = "\"deviceId\"";
    const char *at = NULL;
    const char *p;
    size_t klen;
    size_t i;
    if (out == NULL) {
        return false;
    }
    memset(out, 0, RECORDER_UUID_STR_LEN);
    if (json == NULL || json_len == 0u) {
        return false;
    }
    klen = strlen(key);
    if (klen == 0u || json_len < klen) {
        return false;
    }
    for (i = 0; i + klen <= json_len; ++i) {
        if (memcmp(json + i, key, klen) == 0) {
            at = json + i;
            break;
        }
    }
    if (at == NULL) {
        return false;
    }
    p = strchr(at, ':');
    if (p == NULL || (size_t)(p - json) >= json_len) {
        return false;
    }
    p++;
    while ((size_t)(p - json) < json_len && (*p == ' ' || *p == '\t')) {
        p++;
    }
    if ((size_t)(p - json) >= json_len || *p != '"') {
        return false;
    }
    p++;
    if ((size_t)(p - json) + 36u > json_len) {
        return false;
    }
    memcpy(out, p, 36u);
    out[36] = '\0';
    if (p[36] != '"') {
        out[0] = '\0';
        return false;
    }
    if (!device_identity_is_valid_uuid(out)) {
        out[0] = '\0';
        return false;
    }
    return true;
}

static bool write_all_synced(FILE *fp, const char *doc) {
    size_t len;
    size_t wrote;
    if (fp == NULL || doc == NULL) {
        return false;
    }
    len = strlen(doc);
    wrote = fwrite(doc, 1, len, fp);
    if (wrote != len) {
        return false;
    }
    if (fflush(fp) != 0) {
        return false;
    }
#ifdef ESP_PLATFORM
    {
        int fd = fileno(fp);
        if (fd < 0 || fsync(fd) != 0) {
            return false;
        }
    }
#endif
    return true;
}

bool device_identity_ensure_device_json(const char *path,
                                        const char *device_id,
                                        const char *model,
                                        const char *firmware_version) {
    char doc[512];
    FILE *fp = NULL;
    long size = -1;
    size_t got;
    char existing[RECORDER_UUID_STR_LEN];
    unsigned version = 0;
    if (path == NULL || path[0] == '\0') {
        return false;
    }
    if (!device_identity_is_valid_uuid(device_id)) {
        return false;
    }
    if (!device_identity_build_device_json(device_id, model, firmware_version,
                                           doc, sizeof(doc))) {
        return false;
    }
    fp = fopen(path, "rb");
    if (fp == NULL) {
        // Missing: create with the canonical document.
        FILE *out = fopen(path, "wb");
        bool ok;
        if (out == NULL) {
            return false;
        }
        ok = write_all_synced(out, doc);
        if (fclose(out) != 0) {
            ok = false;
        }
        return ok;
    }
    // Present: read fully (bounded) and compare identity without
    // rewriting. Any mismatch or unknown schema is fail-closed with the
    // existing file preserved (never auto-overwritten).
    if (fseek(fp, 0L, SEEK_END) != 0) {
        fclose(fp);
        return false;
    }
    size = ftell(fp);
    if (size < 0 || size > 8192L) {
        fclose(fp);
        return false;
    }
    if (fseek(fp, 0L, SEEK_SET) != 0) {
        fclose(fp);
        return false;
    }
    {
        // Bounded read: device.json is under 512 bytes by construction;
        // reject anything larger as corrupt. The buffer is function-static
        // (.bss, single owner, no lock) so the writer task frame stays
        // small (see main.c stack-budget note).
        static char local[2048];
        if (size > (long)sizeof(local)) {
            fclose(fp);
            return false;
        }
        got = fread(local, 1, (size_t)size, fp);
        fclose(fp);
        fp = NULL;
        if (got != (size_t)size) {
            return false;
        }
        if (!device_identity_parse_schema_version(local, got, &version) ||
            version != RECORDER_METADATA_SCHEMA_VERSION) {
            return false;
        }
        memset(existing, 0, sizeof(existing));
        if (!device_identity_parse_device_id(local, got, existing)) {
            return false;
        }
        if (strcmp(existing, device_id) != 0) {
            // NVS/device.json fork: preserve the on-card file, fail loud.
            return false;
        }
        return true;
    }
}

#ifdef ESP_PLATFORM
esp_err_t device_identity_ensure_device_id(char out[RECORDER_UUID_STR_LEN]) {
    esp_err_t err;
    nvs_handle_t handle = 0;
    size_t need = RECORDER_UUID_STR_LEN;
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(out, 0, RECORDER_UUID_STR_LEN);
    err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES ||
        err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        if (nvs_flash_erase() != ESP_OK) {
            return ESP_FAIL;
        }
        err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_open("m5daylog", NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_get_str(handle, "device_id", NULL, &need);
    if (err == ESP_OK) {
        // Present: validate before reuse; a corrupt value fails loud
        // rather than forking the device identity silently.
        char stored[RECORDER_UUID_STR_LEN];
        size_t copy = RECORDER_UUID_STR_LEN;
        memset(stored, 0, sizeof(stored));
        err = nvs_get_str(handle, "device_id", stored, &copy);
        nvs_close(handle);
        if (err != ESP_OK || !device_identity_is_valid_uuid(stored)) {
            return ESP_FAIL;
        }
        memcpy(out, stored, RECORDER_UUID_STR_LEN);
        return ESP_OK;
    }
    if (err != ESP_ERR_NVS_NOT_FOUND) {
        nvs_close(handle);
        return err;
    }
    // First boot: 122 random bits from the hardware RNG, formatted UUIDv4.
    {
        uint8_t rand16[16];
        uint32_t w;
        int i;
        char fresh[RECORDER_UUID_STR_LEN];
        for (i = 0; i < 4; ++i) {
            w = esp_random();
            rand16[i * 4 + 0] = (uint8_t)(w >> 24);
            rand16[i * 4 + 1] = (uint8_t)(w >> 16);
            rand16[i * 4 + 2] = (uint8_t)(w >> 8);
            rand16[i * 4 + 3] = (uint8_t)(w);
        }
        memset(fresh, 0, sizeof(fresh));
        if (!device_identity_format_uuid_v4(rand16, fresh)) {
            nvs_close(handle);
            return ESP_FAIL;
        }
        err = nvs_set_str(handle, "device_id", fresh);
        if (err != ESP_OK) {
            nvs_close(handle);
            return ESP_FAIL;
        }
        err = nvs_commit(handle);
        nvs_close(handle);
        if (err != ESP_OK) {
            return err;
        }
        memcpy(out, fresh, RECORDER_UUID_STR_LEN);
        return ESP_OK;
    }
}
#else  // !ESP_PLATFORM — host/test build: NVS unavailable.
esp_err_t device_identity_ensure_device_id(char out[RECORDER_UUID_STR_LEN]) {
    if (out != NULL) {
        memset(out, 0, RECORDER_UUID_STR_LEN);
    }
    (void)out;
    return ESP_ERR_NOT_SUPPORTED;
}
#endif  // ESP_PLATFORM
