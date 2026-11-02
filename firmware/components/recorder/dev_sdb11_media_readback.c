#include "dev_sdb11_media_readback.h"

#if defined(ESP_PLATFORM) && defined(CONFIG_M5DAYLOG_SDB11_DEV_INSTALL) && CONFIG_M5DAYLOG_SDB11_DEV_INSTALL
#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

#include "device_identity.h"
#include "device_manifest.h"
#include "recorder_config.h"

// These reads are deliberately side-effect-free: no mkdir, recovery,
// fallback empty manifest, or FAT formatting. The DEV installer writes the
// exact documents; unknown or absent media is never silently enrolled.
static bool dev_sd_file_exact(const char *path, const char *expected) {
    const size_t len = strlen(expected);
    char bytes[640];
    FILE *f;
    bool ok;
    if (len >= sizeof(bytes)) return false;
    f = fopen(path, "rb");
    if (f == NULL) return false;
    ok = fread(bytes, 1u, len + 1u, f) == len;
    if (fclose(f) != 0) ok = false;
    return ok && memcmp(bytes, expected, len) == 0;
}

static bool dev_sd_dir_exact(const char *path, const char *const *names,
                             size_t count) {
    bool seen[6] = {false};
    DIR *d = opendir(path);
    struct dirent *entry;
    bool valid = d != NULL && count <= 6u;
    if (!valid) {
        if (d != NULL) (void)closedir(d);
        return false;
    }
    errno = 0;
    while ((entry = readdir(d)) != NULL) {
        size_t i;
        if (strcmp(entry->d_name, ".") == 0 ||
            strcmp(entry->d_name, "..") == 0) {
            errno = 0;
            continue;
        }
        for (i = 0u; i < count; ++i) {
            if (strcmp(entry->d_name, names[i]) == 0) break;
        }
        if (i == count || seen[i]) { valid = false; break; }
        seen[i] = true;
        errno = 0;
    }
    if (errno != 0) valid = false;
    for (size_t i = 0u; i < count; ++i) {
        if (!seen[i]) valid = false;
    }
    if (closedir(d) != 0) valid = false;
    return valid;
}

bool dev_sdb11_media_readback_matches(const char *generation,
                                     const char *device_id,
                                     const char *binding,
                                     bool first_boot) {
    char marker[320], doc[640], manifest[640];
    const char *const root_names[] = { "M5DAYLOG" };
    const char *const daylog_names[] = {
        "device.json", "manifest.json", "dev-install.json",
        "recordings", "quarantine"
    };
    int n;
    if (!device_identity_is_valid_uuid(generation) ||
        !device_identity_is_valid_uuid(device_id) ||
        !device_manifest_is_valid_sha256_hex(binding) ||
        strlen(binding) != 64u ||
        !device_identity_build_device_json(device_id, NULL, NULL,
                                          doc, sizeof(doc))) return false;
    n = snprintf(marker, sizeof(marker),
         "{\"schemaVersion\":1,\"generation\":\"%s\","
         "\"deviceId\":\"%s\",\"baselineSha256\":\"%s\"}",
         generation, device_id, binding);
    if (n <= 0 || (size_t)n >= sizeof(marker)) return false;
    if (!dev_sd_file_exact(RECORDER_M5DAYLOG_DIR "/dev-install.json", marker) ||
        !dev_sd_file_exact(RECORDER_DEVICE_JSON_PATH, doc)) return false;
    if (!first_boot) return true;

    // On the initial supervised recording, the on-card baseline still must
    // be *exactly* the pristine install; no stale WAV or metadata fallback.
    if (!device_manifest_build_empty(device_id,
         "1970-01-01T00:00:00+00:00", manifest, sizeof(manifest))) return false;
    return dev_sd_dir_exact(RECORDER_SD_MOUNT_POINT, root_names, 1u) &&
           dev_sd_dir_exact(RECORDER_M5DAYLOG_DIR, daylog_names, 5u) &&
           dev_sd_dir_exact(RECORDER_RECORDINGS_DIR, NULL, 0u) &&
           dev_sd_dir_exact(RECORDER_QUARANTINE_DIR, NULL, 0u) &&
           dev_sd_file_exact(RECORDER_MANIFEST_PATH, manifest);
}


#else
bool dev_sdb11_media_readback_matches(const char *generation,
                                      const char *device_id,
                                      const char *binding,
                                      bool first_boot) {
    (void)generation; (void)device_id; (void)binding; (void)first_boot;
    return false;
}
#endif
