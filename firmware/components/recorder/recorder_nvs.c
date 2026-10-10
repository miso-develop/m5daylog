// Shared default-NVS lifetime/serialization for recorder subsystems.
//
// Task #50 REV-83-12: NVS init/deinit is not safe to race with active NVS
// operations. Keep the default partition initialized for the process lifetime
// and serialize recorder-owned transactions through one mutex.

#include "recorder_nvs.h"

#ifdef ESP_PLATFORM

#include <stdbool.h>
#include <stddef.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs_flash.h"

static SemaphoreHandle_t s_nvs_lock = NULL;
static bool s_nvs_initialized = false;

esp_err_t recorder_nvs_init(void) {
    esp_err_t err = ESP_OK;

    // app_main performs the first initialization before recorder worker tasks
    // are created. The null check remains defensive for unit-level callers.
    if (s_nvs_lock == NULL) {
        s_nvs_lock = xSemaphoreCreateMutex();
        if (s_nvs_lock == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    if (xSemaphoreTake(s_nvs_lock, portMAX_DELAY) != pdTRUE) {
        return ESP_FAIL;
    }
    if (!s_nvs_initialized) {
        err = nvs_flash_init();
        if (err == ESP_OK) {
            s_nvs_initialized = true;
        }
    }
    xSemaphoreGive(s_nvs_lock);
    return err;
}

esp_err_t recorder_nvs_lock(void) {
    esp_err_t err = recorder_nvs_init();

    if (err != ESP_OK) {
        return err;
    }
    if (xSemaphoreTake(s_nvs_lock, portMAX_DELAY) != pdTRUE) {
        return ESP_FAIL;
    }
    if (!s_nvs_initialized) {
        xSemaphoreGive(s_nvs_lock);
        return ESP_ERR_INVALID_STATE;
    }
    return ESP_OK;
}

void recorder_nvs_unlock(void) {
    if (s_nvs_lock != NULL) {
        (void)xSemaphoreGive(s_nvs_lock);
    }
}

#endif
