#pragma once

// Task #50: USB CDC line-delimited JSON protocol v1.
//
// Requests are UTF-8 JSON objects framed by LF (CRLF accepted on RX), with a
// maximum payload of 1024 bytes excluding the terminator. Processing is
// sequential and responses preserve request order. CDC transport callbacks
// report connectivity/RX only; command admission is opened explicitly by the
// recorder lifecycle after MSC ownership reaches USB_SYNC/CDC-ready.

#include <stdbool.h>
#include <stddef.h>

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

#define USB_CDC_PROTOCOL_MAJOR 1u
#define USB_CDC_PROTOCOL_MAX_LINE_BYTES 1024u
#define USB_CDC_PROTOCOL_MAX_ID_BYTES 64u

#define USB_CDC_ERROR_INVALID_JSON "INVALID_JSON"
#define USB_CDC_ERROR_INVALID_REQUEST "INVALID_REQUEST"
#define USB_CDC_ERROR_REQUEST_TOO_LARGE "REQUEST_TOO_LARGE"
#define USB_CDC_ERROR_UNKNOWN_COMMAND "UNKNOWN_COMMAND"
#define USB_CDC_ERROR_INVALID_ARGS "INVALID_ARGS"
#define USB_CDC_ERROR_RANGE_ERROR "RANGE_ERROR"
#define USB_CDC_ERROR_BUSY "BUSY"
#define USB_CDC_ERROR_INTERNAL_ERROR "INTERNAL_ERROR"

typedef struct {
    const char *state;
    const char *reason;
    int battery_mv;
    bool battery_valid;
    bool rtc_correction_pending;
} usb_cdc_protocol_status_t;

typedef bool (*usb_cdc_protocol_status_provider_t)(
    usb_cdc_protocol_status_t *out,
    void *ctx);

typedef struct {
    // Stable canonical deviceId owned by the existing identity subsystem.
    const char *device_id;
    usb_cdc_protocol_status_provider_t status_provider;
    void *status_ctx;
} usb_cdc_protocol_config_t;

// Bind immutable identity/status providers. TinyUSB driver installation is
// still owned by usb_msc_ownership_start() so MSC ownership semantics remain
// unchanged.
esp_err_t usb_cdc_protocol_init(const usb_cdc_protocol_config_t *config);

// Close command admission, invalidate old RX identity, wait for any command
// already executing to finish, and force stale RX to be drained before a later
// lifecycle-owned reopen. This is the teardown barrier required before
// remount/RTC-event flush/restart.
void usb_cdc_protocol_reset_session(void);

// Open command admission at the accepted CDC-ready lifecycle point. This is
// intentionally separate from RX/DTR transport callbacks; only the recorder
// ownership lifecycle may make commands executable.
void usb_cdc_protocol_open_session(void);

// Initialize CDC-ACM interface 0 and its dedicated sequential worker after
// the common TinyUSB driver has been installed by the MSC owner.
esp_err_t usb_cdc_protocol_start(void);

#ifdef __cplusplus
}
#endif
