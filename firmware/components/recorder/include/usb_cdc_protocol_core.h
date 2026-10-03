#pragma once

// Task #50 portable CDC protocol core.
//
// The parser/dispatcher and line framer live outside the ESP/TinyUSB transport
// so the exact Device command path can be executed by native contract tests.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "rtc_correction.h"
#include "usb_cdc_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef rtc_correction_result_t (*usb_cdc_protocol_set_time_fn_t)(
    const char *requested_time,
    char *normalized,
    size_t normalized_size);

typedef enum {
    USB_CDC_PROTOCOL_FRAME_NONE = 0,
    USB_CDC_PROTOCOL_FRAME_LINE,
    USB_CDC_PROTOCOL_FRAME_TOO_LARGE,
} usb_cdc_protocol_frame_result_t;

typedef struct {
    // One extra byte permits an exact 1024-byte payload followed by CRLF.
    uint8_t line[USB_CDC_PROTOCOL_MAX_LINE_BYTES + 1u];
    size_t line_len;
    bool oversized;
} usb_cdc_protocol_framer_t;

void usb_cdc_protocol_framer_init(usb_cdc_protocol_framer_t *framer);
void usb_cdc_protocol_framer_reset(usb_cdc_protocol_framer_t *framer);
usb_cdc_protocol_frame_result_t usb_cdc_protocol_framer_feed(
    usb_cdc_protocol_framer_t *framer,
    uint8_t byte,
    const uint8_t **out_line,
    size_t *out_len);

// Process exactly one already-framed request. `set_time` is the RTC/NVS
// transaction seam: firmware passes rtc_correction_apply; native tests pass a
// deterministic hardware-free implementation while executing this same parser
// and dispatcher.
int usb_cdc_protocol_process_line(
    const uint8_t *line,
    size_t len,
    char *out,
    size_t out_size,
    const usb_cdc_protocol_config_t *config,
    usb_cdc_protocol_set_time_fn_t set_time);

#ifdef __cplusplus
}
#endif
