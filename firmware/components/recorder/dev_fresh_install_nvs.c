#include "dev_fresh_install_nvs.h"
#ifdef ESP_PLATFORM
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "nvs.h"
#include "recorder_nvs.h"

// Existing values in release_cycle_fence.c and shutdown_armed.c, not new
// enrollment markers. Verify the precise external candidate; never infer it.
#define DEV_FI_UNQUALIFIED 0x51u
#define DEV_FI_NORMAL 0u

esp_err_t dev_fresh_install_nvs_readback_matches(
    const char *expected_device_id) {
    nvs_handle_t handle = 0;
    char actual[RECORDER_UUID_STR_LEN] = {0};
    size_t actual_len = sizeof(actual);
    uint8_t fence = 0;
    uint8_t lifecycle = 0xffu;
    size_t pending_len = 0;
    esp_err_t err;
    if (!device_identity_is_valid_uuid(expected_device_id)) {
        return ESP_ERR_INVALID_ARG;
    }
    err = recorder_nvs_lock();
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_open("m5daylog", NVS_READONLY, &handle);
    if (err != ESP_OK) goto done;
    err = nvs_get_str(handle, "device_id", actual, &actual_len);
    if (err != ESP_OK) goto done;
    if (actual_len != RECORDER_UUID_STR_LEN ||
        !device_identity_is_valid_uuid(actual) ||
        strcmp(actual, expected_device_id) != 0) {
        err = ESP_ERR_INVALID_STATE;
        goto done;
    }
    err = nvs_get_u8(handle, "release_boot", &fence);
    if (err != ESP_OK) goto done;
    err = nvs_get_u8(handle, "shutdown_armed", &lifecycle);
    if (err != ESP_OK) goto done;
    if (fence != DEV_FI_UNQUALIFIED || lifecycle != DEV_FI_NORMAL) {
        err = ESP_ERR_INVALID_STATE;
        goto done;
    }
    nvs_close(handle);
    handle = 0;

    // RTC is unqualified only when there is no pending correction record.
    // This does not assert that the external RTC itself has been verified.
    err = nvs_open("m5daylog_rtc", NVS_READONLY, &handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        err = ESP_OK;
        goto done;
    }
    if (err != ESP_OK) goto done;
    err = nvs_get_blob(handle, "pending", NULL, &pending_len);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        err = ESP_OK;
    } else if (err == ESP_OK) {
        // Existing zero-length record also fails closed.
        err = ESP_ERR_INVALID_STATE;
    }
    // Other errors, including present-blob length query, fail closed.
done:
    if (handle != 0) nvs_close(handle);
    recorder_nvs_unlock();
    return err;
}
#else
esp_err_t dev_fresh_install_nvs_readback_matches(
    const char *expected_device_id) {
    (void)expected_device_id;
    return ESP_ERR_NOT_SUPPORTED;
}
#endif
