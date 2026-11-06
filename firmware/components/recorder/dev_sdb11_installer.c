#include "dev_sdb11_installer.h"
#if defined(ESP_PLATFORM) && defined(CONFIG_M5DAYLOG_SDB11_DEV_INSTALL) && CONFIG_M5DAYLOG_SDB11_DEV_INSTALL && defined(CONFIG_M5DAYLOG_SDB11_INSTALLER_MODE) && CONFIG_M5DAYLOG_SDB11_INSTALLER_MODE

#include <errno.h>
#include <dirent.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "device_identity.h"
#include "device_manifest.h"
#include "dev_fresh_install_provision.h"
#include "dev_sdb11_first_boot.h"
#include "recorder_config.h"
#include "sd_mount.h"

// The installer is a separate, explicitly pinned image; normal app_main and
// CDC never call this API. Every write is DEV-only and only to the card whose
// positive CID/capacity match was checked before format and after remount.
// CID+capacity alone are not authenticated proof of exact physical target.
#define DEV_MARKER_PATH RECORDER_M5DAYLOG_DIR "/dev-install.json"
#define DEV_EMPTY_TIME "1970-01-01T00:00:00+00:00"
// Sentinel for an UNQUALIFIED RTC, never evidence that time was observed.
static bool s_install_attempted;

static bool dev_dir_has_exact(const char *path, const char *const *wanted,
                               size_t count) {
    DIR *d = opendir(path);
    struct dirent *ent;
    bool seen[6] = {false};
    bool ok = d != NULL && count <= 6u;
    if (!ok) {
        if (d != NULL) (void)closedir(d);
        return false;
    }
    errno = 0;
    while ((ent = readdir(d)) != NULL) {
        if (strcmp(ent->d_name, ".") == 0 ||
            strcmp(ent->d_name, "..") == 0) {
            errno = 0;
            continue;
        }
        size_t i;
        for (i = 0u; i < count; ++i) {
            if (strcmp(ent->d_name, wanted[i]) == 0) break;
        }
        if (i == count || seen[i]) {
            ok = false;
            break;
        }
        seen[i] = true;
        errno = 0;
    }
    if (errno != 0) ok = false;
    for (size_t i = 0u; i < count; ++i) if (!seen[i]) ok = false;
    if (closedir(d) != 0) ok = false;
    return ok;
}

static bool dev_write_new_exact(const char *path, const char *doc) {
    const size_t len = strlen(doc);
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0) return false;
    ssize_t count = write(fd, doc, len);
    bool ok = count == (ssize_t)len && fsync(fd) == 0;
    if (close(fd) != 0) ok = false;
    // Do not remove partially persisted data on failure; it remains a
    // visible failed one-shot installation attempt requiring owner action.
    return ok;
}

static bool dev_readback_exact(const char *path, const char *expected) {
    char bytes[640];
    const size_t size = strlen(expected);
    FILE *f = fopen(path, "rb");
    bool ok;
    if (f == NULL || size >= sizeof(bytes)) {
        if (f != NULL) (void)fclose(f);
        return false;
    }
    ok = fread(bytes, 1, size + 1u, f) == size;
    if (fclose(f) != 0) ok = false;
    return ok && memcmp(bytes, expected, size) == 0;
}

static bool dev_media_readback(const char *device_json,
                                const char *empty_manifest,
                                const char *generation_doc) {
    static const char *const dirs[] = { "M5DAYLOG" };
    static const char *const files[] = {
        "device.json", "manifest.json", "dev-install.json",
        "recordings", "quarantine"
    };
    // No old WAV/part/manifest.tmp/backup, or unexpected extra bytes.
    return dev_dir_has_exact(RECORDER_SD_MOUNT_POINT, dirs, 1u) &&
           dev_dir_has_exact(RECORDER_M5DAYLOG_DIR, files, 5u) &&
           dev_dir_has_exact(RECORDER_RECORDINGS_DIR, NULL, 0u) &&
           dev_dir_has_exact(RECORDER_QUARANTINE_DIR, NULL, 0u) &&
           dev_readback_exact(RECORDER_DEVICE_JSON_PATH, device_json) &&
           dev_readback_exact(RECORDER_MANIFEST_PATH, empty_manifest) &&
           dev_readback_exact(DEV_MARKER_PATH, generation_doc);
}

