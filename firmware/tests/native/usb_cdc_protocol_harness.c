#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "recorder_config.h"
#include "usb_cdc_protocol_core.h"

#define RESPONSE_BYTES 768u

static bool s_pending = false;

static bool status_provider(usb_cdc_protocol_status_t *out, void *ctx) {
    (void)ctx;
    if (out == NULL) {
        return false;
    }
    out->state = "USB_SYNC";
    out->reason = "usb";
    out->battery_mv = 3900;
    out->battery_valid = true;
    out->rtc_correction_pending = s_pending;
    return true;
}

static rtc_correction_result_t host_set_time(const char *requested_time,
                                              char *normalized,
                                              size_t normalized_size) {
    const char *value = NULL;

    // Hardware/NVS is the platform seam. Classification below supplies the
    // backend outcomes needed to execute the production CDC mapping. The
    // firmware build still binds this exact dispatcher to rtc_correction_apply.
    if (requested_time == NULL || normalized == NULL || normalized_size < 26u) {
        return RTC_CORRECTION_INVALID_ARGS;
    }
    if (strcmp(requested_time, "2026-02-30T10:30:00+09:00") == 0 ||
        strcmp(requested_time, "1999-12-31T23:59:59Z") == 0) {
        return RTC_CORRECTION_RANGE_ERROR;
    }
    if (strcmp(requested_time, "2026-10-03T10:30:00") == 0) {
        return RTC_CORRECTION_INVALID_ARGS;
    }
    if (strcmp(requested_time, "2026-10-03T10:30:00+09:00") == 0) {
        value = "2026-10-03T01:30:00+00:00";
    } else if (strcmp(requested_time, "2026-10-03T02:00:00Z") == 0) {
        value = "2026-10-03T02:00:00+00:00";
    } else {
        return RTC_CORRECTION_INVALID_ARGS;
    }
    if (s_pending) {
        return RTC_CORRECTION_BUSY;
    }
    s_pending = true;
    memcpy(normalized, value, strlen(value) + 1u);
    return RTC_CORRECTION_OK;
}

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

static void process(const usb_cdc_protocol_config_t *config, const char *raw,
                    char response[RESPONSE_BYTES]) {
    int n;
    memset(response, 0, RESPONSE_BYTES);
    n = usb_cdc_protocol_process_line((const uint8_t *)raw, strlen(raw),
                                      response, RESPONSE_BYTES, config,
                                      host_set_time);
    if (n <= 0 || (size_t)n >= RESPONSE_BYTES) {
        fail("processor returned invalid response length", response);
    }
}

