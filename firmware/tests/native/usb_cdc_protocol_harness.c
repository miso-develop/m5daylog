#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "usb_cdc_protocol_core.h"

#define RESPONSE_BYTES 768u
#define CHECK(expr) do { if (!(expr)) {     fprintf(stderr, "CHECK failed %s:%d: %s\n", __FILE__, __LINE__, #expr);     exit(2); } } while (0)

static bool s_pending;
static unsigned s_set_time_calls;
static unsigned s_release_calls;

static bool status_provider(usb_cdc_protocol_status_t *out, void *ctx) {
    (void)ctx;
    CHECK(out != NULL);
    out->state = "USB_SYNC";
    out->reason = "usb";
    out->battery_mv = 3900;
    out->battery_valid = true;
    out->rtc_correction_pending = s_pending;
    return true;
}

static usb_cdc_set_time_result_t set_time(
    const char *requested_time, char *normalized, size_t normalized_size,
    void *ctx) {
    const char *value = NULL;
    (void)ctx;
    s_set_time_calls++;
    if (s_pending) return USB_CDC_SET_TIME_BUSY;
    if (requested_time == NULL || normalized == NULL || normalized_size < 26u) {
        return USB_CDC_SET_TIME_INVALID_ARGS;
    }
    if (strcmp(requested_time, "2026-02-30T10:30:00+09:00") == 0 ||
        strcmp(requested_time, "1999-12-31T23:59:59Z") == 0) {
        return USB_CDC_SET_TIME_RANGE_ERROR;
    }
    if (strcmp(requested_time, "2026-10-03T10:30:00") == 0) {
        return USB_CDC_SET_TIME_INVALID_ARGS;
    }
    if (strcmp(requested_time, "2026-10-03T10:30:00+09:00") == 0) {
        value = "2026-10-03T01:30:00+00:00";
    } else if (strcmp(requested_time, "2026-10-03T02:00:00Z") == 0) {
        value = "2026-10-03T02:00:00+00:00";
    } else {
        return USB_CDC_SET_TIME_INVALID_ARGS;
    }
    memcpy(normalized, value, strlen(value) + 1u);
    s_pending = true;
    return USB_CDC_SET_TIME_OK;
}

static usb_cdc_release_result_t release_accept(
    const char *attempt, void *ctx) {
    (void)ctx;
    s_release_calls++;
    if (attempt == NULL || strcmp(attempt, "release-1") != 0) {
        return USB_CDC_RELEASE_INVALID_ARGS;
    }
    return USB_CDC_RELEASE_ACCEPTED;
}

static bool response_complete(const char *attempt, void *ctx) {
    (void)attempt; (void)ctx; return true;
}
static bool admission_open(void *ctx) {
    (void)ctx; return true;
}

static void expect(const usb_cdc_protocol_config_t *cfg, const char *raw,
                   const char *needle, usb_cdc_protocol_effect_t *effect) {
    char response[RESPONSE_BYTES];
    usb_cdc_protocol_effect_t local;
    int n;
    memset(response, 0, sizeof(response));
    memset(&local, 0, sizeof(local));
    n = usb_cdc_protocol_process_line(
        (const uint8_t *)raw, strlen(raw), response, sizeof(response),
        cfg, effect != NULL ? effect : &local);
    CHECK(n > 0 && (size_t)n < sizeof(response));
    if (strstr(response, needle) == NULL) {
        fprintf(stderr, "missing %s in %s\n", needle, response);
        exit(3);
    }
}

