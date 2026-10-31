#pragma once
// #147 FI2-I1: supervised development-install NVS baseline preparation ONLY.
// The caller must independently verify an explicit one-generation install
// receipt, target/image/partition write-set, H0 and selected C/R/A media.
// This does NOT erase NVS, prove the partition was erased, access SD/RTC
// hardware or authorize first release. Never call from ordinary app boot.
#include "device_identity.h"

#ifdef __cplusplus
extern "C" {
#endif

// Atomically stage the exact three fresh-session keys only when BOTH recorder
// namespaces are completely absent. A present namespace, including one left
// by a partial/uncertain earlier attempt, is a hard failure; never repair it.
// Does not inspect unrelated NVS namespaces: independent partition inventory
// and custody evidence is required before this operation is authorized.
// On success, separately reopens and checks the committed keys and RTC marker.
// A commit/readback error must burn the external attempt; NEVER retry blindly.
esp_err_t dev_fresh_install_nvs_provision_pristine(const char *expected_device_id);

#ifdef __cplusplus
}
#endif
