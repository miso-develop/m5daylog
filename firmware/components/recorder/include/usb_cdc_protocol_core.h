#pragma once

// D-031 / Task #87 portable canonical CDC v1 framer/dispatcher.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "usb_cdc_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    USB_CDC_PROTOCOL_FRAME_NONE = 0,
    USB_CDC_PROTOCOL_FRAME_LINE,
    USB_CDC_PROTOCOL_FRAME_TOO_LARGE,
} usb_cdc_protocol_frame_result_t;

typedef struct {
    uint8_t line[USB_CDC_PROTOCOL_MAX_LINE_BYTES + 1u];
    size_t line_len;
    bool oversized;
} usb_cdc_protocol_framer_t;

typedef struct {
    bool release_accepted;
    char release_attempt_id[USB_CDC_PROTOCOL_MAX_ID_BYTES + 1u];
} usb_cdc_protocol_effect_t;

void usb_cdc_protocol_framer_init(usb_cdc_protocol_framer_t *framer);
void usb_cdc_protocol_framer_reset(usb_cdc_protocol_framer_t *framer);
usb_cdc_protocol_frame_result_t usb_cdc_protocol_framer_feed(
    usb_cdc_protocol_framer_t *framer,
    uint8_t byte,
    const uint8_t **out_line,
    size_t *out_len);

int usb_cdc_protocol_process_line(
    const uint8_t *line,
    size_t len,
    char *out,
    size_t out_size,
    const usb_cdc_protocol_config_t *config,
    usb_cdc_protocol_effect_t *effect);

#ifdef __cplusplus
}
#endif
