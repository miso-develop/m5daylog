#include "shutdown_armed.h"

#ifdef ESP_PLATFORM

#include <stddef.h>
#include <stdint.h>

#include "nvs.h"
#include "nvs_flash.h"

static const char *const k_namespace = "m5daylog";
static const char *const k_key = "shutdown_armed";

typedef enum {
    SHUTDOWN_LIFECYCLE_NORMAL = 0,
    SHUTDOWN_LIFECYCLE_ARMED = 1,
    SHUTDOWN_LIFECYCLE_HOST_UNRESOLVED = 2,
} shutdown_lifecycle_state_t;

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

static esp_err_t shutdown_armed_read_state(shutdown_lifecycle_state_t *state) {
    nvs_handle_t handle;
    uint8_t value = SHUTDOWN_LIFECYCLE_NORMAL;
    esp_err_t err;

    if (state == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *state = SHUTDOWN_LIFECYCLE_NORMAL;
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
    if (value > SHUTDOWN_LIFECYCLE_HOST_UNRESOLVED) {
        return ESP_FAIL;
    }
    *state = (shutdown_lifecycle_state_t)value;
    return ESP_OK;
}

esp_err_t shutdown_armed_read(bool *armed) {
    shutdown_lifecycle_state_t state;
    esp_err_t err;

    if (armed == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *armed = false;
    err = shutdown_armed_read_state(&state);
    if (err != ESP_OK) {
        return err;
    }
    if (state == SHUTDOWN_LIFECYCLE_HOST_UNRESOLVED) {
        return ESP_ERR_INVALID_STATE;
    }
    *armed = state == SHUTDOWN_LIFECYCLE_ARMED;
    return ESP_OK;
}

esp_err_t shutdown_armed_mark_host_unresolved(void) {
    return shutdown_armed_write(SHUTDOWN_LIFECYCLE_HOST_UNRESOLVED);
}

esp_err_t shutdown_armed_commit(void) {
    return shutdown_armed_write(SHUTDOWN_LIFECYCLE_ARMED);
}

esp_err_t shutdown_armed_clear(void) {
    shutdown_lifecycle_state_t state;
    esp_err_t err = shutdown_armed_read_state(&state);
    if (err != ESP_OK) {
        return err;
    }
    if (state != SHUTDOWN_LIFECYCLE_ARMED) {
        return ESP_ERR_INVALID_STATE;
    }
    return shutdown_armed_write(SHUTDOWN_LIFECYCLE_NORMAL);
}

esp_err_t shutdown_armed_boot_action(bool manual_wake,
                                     shutdown_armed_boot_action_t *action) {
    shutdown_lifecycle_state_t state;
    esp_err_t err;

    if (action == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *action = SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN;
    err = shutdown_armed_read_state(&state);
    if (err != ESP_OK) {
        return err;
    }
    if (state == SHUTDOWN_LIFECYCLE_NORMAL) {
        *action = SHUTDOWN_ARMED_BOOT_NORMAL;
    } else if (state == SHUTDOWN_LIFECYCLE_ARMED && manual_wake) {
        *action = SHUTDOWN_ARMED_BOOT_MANUAL_RESUME;
    }
    return ESP_OK;
}

#else

esp_err_t shutdown_armed_read(bool *armed) {
    (void)armed;
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t shutdown_armed_mark_host_unresolved(void) {
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
