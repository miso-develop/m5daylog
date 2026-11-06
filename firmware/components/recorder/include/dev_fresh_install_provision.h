#pragma once
// #147 FI2-I1: supervised development-install NVS baseline preparation ONLY.
// The caller must independently verify an explicit one-generation install
// receipt, target/image/partition write-set, H0 and selected C/R/A media.
// FI3.1 first-write ordering (external physical-containment alternative):
// the selected SD is electrically excluded from ALL potential APP/host writers
// throughout preparation, including unexpected old/wrong APP boots or absent
// lifecycle NVS (whose legacy default is NORMAL). An independently recorded
// NEW_GENERATION_RECORDING_AUTHORIZED grant MUST already be durably consumed
// before this helper can write the final NORMAL NVS baseline. This helper
// cannot validate or consume that external grant; NVS readback is not a permit.
// This does NOT erase NVS, prove a partition is pristine, access SD/RTC
// hardware, attest the selected running APP, or authorize first release.
// Never call from ordinary app boot.
#include "device_identity.h"

#ifdef __cplusplus
extern "C" {
#endif

// Request persistence of three fresh-session keys using a single NVS commit
// only when BOTH recorder namespaces are absent. ESP-IDF does not guarantee
// multi-key transaction atomicity: any set/commit/readback failure is an
// indeterminate, externally SPENT attempt. Never retry or repair automatically.
// A present namespace, including one from a partial attempt, blocks re-entry.
// Does not inspect unrelated NVS namespaces: independent partition inventory
// and custody evidence is required before this operation is authorized.
// On success, separately reopens and checks the committed keys and RTC marker.
// A commit/readback error must burn the external attempt; NEVER retry blindly.
esp_err_t dev_fresh_install_nvs_provision_pristine(const char *expected_device_id);

#ifdef __cplusplus
}
#endif
