#pragma once

// Task #49: fail-closed USB MSC ownership handoff.
//
// The TinyUSB storage callback reports an ATTACH before the SD card is
// transferred from the application to the USB host. The callback blocks
// until the recorder confirms WAV finalize + manifest commit + Device-side
// filesystem release. A normal detach is reported only after TinyUSB has
// mounted the filesystem back to the application.

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
    USB_MSC_EVENT_DETACH,
    USB_MSC_EVENT_FAILED,
} usb_msc_ownership_event_t;

// Bind the Task #49 callback to the MSC storage created by sd_mount.
// The USB device stack is intentionally started separately so boot can reach
// RECORDING before an already-connected host can request ownership.
esp_err_t usb_msc_ownership_init(void);
esp_err_t usb_msc_ownership_start(void);

// Wait for one ownership event. timeout_ms == UINT32_MAX waits indefinitely.
usb_msc_ownership_event_t usb_msc_ownership_wait_event(uint32_t timeout_ms);

// Complete the fail-closed pre-publish gate. All three proofs are mandatory;
// false never releases the blocked TinyUSB mount callback.
esp_err_t usb_msc_ownership_note_prepare_complete(bool wav_finalized,
                                                   bool manifest_committed,
                                                   bool device_fs_released);

bool usb_msc_ownership_is_host_owned(void);

#ifdef __cplusplus
}
#endif
