#pragma once

// #144: extra release-cycle authorization, independent of recording UUID.
// This does not authorize a RELEASE_STORAGE command on its own.
#include <stdbool.h>
#ifdef ESP_PLATFORM
#include "esp_err.h"
#else
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL 1
#define ESP_ERR_INVALID_ARG 2
#define ESP_ERR_INVALID_STATE 3
#define ESP_ERR_NOT_SUPPORTED 5
#endif

#ifdef __cplusplus
extern "C" {
#endif

// Once per process boot, after physical WAKE sampling and lifecycle NVS init.
// Only a fresh durable SHUTDOWN_ARMED + previously ready (supervised) fence
// can consume one candidate permit. An error disables this boot's candidate,
// but the existing recorder/USB recovery policy remains independent.
esp_err_t release_cycle_fence_begin_boot(bool manual_wake);

// Called only after #87 manual-WAKE recovery has durably reached NORMAL,
// recorder reached RECORDING and RTC recovery checks succeeded. This does
// not authorize publication by itself.
esp_err_t release_cycle_fence_note_recorder_ready(void);

// RAM-only completed-boot origin witness. NOT sufficient for GET_STATUS: a separate finalized,
// durably indexed, current-boot nonzero-PCM manifest proof is also required.
bool release_cycle_fence_boot_qualified(void);

// RAM-only, current-boot admission origin. NULL unless the real durable
// permit was consumed and #87 recorder readiness reached. Not a new grant.
const char *release_cycle_fence_current_origin(void);

// R5-FU: source-only, explicitly prepared first-use staging path.
// Caller must establish 10 seconds of uninterrupted real GPIO42 hold in
// USB_SYNC/PC_OWNED, same-session custody and a separately authorized #119
// one-shot intent. This function does not accept missing NVS as enrollment.
// It durably stages intent, NOT release qualification or first boot authority.
esp_err_t release_cycle_fence_stage_first_use(void);
bool release_cycle_fence_first_use_staged(void);

// Called only after the existing canonical CDC RELEASE_STORAGE accept
// succeeds; never from SCSI eject, unplug or the button handler.
void release_cycle_fence_note_canonical_release_accepted(void);

// Callback-safe invalidation for a physical USB-session cutoff before the
// canonical acceptance; no persistent state mutation/re-enrollment.
void release_cycle_fence_note_physical_session_cutoff(void);

// Call once after PC-owned USB_SYNC and durable HOST_UNRESOLVED are proven.
// It cannot be invoked simply because a stale boot marker was found.
esp_err_t release_cycle_fence_note_host_owned(void);
// RAM-only proof of the current-boot durable HOST_UNRESOLVED observation.
bool release_cycle_fence_host_owned_qualified(void);

// Call only AFTER #87 has completed release quiescence, committed
// SHUTDOWN_ARMED, and proven the preceding host release was accepted.
// A failed write never grants the following boot a new candidate.
esp_err_t release_cycle_fence_note_release_completed(void);

// No erase, auto-enroll or NVS reset API exists. A missing/invalid key stays
// unqualified: supervised staging needs an already explicitly provisioned
// UNQUALIFIED marker, live GPIO custody, and host/human attempt evidence.
#ifdef __cplusplus
}
#endif
