#pragma once
// #147/#144 DEV_FRESH_INSTALL: non-authoritative post-provision NVS readback.
// Success verifies ONLY the observed NVS keys, not the independent install
// receipt, host H0, retained SD integrity, running APP, custody or release.
// Never provisions, enrolls, releases, repairs or authorizes a physical action.
#include "device_identity.h"
#ifdef __cplusplus
extern "C" {
#endif
esp_err_t dev_fresh_install_nvs_readback_matches(const char *expected_device_id);
#ifdef __cplusplus
}
#endif
