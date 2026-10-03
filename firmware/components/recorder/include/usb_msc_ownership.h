#pragma once

// Tasks #49/#87: fail-closed USB MSC ownership handoff.
//
// APP -> USB publication requires finalized WAV + durable manifest + Device
// filesystem release. The reverse direction is not an ownership return:
// Strategy 2 accepts only an explicit MSC eject, quiesces host I/O, releases
// the USB-owned storage object, and leaves the Device filesystem unmounted for
// persistent shutdown.

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
    USB_MSC_EVENT_RELEASE_REQUESTED,
    USB_MSC_EVENT_RELEASE_QUIESCED,
    USB_MSC_EVENT_FAILED,
} usb_msc_ownership_event_t;

esp_err_t usb_msc_ownership_init(void);
esp_err_t usb_msc_ownership_start(void);
usb_msc_ownership_event_t usb_msc_ownership_wait_event(uint32_t timeout_ms);

esp_err_t usb_msc_ownership_note_prepare_complete(bool wav_finalized,
                                                   bool manifest_committed,
                                                   bool device_fs_released);

// Valid only after the explicit START STOP UNIT(load_eject=1,start=0) wrapper
// has logically disconnected the device and emitted RELEASE_REQUESTED. Stops
// TinyUSB first, then destroys the USB storage object; the latter is the
// esp_tinyusb deferred-write-zero proof. It never builds APP storage.
esp_err_t usb_msc_ownership_complete_release_quiesce(void);

bool usb_msc_ownership_is_host_owned(void);

#ifdef __cplusplus
}
#endif
