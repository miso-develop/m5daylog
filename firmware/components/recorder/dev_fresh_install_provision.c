#include "dev_fresh_install_provision.h"

#ifdef ESP_PLATFORM
#include <stdint.h>
#include "nvs.h"
#include "recorder_nvs.h"
#include "dev_fresh_install_nvs.h"

// These are the existing #143 fence/lifecycle values, not new privileges.
#define DEV_FI_UNQUALIFIED 0x51u
#define DEV_FI_NORMAL 0u

esp_err_t dev_fresh_install_nvs_provision_pristine(
    const char *expected_device_id) {
    nvs_handle_t handle = 0;
    esp_err_t err;

    if (!device_identity_is_valid_uuid(expected_device_id)) {
        return ESP_ERR_INVALID_ARG;
    }
    err = recorder_nvs_lock();
    if (err != ESP_OK) return err;

    // External authorization must already have proven the exact partition
    // baseline. This is an additional fail-closed guard, not erase proof:
    // even an empty existing namespace can indicate a spent/partial attempt.
    err = nvs_open("m5daylog", NVS_READONLY, &handle);
    if (err == ESP_OK) {
        nvs_close(handle);
        handle = 0;
        err = ESP_ERR_INVALID_STATE;
        goto done;
    }
    if (err != ESP_ERR_NVS_NOT_FOUND) goto done;

    err = nvs_open("m5daylog_rtc", NVS_READONLY, &handle);
    if (err == ESP_OK) {
        nvs_close(handle);
        handle = 0;
        err = ESP_ERR_INVALID_STATE;
        goto done;
    }
    if (err != ESP_ERR_NVS_NOT_FOUND) goto done;

    // Request persistence of the identity, unqualified release fence, and
    // NORMAL lifecycle through one NVS commit. NVS does not guarantee that
    // these three keys change atomically, including across power failure.
    // A partial or uncertain write is a spent external installation attempt;
    // neither this function nor its future caller may retry or self-repair.
    err = nvs_open("m5daylog", NVS_READWRITE, &handle);
    if (err != ESP_OK) goto done;
    err = nvs_set_str(handle, "device_id", expected_device_id);
    if (err == ESP_OK) {
        err = nvs_set_u8(handle, "release_boot", DEV_FI_UNQUALIFIED);
    }
    if (err == ESP_OK) {
        err = nvs_set_u8(handle, "shutdown_armed", DEV_FI_NORMAL);
    }
    if (err == ESP_OK) err = nvs_commit(handle);

done:
    if (handle != 0) nvs_close(handle);
    recorder_nvs_unlock();
    if (err != ESP_OK) return err;

    // This readback is an observation, never an install receipt or a release
    // permit. No ordinary boot path invokes this provisioning function.
    return dev_fresh_install_nvs_readback_matches(expected_device_id);
}
#else
esp_err_t dev_fresh_install_nvs_provision_pristine(
    const char *expected_device_id) {
    (void)expected_device_id;
    return ESP_ERR_NOT_SUPPORTED;
}
#endif
