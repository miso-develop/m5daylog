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

#define USB_MSC_RELEASE_ATTEMPT_MAX_BYTES 64u

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

// Valid only after a completed provisional MSC command proves that the host has
// finished SET_CONFIGURATION and bound the MSC class. Performs the logical
// disconnect from the recorder coordinator, never from a TinyUSB callback.
esp_err_t usb_msc_ownership_begin_prepare(void);

esp_err_t usb_msc_ownership_note_prepare_complete(bool wav_finalized,
                                                   bool manifest_committed,
                                                   bool device_fs_released);

typedef enum {
    USB_MSC_RELEASE_STORAGE_ACCEPTED = 0,
    USB_MSC_RELEASE_STORAGE_INVALID_ARGS,
    USB_MSC_RELEASE_STORAGE_WRONG_STATE,
    USB_MSC_RELEASE_STORAGE_CONFLICT,
} usb_msc_release_storage_result_t;

// D-031 normal Windows release authority. Acceptance atomically closes MSC
// backend admission before returning but does not signal teardown until the CDC
// transport confirms the accepted response has completed.
usb_msc_release_storage_result_t usb_msc_ownership_accept_release_storage(
    const char *release_attempt_id);
bool usb_msc_ownership_release_response_complete(
    const char *release_attempt_id);

// Ordinary CDC requests are admitted only in the current PC-owned session
// before release acceptance. Ambiguous bus/line-state signals never open it.
bool usb_msc_ownership_release_command_admission_open(void);

// Valid only after exact START STOP UNIT(load_eject=1,start=0) has completed
// its SCSI status transaction and emitted RELEASE_REQUESTED. Stops TinyUSB
// first, then destroys the USB storage object; the latter is the
// esp_tinyusb deferred-write-zero proof. It never builds APP storage.
esp_err_t usb_msc_ownership_complete_release_quiesce(void);

// Human-Gate diagnostic only: persist the latest non-authoritative SCSI
// observation counters. Failure to persist diagnostics never grants release.
esp_err_t usb_msc_ownership_flush_scsi_trace(void);

bool usb_msc_ownership_is_host_owned(void);

#ifdef __cplusplus
}
#endif
