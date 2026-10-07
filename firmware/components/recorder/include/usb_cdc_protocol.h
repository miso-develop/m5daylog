#pragma once

// D-031 / Task #87: minimal canonical USB CDC protocol v1 foundation.
//
// Requests are UTF-8 line-delimited JSON (LF; CRLF accepted on RX), maximum
// 1024 payload bytes. Task #87 owns RELEASE_STORAGE lifecycle authority. Task
// #50 will rebase onto and extend this same parser/dispatcher with the remaining
// canonical v1 commands; no second CDC protocol stack is permitted.

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
#define USB_CDC_ERROR_BUSY "BUSY"
#define USB_CDC_ERROR_INTERNAL_ERROR "INTERNAL_ERROR"

typedef enum {
    USB_CDC_RELEASE_ACCEPTED = 0,
    USB_CDC_RELEASE_INVALID_ARGS,
    USB_CDC_RELEASE_WRONG_STATE,
    USB_CDC_RELEASE_CONFLICT,
    USB_CDC_RELEASE_INTERNAL_ERROR,
} usb_cdc_release_result_t;

typedef usb_cdc_release_result_t (*usb_cdc_release_accept_fn_t)(
    const char *release_attempt_id,
    void *ctx);
typedef bool (*usb_cdc_release_response_complete_fn_t)(
    const char *release_attempt_id,
    void *ctx);
typedef bool (*usb_cdc_command_admission_open_fn_t)(void *ctx);

typedef struct {
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
