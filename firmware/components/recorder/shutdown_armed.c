#include "shutdown_armed.h"

#ifdef ESP_PLATFORM

#include <stddef.h>
#include <stdint.h>

#include "nvs.h"
#include "nvs_flash.h"

static const char *const k_namespace = "m5daylog";
static const char *const k_key = "shutdown_armed";

static esp_err_t shutdown_armed_write(uint8_t value) {
    nvs_handle_t handle;
    esp_err_t err = nvs_flash_init();
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_open(k_namespace, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_u8(handle, k_key, value);
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    return err;
}

esp_err_t shutdown_armed_read(bool *armed) {
    nvs_handle_t handle;
    uint8_t value = 0;
    esp_err_t err;

    if (armed == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *armed = false;
    err = nvs_flash_init();
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_open(k_namespace, NVS_READONLY, &handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_get_u8(handle, k_key, &value);
    nvs_close(handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    if (err != ESP_OK) {
        return err;
    }
    if (value > 1u) {
        return ESP_FAIL;
    }
    *armed = value == 1u;
    return ESP_OK;
}

esp_err_t shutdown_armed_commit(void) {
    return shutdown_armed_write(1u);
}

esp_err_t shutdown_armed_clear(void) {
    return shutdown_armed_write(0u);
}

esp_err_t shutdown_armed_boot_action(bool manual_wake,
                                     shutdown_armed_boot_action_t *action) {
    bool armed = false;
    esp_err_t err;

    if (action == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *action = SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN;
    err = shutdown_armed_read(&armed);
    if (err != ESP_OK) {
        return err;
    }
    if (!armed) {
        *action = SHUTDOWN_ARMED_BOOT_NORMAL;
    } else if (manual_wake) {
        *action = SHUTDOWN_ARMED_BOOT_MANUAL_RESUME;
    }
    return ESP_OK;
}

#else

esp_err_t shutdown_armed_read(bool *armed) {
    (void)armed;
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t shutdown_armed_commit(void) {
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t shutdown_armed_clear(void) {
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t shutdown_armed_boot_action(bool manual_wake,
                                     shutdown_armed_boot_action_t *action) {
    (void)manual_wake;
    (void)action;
    return ESP_ERR_NOT_SUPPORTED;
}

#endif
