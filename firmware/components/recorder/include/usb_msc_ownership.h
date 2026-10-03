#pragma once

// Tasks #49/#87: fail-closed USB MSC ownership handoff.
//
// Initial APP -> USB publication still requires WAV finalize + manifest commit
// + Device filesystem release. Task #87 adds a second barrier in the reverse
// direction: ambiguous suspend/bus-loss may only request explicit USB teardown;
// Device remount is allowed only after teardown and deferred-write proof.

#include <stdbool.h>
#include <stdint.h>

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

typedef enum {
    USB_MSC_EVENT_NONE = 0,
    USB_MSC_EVENT_ATTACH,
    USB_MSC_EVENT_HOST_OWNED,
    USB_MSC_EVENT_BARRIER_REQUIRED,
    USB_MSC_EVENT_DETACH,
    USB_MSC_EVENT_FAILED,
} usb_msc_ownership_event_t;

// Bind the ownership callbacks to the MSC storage created by sd_mount. The USB
// device stack is intentionally started separately so boot can reach RECORDING
// before an already-connected host can request ownership.
esp_err_t usb_msc_ownership_init(void);
esp_err_t usb_msc_ownership_start(void);

// Wait for one ownership event. timeout_ms == UINT32_MAX waits indefinitely.
usb_msc_ownership_event_t usb_msc_ownership_wait_event(uint32_t timeout_ms);

// Complete the fail-closed pre-publish gate. All three proofs are mandatory;
// false never transfers storage from APP to USB.
esp_err_t usb_msc_ownership_note_prepare_complete(bool wav_finalized,
                                                   bool manifest_committed,
                                                   bool device_fs_released);

// Task #87 reverse barrier. May be called only after BARRIER_REQUIRED while USB
// owns the storage. It tears down the TinyUSB device stack first, then asks the
// SD layer to prove no deferred host write remains and rebuild APP ownership.
// Any failure leaves Device filesystem access disabled.
esp_err_t usb_msc_ownership_complete_disconnect_barrier(void);

// Start a fresh USB publication session only after Device remount and recording
// recovery have completed. Stale event bits/proofs do not cross sessions.
esp_err_t usb_msc_ownership_rearm(void);

bool usb_msc_ownership_is_host_owned(void);

#ifdef __cplusplus
}
#endif