esp_err_t dev_sdb11_install_baseline(const char *generation,
                                     const char *device_id,
                                     const char *baseline_sha256,
                                     uint32_t expected_cid_serial,
                                     uint64_t expected_sector_count,
                                     bool owner_grant_consumed) {
    char device_json[640];
    char manifest[640];
    char marker[320];
    bool mounted = false;
    esp_err_t err;

    if (s_install_attempted || !owner_grant_consumed ||
        !device_identity_is_valid_uuid(generation) ||
        !device_identity_is_valid_uuid(device_id) ||
        !device_manifest_is_valid_sha256_hex(baseline_sha256) ||
        expected_sector_count == 0u ||
        sd_mount_is_mounted()) return ESP_ERR_INVALID_STATE;
    // Explicit install image and external owner grant preconditions are
    // required. Do not infer them from a blank NVS/SD or a random UUID.
    s_install_attempted = true;  // no retry on an ambiguous operation

    if (!device_identity_build_device_json(device_id, NULL, NULL,
                                           device_json, sizeof(device_json)) ||
        !device_manifest_build_empty(device_id, DEV_EMPTY_TIME,
                                     manifest, sizeof(manifest))) {
        return ESP_ERR_INVALID_ARG;
    }
    const int m = snprintf(marker, sizeof(marker),
            "{\"schemaVersion\":1,\"generation\":\"%s\","
            "\"deviceId\":\"%s\",\"baselineSha256\":\"%s\"}",
            generation, device_id, baseline_sha256);
    if (m <= 0 || (size_t)m >= sizeof(marker)) return ESP_ERR_INVALID_ARG;

    // Fresh NVS is separately partition-scoped by the approved operator.
    // Any per-key partial write (or ambiguous commit) burns the attempt.
    err = dev_fresh_install_nvs_provision_pristine(device_id);
    if (err != ESP_OK) return err;
    err = sd_mount_dev_rebaseline_selected(true, expected_cid_serial,
                                           expected_sector_count);
    if (err != ESP_OK) return err;

    // Still no TinyUSB driver/CDC session. The APP storage backend can exist
    // only in this installer, with no host-published MSC endpoint.
    err = sd_mount_recordings();
    if (err != ESP_OK) return err;
    mounted = true;
    if (!sd_mount_dev_card_matches(expected_cid_serial,
                                    expected_sector_count)) {
        err = ESP_ERR_INVALID_STATE;
        goto done;
    }
    if (!dev_write_new_exact(RECORDER_DEVICE_JSON_PATH, device_json) ||
        !dev_write_new_exact(RECORDER_MANIFEST_PATH, manifest) ||
        !dev_write_new_exact(DEV_MARKER_PATH, marker)) {
        err = ESP_FAIL;
        goto done;
    }
    if (!dev_media_readback(device_json, manifest, marker)) {
        err = ESP_ERR_INVALID_STATE;
        goto done;
    }

    // Independently remount and re-check exact bytes. This helper never
    // accepts success merely because a write() or FAT format returned OK.
    sd_mount_unmount();
    mounted = sd_mount_is_mounted();
    if (mounted) return ESP_ERR_INVALID_STATE;
    err = sd_mount_recordings();
    if (err != ESP_OK) return err;
    mounted = true;
    if (!sd_mount_dev_card_matches(expected_cid_serial,
                                    expected_sector_count) ||
        !dev_media_readback(device_json, manifest, marker)) {
        err = ESP_ERR_INVALID_STATE;
        goto done;
    }
    err = ESP_OK;
done:
    if (mounted) {
        sd_mount_unmount();
        if (sd_mount_is_mounted() && err == ESP_OK) err = ESP_ERR_INVALID_STATE;
    }
    if (err != ESP_OK) return err;
    // RTC remains unqualified. Only after BOTH independent card and NVS
    // readbacks may persistent readiness change; readiness still grants
    // NO release and needs a later separate real physical first-boot hold.
    return dev_sdb11_mark_baseline_ready(generation, device_id,
                                         baseline_sha256, true);
}

#else
esp_err_t dev_sdb11_install_baseline(const char *generation,
                                     const char *device_id,
                                     const char *baseline_sha256,
                                     uint32_t expected_cid_serial,
                                     uint64_t expected_sector_count,
                                     bool owner_grant_consumed) {
    (void)generation; (void)device_id; (void)baseline_sha256;
    (void)expected_cid_serial; (void)expected_sector_count;
    (void)owner_grant_consumed;
    return ESP_ERR_NOT_SUPPORTED;
}
#endif
