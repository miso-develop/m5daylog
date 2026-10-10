#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Default NVS is a recorder process-lifetime service. Initialization is
// idempotent and the partition is intentionally never deinitialized while the
// firmware process is alive.
esp_err_t recorder_nvs_init(void);

// Serialize recorder-owned NVS transactions across lifecycle state, RTC
// correction pending state, and diagnostic persistence.
esp_err_t recorder_nvs_lock(void);
void recorder_nvs_unlock(void);

#ifdef __cplusplus
}
#endif