static void test_command_vectors(const usb_cdc_protocol_config_t *config) {
    char response[RESPONSE_BYTES];
    char oversized[USB_CDC_PROTOCOL_MAX_LINE_BYTES + 2u];

    s_pending = false;

    process(config, "{\"id\":\"p1\",\"cmd\":\"PING\",\"args\":{}}",
            response);
    expect_contains(response, "\"ok\":true", "PING must succeed");
    expect_contains(response, "\"pong\":true", "PING pong missing");

    process(config,
            "{\"id\":\"i1\",\"cmd\":\"GET_INFO\",\"args\":{}}",
            response);
    expect_contains(response, "\"protocolMajor\":1", "GET_INFO major");
    expect_contains(response, "M5Capsule v1.1", "GET_INFO model");

    process(config,
            "{\"id\":\"s1\",\"cmd\":\"GET_STATUS\",\"args\":{}}",
            response);
    expect_contains(response, "\"state\":\"USB_SYNC\"", "GET_STATUS state");
    expect_contains(response, "\"rtcCorrectionPending\":false",
                    "GET_STATUS pending");

    process(config, "{", response);
    expect_contains(response, "\"code\":\"INVALID_JSON\"",
                    "malformed JSON code");

    process(config, "[]", response);
    expect_contains(response, "\"code\":\"INVALID_REQUEST\"",
                    "non-object envelope code");

    process(config, "{\"cmd\":\"PING\",\"args\":{}}", response);
    expect_contains(response, "\"id\":null", "missing id must not echo");
    expect_contains(response, "\"code\":\"INVALID_REQUEST\"",
                    "missing id code");

    process(config, "{\"id\":\"bad id\",\"cmd\":\"PING\",\"args\":{}}",
            response);
    expect_contains(response, "\"id\":null", "invalid id must not echo");
    expect_contains(response, "\"code\":\"INVALID_REQUEST\"",
                    "invalid id code");

    process(config, "{\"id\":\"m1\",\"args\":{}}", response);
    expect_contains(response, "\"code\":\"INVALID_REQUEST\"",
                    "missing cmd code");

    process(config, "{\"id\":\"a1\",\"cmd\":\"PING\",\"args\":[]}",
            response);
    expect_contains(response, "\"code\":\"INVALID_REQUEST\"",
                    "non-object args code");

    process(config, "{\"id\":\"u1\",\"cmd\":\"NOPE\",\"args\":{}}",
            response);
    expect_contains(response, "\"id\":\"u1\"", "unknown command id");
    expect_contains(response, "\"code\":\"UNKNOWN_COMMAND\"",
                    "unknown command code");

    process(config, "{\"id\":\"t0\",\"cmd\":\"SET_TIME\",\"args\":{}}",
            response);
    expect_contains(response, "\"code\":\"INVALID_ARGS\"",
                    "missing SET_TIME arg");

    process(config,
            "{\"id\":\"t0b\",\"cmd\":\"SET_TIME\",\"args\":{\"time\":123}}",
            response);
    expect_contains(response, "\"code\":\"INVALID_ARGS\"",
                    "non-string SET_TIME arg");

    process(config,
            "{\"id\":\"t0c\",\"cmd\":\"SET_TIME\",\"args\":{\"time\":\"2026-10-03T10:30:00\"}}",
            response);
    expect_contains(response, "\"code\":\"INVALID_ARGS\"",
                    "naive timestamp code");

    process(config,
            "{\"id\":\"r1\",\"cmd\":\"SET_TIME\",\"args\":{\"time\":\"2026-02-30T10:30:00+09:00\"}}",
            response);
    expect_contains(response, "\"code\":\"RANGE_ERROR\"",
                    "impossible calendar date mapping");

    process(config,
            "{\"id\":\"r2\",\"cmd\":\"SET_TIME\",\"args\":{\"time\":\"1999-12-31T23:59:59Z\"}}",
            response);
    expect_contains(response, "\"code\":\"RANGE_ERROR\"",
                    "supported RTC range mapping");

    s_pending = false;
    process(config,
            "{\"id\":\"t1\",\"cmd\":\"SET_TIME\",\"args\":{\"time\":\"2026-10-03T10:30:00+09:00\"}}",
            response);
    expect_contains(response, "\"ok\":true", "offset SET_TIME success");
    expect_contains(response, "2026-10-03T01:30:00+00:00",
                    "offset normalization");
    expect_contains(response, "\"eventPending\":true",
                    "SET_TIME pending flag");

    process(config,
            "{\"id\":\"t2\",\"cmd\":\"SET_TIME\",\"args\":{\"time\":\"2026-10-03T02:00:00Z\"}}",
            response);
    expect_contains(response, "\"code\":\"BUSY\"",
                    "second SET_TIME must be BUSY");

    s_pending = false;
    process(config,
            "{\"id\":\"tz\",\"cmd\":\"SET_TIME\",\"args\":{\"time\":\"2026-10-03T02:00:00Z\"}}",
            response);
    expect_contains(response, "2026-10-03T02:00:00+00:00",
                    "Z normalization");

    memset(oversized, 'x', sizeof(oversized));
    oversized[USB_CDC_PROTOCOL_MAX_LINE_BYTES + 1u] = '\0';
    memset(response, 0, sizeof(response));
    if (usb_cdc_protocol_process_line(
            (const uint8_t *)oversized, USB_CDC_PROTOCOL_MAX_LINE_BYTES + 1u,
            response, sizeof(response), config, host_set_time) <= 0) {
        fail("oversized request response", response);
    }
    expect_contains(response, "\"code\":\"REQUEST_TOO_LARGE\"",
                    "oversized request code");
}

