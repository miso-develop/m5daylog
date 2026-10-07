#pragma once

// Canonical USB CDC JSON protocol v1.
//
// Task #87 owns RELEASE_STORAGE lifecycle authority and the transport/session
// gate. Task #50 extends that same parser/dispatcher with the four application
// commands and RTC mutation seam; no second CDC stack is permitted.

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
#define USB_CDC_PROTOCOL_TIME_BYTES 32u

#define USB_CDC_ERROR_INVALID_JSON "INVALID_JSON"
#define USB_CDC_ERROR_INVALID_REQUEST "INVALID_REQUEST"
#define USB_CDC_ERROR_REQUEST_TOO_LARGE "REQUEST_TOO_LARGE"
#define USB_CDC_ERROR_UNKNOWN_COMMAND "UNKNOWN_COMMAND"
#define USB_CDC_ERROR_INVALID_ARGS "INVALID_ARGS"
#define USB_CDC_ERROR_RANGE_ERROR "RANGE_ERROR"
#define USB_CDC_ERROR_BUSY "BUSY"
#define USB_CDC_ERROR_INTERNAL_ERROR "INTERNAL_ERROR"

typedef enum {
    USB_CDC_RELEASE_ACCEPTED = 0,
    USB_CDC_RELEASE_INVALID_ARGS,
    USB_CDC_RELEASE_WRONG_STATE,
    USB_CDC_RELEASE_CONFLICT,
    USB_CDC_RELEASE_INTERNAL_ERROR,
} usb_cdc_release_result_t;

typedef enum {
    USB_CDC_SET_TIME_OK = 0,
    USB_CDC_SET_TIME_INVALID_ARGS,
    USB_CDC_SET_TIME_RANGE_ERROR,
    USB_CDC_SET_TIME_BUSY,
    USB_CDC_SET_TIME_INTERNAL_ERROR,
} usb_cdc_set_time_result_t;

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
typedef usb_cdc_set_time_result_t (*usb_cdc_set_time_fn_t)(
    const char *requested_time,
    char *normalized,
    size_t normalized_size,
    void *ctx);

typedef usb_cdc_release_result_t (*usb_cdc_release_accept_fn_t)(
    const char *release_attempt_id,
    void *ctx);
typedef bool (*usb_cdc_release_response_complete_fn_t)(
    const char *release_attempt_id,
    void *ctx);
typedef bool (*usb_cdc_command_admission_open_fn_t)(void *ctx);

typedef struct {
    // Application command providers. device_id is the existing canonical
    // identity; SET_TIME implementation must not write microSD.
    const char *device_id;
    usb_cdc_protocol_status_provider_t status_provider;
    void *status_ctx;
    usb_cdc_set_time_fn_t set_time;
    void *set_time_ctx;

    // D-031 / Task #87 lifecycle callbacks. RELEASE_STORAGE acceptance closes
    // shared command/MSC admission before the success response is emitted.
    usb_cdc_release_accept_fn_t release_accept;
    usb_cdc_release_response_complete_fn_t release_response_complete;
    usb_cdc_command_admission_open_fn_t command_admission_open;
    void *lifecycle_ctx;
} usb_cdc_protocol_config_t;

esp_err_t usb_cdc_protocol_init(const usb_cdc_protocol_config_t *config);
void usb_cdc_protocol_reset_session(void);
void usb_cdc_protocol_open_session(void);
esp_err_t usb_cdc_protocol_start(void);

#ifdef __cplusplus
}
#endif
