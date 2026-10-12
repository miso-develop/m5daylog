// Task #50 / SEC-83-01: execute the production protocol core with ESP-IDF cJSON.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "usb_cdc_protocol_core.h"

#define RESPONSE_BYTES 768u

static unsigned s_set_time_calls = 0u;

static void fail(const char *message, const char *response) {
    fprintf(stderr, "FAIL: %s\nresponse: %s\n", message,
            response != NULL ? response : "<null>");
    exit(1);
}

static void expect_contains(const char *response, const char *needle,
                            const char *message) {
    if (response == NULL || strstr(response, needle) == NULL) {
        fail(message, response);
    }
}

static bool status_provider(usb_cdc_protocol_status_t *out, void *ctx) {
    (void)ctx;
    if (out == NULL) {
        return false;
    }
    out->state = "USB_SYNC";
    out->reason = "usb";
    out->battery_mv = 3900;
    out->battery_valid = true;
    out->rtc_correction_pending = false;
    return true;
}

static usb_cdc_set_time_result_t guarded_set_time(const char *requested_time,
                                                 char *normalized,
                                                 size_t normalized_size,
                                                 void *ctx) {
    (void)ctx;
    (void)requested_time;
    ++s_set_time_calls;
    if (normalized == NULL || normalized_size < 26u) {
        return USB_CDC_SET_TIME_INTERNAL_ERROR;
    }
    memcpy(normalized, "2026-10-03T02:00:00+00:00", 26u);
    return USB_CDC_SET_TIME_OK;
}

static void process(const usb_cdc_protocol_config_t *config, const char *raw,
                    char response[RESPONSE_BYTES]) {
    int n;
    memset(response, 0, RESPONSE_BYTES);
    usb_cdc_protocol_effect_t effect;
    memset(&effect, 0, sizeof(effect));
    n = usb_cdc_protocol_process_line((const uint8_t *)raw, strlen(raw),
                                      response, RESPONSE_BYTES, config,
                                      &effect);
    if (n <= 0 || (size_t)n >= RESPONSE_BYTES) {
        fail("processor returned invalid response length", response);
    }
}

int main(void) {
    usb_cdc_protocol_config_t config = {
        .device_id = "01234567-89ab-4def-8123-456789abcdef",
        .status_provider = status_provider,
        .status_ctx = NULL,
        .set_time = guarded_set_time,
        .set_time_ctx = NULL,
    };
    char response[RESPONSE_BYTES];

    // Root object=1, args=2, a=3, b=4. This is intentionally valid JSON at
    // the protocol ceiling; it must reach normal command validation.
    process(&config,
            "{\"id\":\"d4\",\"cmd\":\"PING\",\"args\":{\"a\":{\"b\":{}}}}",
            response);
    expect_contains(response, "\"code\":\"INVALID_ARGS\"",
                    "accepted depth did not reach command validation");

    // Root=1, args=2, a=3, b=4, c=5. The over-limit request contains a valid
    // mutating command, so rejection must occur before command dispatch.
    s_set_time_calls = 0u;
    process(&config,
            "{\"id\":\"deep\",\"cmd\":\"SET_TIME\",\"args\":{\"time\":\"2026-10-03T02:00:00Z\",\"a\":{\"b\":{\"c\":{}}}}}",
            response);
    expect_contains(response, "\"code\":\"INVALID_JSON\"",
                    "over-limit nesting was not rejected before cJSON dispatch");
    if (s_set_time_calls != 0u) {
        fail("over-limit nesting reached SET_TIME mutation seam", response);
    }

    // The rejected request must not poison parser/session state.
    process(&config, "{\"id\":\"next\",\"cmd\":\"PING\",\"args\":{}}",
            response);
    expect_contains(response, "\"id\":\"next\"", "post-rejection request id");
    expect_contains(response, "\"pong\":true",
                    "normal request failed after depth rejection");

    puts("production cJSON depth boundary: PASS");
    return 0;
}
