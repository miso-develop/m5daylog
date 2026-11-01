#include "dev_sdb11_first_boot.h"
#if defined(ESP_PLATFORM) && defined(CONFIG_M5DAYLOG_SDB11_DEV_INSTALL) && CONFIG_M5DAYLOG_SDB11_DEV_INSTALL

#include <stdint.h>
#include <string.h>
#include "nvs.h"
#include "esp_system.h"
#include "device_identity.h"
#include "recorder_nvs.h"

// All values are explicit and are NOT defaults for absent NVS keys.
enum {
    DEV_READY_UNSPENT = 0xa2u,
    DEV_INTENT_STAGED = 0xa3u,
    DEV_FIRST_BOOT_USED = 0xa4u,
    FENCE_UNQUALIFIED = 0x51u,
    FENCE_BOOT_ACTIVE = 0x52u,
    LIFECYCLE_NORMAL = 0u,
};
static bool s_attempted; // process-local; no retry after an uncertain write
static bool s_consumed;

static bool sha256_lower_hex(const char *s) {
    if (s == NULL || strlen(s) != 64u) return false;
    for (size_t i = 0u; i < 64u; ++i) {
        if (!((s[i] >= '0' && s[i] <= '9') ||
              (s[i] >= 'a' && s[i] <= 'f'))) return false;
    }
    return true;
}

bool dev_sdb11_boot_intent_consumed_this_boot(void) {
    return s_consumed;
}

esp_err_t dev_sdb11_consume_boot_intent(const char *expected_generation,
                                       const char *expected_device_id,
                                       const char *expected_binding_sha256,
                                       bool physical_hold_proven) {
    nvs_handle_t h = 0;
    char generation[RECORDER_UUID_STR_LEN] = {0};
    char device_id[RECORDER_UUID_STR_LEN] = {0};
    char binding[65] = {0};
    size_t generation_len = sizeof(generation);
    size_t id_len = sizeof(device_id);
    size_t binding_len = sizeof(binding);
    uint8_t state = 0, lifecycle = 0xffu, fence = 0;
    uint64_t nonce = 0u, read_nonce = 0u;
    esp_err_t err;

    if (!physical_hold_proven ||
        !device_identity_is_valid_uuid(expected_generation) ||
        !device_identity_is_valid_uuid(expected_device_id) ||
        !sha256_lower_hex(expected_binding_sha256) ||
        s_attempted) return ESP_ERR_INVALID_STATE;
    // This latch is set even if a later read or write fails. A reboot must
    // NOT reconstruct it from STAGED, USED, BOOT_ACTIVE, UUID or nonce.
    s_attempted = true;
    s_consumed = false;
    err = recorder_nvs_lock();
    if (err != ESP_OK) return err;
    err = nvs_open("m5daylog", NVS_READWRITE, &h);
    if (err != ESP_OK) goto done;
    err = nvs_get_str(h, "dev_gen", generation, &generation_len);
    if (err != ESP_OK) goto done;
    err = nvs_get_str(h, "device_id", device_id, &id_len);
    if (err != ESP_OK) goto done;
    err = nvs_get_str(h, "dev_bind", binding, &binding_len);
    if (err != ESP_OK) goto done;
    err = nvs_get_u8(h, "dev_state", &state);
    if (err != ESP_OK) goto done;
    err = nvs_get_u8(h, "release_boot", &fence);
    if (err != ESP_OK) goto done;
    err = nvs_get_u8(h, "shutdown_armed", &lifecycle);
    if (err != ESP_OK) goto done;
    // Any surviving nonce may represent a partially committed prior attempt.
    err = nvs_get_u64(h, "dev_nonce", &read_nonce);
    if (err != ESP_ERR_NVS_NOT_FOUND) {
        if (err == ESP_OK) err = ESP_ERR_INVALID_STATE;
        goto done;
    }
    if (generation_len != RECORDER_UUID_STR_LEN ||
        id_len != RECORDER_UUID_STR_LEN || binding_len != 65u ||
        strcmp(generation, expected_generation) != 0 ||
        strcmp(device_id, expected_device_id) != 0 ||
        strcmp(binding, expected_binding_sha256) != 0 ||
        state != DEV_READY_UNSPENT || fence != FENCE_UNQUALIFIED ||
        lifecycle != LIFECYCLE_NORMAL) {
        err = ESP_ERR_INVALID_STATE;
        goto done;
    }

    // Two one-way persistent commits. Neither multi-key commit nor an
    // ESP_ERR_* result provides rollback/atomicity. External *already spent*
    // owner intent must prohibit an operator retry after an uncertain result.
    err = nvs_set_u8(h, "dev_state", DEV_INTENT_STAGED);
    if (err == ESP_OK) err = nvs_commit(h);
    if (err != ESP_OK) goto done;
    err = nvs_get_u8(h, "dev_state", &state);
    if (err != ESP_OK) goto done;
    if (state != DEV_INTENT_STAGED) {
        err = ESP_ERR_INVALID_STATE;
        goto done;
    }
    nonce = ((uint64_t)esp_random() << 32) | esp_random();
    if (nonce == 0u) {
        err = ESP_ERR_INVALID_STATE;
        goto done;
    }
    err = nvs_set_u64(h, "dev_nonce", nonce);
    if (err == ESP_OK) err = nvs_set_u8(h, "dev_state", DEV_FIRST_BOOT_USED);
    if (err == ESP_OK) err = nvs_set_u8(h, "release_boot", FENCE_BOOT_ACTIVE);
    if (err == ESP_OK) err = nvs_commit(h);
    if (err != ESP_OK) goto done;
    state = 0; fence = 0;
    err = nvs_get_u8(h, "dev_state", &state);
    if (err == ESP_OK) err = nvs_get_u8(h, "release_boot", &fence);
    if (err == ESP_OK) err = nvs_get_u64(h, "dev_nonce", &read_nonce);
    if (err == ESP_OK && (state != DEV_FIRST_BOOT_USED ||
                          fence != FENCE_BOOT_ACTIVE ||
                          read_nonce != nonce)) {
        err = ESP_ERR_INVALID_STATE;
    }
    if (err == ESP_OK) s_consumed = true;

done:
    if (h != 0) nvs_close(h);
    recorder_nvs_unlock();
    return err;
}

#else
bool dev_sdb11_boot_intent_consumed_this_boot(void) { return false; }
esp_err_t dev_sdb11_consume_boot_intent(const char *generation,
                                       const char *device_id,
                                       const char *binding,
                                       bool physical_hold_proven) {
    (void)generation; (void)device_id; (void)binding; (void)physical_hold_proven;
    return ESP_ERR_NOT_SUPPORTED;
}
#endif