static void test_framer_vectors(const usb_cdc_protocol_config_t *config) {
    usb_cdc_protocol_framer_t framer;
    const uint8_t *line = NULL;
    size_t line_len = 0u;
    usb_cdc_protocol_frame_result_t result = USB_CDC_PROTOCOL_FRAME_NONE;
    char response[RESPONSE_BYTES];
    const char *partial =
        "{\"id\":\"stale\",\"cmd\":\"SET_TIME\",\"args\":{\"time\":\"2026";
    const char *tail = "-10-03T10:30:00Z\"}}\n";
    const char *good = "{\"id\":\"next\",\"cmd\":\"PING\",\"args\":{}}\r\n";
    size_t i;

    usb_cdc_protocol_framer_init(&framer);
    for (i = 0u; partial[i] != '\0'; ++i) {
        result = usb_cdc_protocol_framer_feed(&framer, (uint8_t)partial[i],
                                              &line, &line_len);
        if (result != USB_CDC_PROTOCOL_FRAME_NONE) {
            fail("partial frame completed early", NULL);
        }
    }

    usb_cdc_protocol_framer_reset(&framer);
    s_pending = false;
    for (i = 0u; tail[i] != '\0'; ++i) {
        result = usb_cdc_protocol_framer_feed(&framer, (uint8_t)tail[i],
                                              &line, &line_len);
    }
    if (result != USB_CDC_PROTOCOL_FRAME_LINE) {
        fail("tail after reset must form one independent line", NULL);
    }
    memset(response, 0, sizeof(response));
    if (usb_cdc_protocol_process_line(line, line_len, response,
                                      sizeof(response), config,
                                      host_set_time) <= 0) {
        fail("stale tail response", response);
    }
    expect_contains(response, "\"code\":\"INVALID_JSON\"",
                    "stale prefix must not survive reset");
    if (s_pending) {
        fail("stale partial SET_TIME mutated pending state", response);
    }

    for (i = 0u; good[i] != '\0'; ++i) {
        result = usb_cdc_protocol_framer_feed(&framer, (uint8_t)good[i],
                                              &line, &line_len);
    }
    if (result != USB_CDC_PROTOCOL_FRAME_LINE) {
        fail("CRLF PING framing", NULL);
    }
    memset(response, 0, sizeof(response));
    if (usb_cdc_protocol_process_line(line, line_len, response,
                                      sizeof(response), config,
                                      host_set_time) <= 0) {
        fail("PING after reset", response);
    }
    expect_contains(response, "\"id\":\"next\"", "next request id");
    expect_contains(response, "\"pong\":true", "next PING result");

    usb_cdc_protocol_framer_reset(&framer);
    result = USB_CDC_PROTOCOL_FRAME_NONE;
    for (i = 0u; i < USB_CDC_PROTOCOL_MAX_LINE_BYTES + 1u; ++i) {
        result = usb_cdc_protocol_framer_feed(&framer, (uint8_t)'x',
                                              &line, &line_len);
        if (result != USB_CDC_PROTOCOL_FRAME_NONE) {
            fail("oversized frame completed before LF", NULL);
        }
    }
    result = usb_cdc_protocol_framer_feed(&framer, (uint8_t)'\n',
                                          &line, &line_len);
    if (result != USB_CDC_PROTOCOL_FRAME_TOO_LARGE) {
        fail("oversized frame classification", NULL);
    }

    for (i = 0u; good[i] != '\0'; ++i) {
        result = usb_cdc_protocol_framer_feed(&framer, (uint8_t)good[i],
                                              &line, &line_len);
    }
    if (result != USB_CDC_PROTOCOL_FRAME_LINE) {
        fail("framer did not recover after oversized line", NULL);
    }
}

int main(void) {
    usb_cdc_protocol_config_t config = {
        .device_id = "01234567-89ab-4def-8123-456789abcdef",
        .status_provider = status_provider,
        .status_ctx = NULL,
    };

    test_command_vectors(&config);
    test_framer_vectors(&config);
    printf("production protocol vectors: PASS\n");
    return 0;
}