static void test_commands(const usb_cdc_protocol_config_t *cfg) {
    usb_cdc_protocol_effect_t effect;
    unsigned before_calls;

    s_pending = false;
    s_set_time_calls = 0u;
    s_release_calls = 0u;

    expect(cfg, "{\"id\":\"p\",\"cmd\":\"PING\",\"args\":{}}",
           "\"pong\":true", NULL);
    expect(cfg, "{\"id\":\"i\",\"cmd\":\"GET_INFO\",\"args\":{}}",
           "\"protocolMajor\":1", NULL);
    expect(cfg, "{\"id\":\"i2\",\"cmd\":\"GET_INFO\",\"args\":{}}",
           "\"deviceId\":\"01234567-89ab-4def-8123-456789abcdef\"", NULL);
    expect(cfg, "{\"id\":\"s\",\"cmd\":\"GET_STATUS\",\"args\":{}}",
           "\"rtcCorrectionPending\":false", NULL);

    expect(cfg, "{", "\"code\":\"INVALID_JSON\"", NULL);
    expect(cfg, "[]", "\"code\":\"INVALID_REQUEST\"", NULL);
    expect(cfg, "{\"id\":\"u\",\"cmd\":\"NOPE\",\"args\":{}}",
           "\"code\":\"UNKNOWN_COMMAND\"", NULL);

    before_calls = s_set_time_calls;
    expect(cfg, "{\"id\":\"bad\",\"cmd\":\"SET_TIME\",\"args\":{}}",
           "\"code\":\"INVALID_ARGS\"", NULL);
    CHECK(s_set_time_calls == before_calls);

    expect(cfg,
           "{\"id\":\"t\",\"cmd\":\"SET_TIME\",\"args\":{"
           "\"time\":\"2026-10-03T10:30:00+09:00\"}}",
           "\"eventPending\":true", NULL);
    CHECK(s_pending);
    expect(cfg, "{\"id\":\"s2\",\"cmd\":\"GET_STATUS\",\"args\":{}}",
           "\"rtcCorrectionPending\":true", NULL);
    expect(cfg,
           "{\"id\":\"t2\",\"cmd\":\"SET_TIME\",\"args\":{"
           "\"time\":\"2026-10-03T02:00:00Z\"}}",
           "\"code\":\"BUSY\"", NULL);

    memset(&effect, 0, sizeof(effect));
    before_calls = s_release_calls;
    expect(cfg,
           "{\"id\":\"r0\",\"cmd\":\"RELEASE_STORAGE\",\"args\":{}}",
           "\"code\":\"INVALID_ARGS\"", &effect);
    CHECK(!effect.release_accepted);
    CHECK(s_release_calls == before_calls);

    memset(&effect, 0, sizeof(effect));
    expect(cfg,
           "{\"id\":\"r1\",\"cmd\":\"RELEASE_STORAGE\",\"args\":{"
           "\"releaseAttemptId\":\"release-1\"}}",
           "\"accepted\":true", &effect);
    CHECK(effect.release_accepted);
    CHECK(strcmp(effect.release_attempt_id, "release-1") == 0);
    CHECK(s_release_calls == before_calls + 1u);
}

static void test_framing(void) {
    usb_cdc_protocol_framer_t framer;
    const uint8_t *line = NULL;
    size_t len = 0u;
    usb_cdc_protocol_frame_result_t result = USB_CDC_PROTOCOL_FRAME_NONE;
    const char *partial = "{\"id\":\"stale\",\"cmd\":\"SET_TIME\",\"args\":{";
    const char *tail = "\"time\":\"2026-10-03T02:00:00Z\"}}\n";
    size_t i;

    usb_cdc_protocol_framer_init(&framer);
    for (i = 0; partial[i] != '\0'; ++i) {
        CHECK(usb_cdc_protocol_framer_feed(
                  &framer, (uint8_t)partial[i], &line, &len) ==
              USB_CDC_PROTOCOL_FRAME_NONE);
    }
    usb_cdc_protocol_framer_reset(&framer);
    for (i = 0; tail[i] != '\0'; ++i) {
        result = usb_cdc_protocol_framer_feed(
            &framer, (uint8_t)tail[i], &line, &len);
    }
    CHECK(result == USB_CDC_PROTOCOL_FRAME_LINE);

    usb_cdc_protocol_framer_reset(&framer);
    for (i = 0; i < USB_CDC_PROTOCOL_MAX_LINE_BYTES + 1u; ++i) {
        CHECK(usb_cdc_protocol_framer_feed(
                  &framer, (uint8_t)'x', &line, &len) ==
              USB_CDC_PROTOCOL_FRAME_NONE);
    }
    CHECK(usb_cdc_protocol_framer_feed(
              &framer, (uint8_t)'\n', &line, &len) ==
          USB_CDC_PROTOCOL_FRAME_TOO_LARGE);
}

int main(void) {
    usb_cdc_protocol_config_t cfg = {
        .device_id = "01234567-89ab-4def-8123-456789abcdef",
        .status_provider = status_provider,
        .set_time = set_time,
        .release_accept = release_accept,
        .release_response_complete = response_complete,
        .command_admission_open = admission_open,
    };
    test_commands(&cfg);
    test_framing();
    puts("cdc v1 application protocol production core: PASS");
    return 0;
}
