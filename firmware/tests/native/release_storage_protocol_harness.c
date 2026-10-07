#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "usb_cdc_protocol_core.h"

#define CHECK(expr) do { if (!(expr)) { \
    fprintf(stderr, "CHECK failed: %s:%d: %s\n", __FILE__, __LINE__, #expr); \
    return 1; \
} } while (0)

static usb_cdc_release_result_t g_result = USB_CDC_RELEASE_ACCEPTED;
static int g_accept_calls = 0;
static char g_attempt[USB_CDC_PROTOCOL_MAX_ID_BYTES + 1u];

static usb_cdc_release_result_t accept_release(const char *attempt, void *ctx) {
    (void)ctx;
    g_accept_calls++;
    snprintf(g_attempt, sizeof(g_attempt), "%s", attempt);
    return g_result;
}

static bool response_complete(const char *attempt, void *ctx) {
    (void)attempt;
    (void)ctx;
    return true;
}

static bool admission_open(void *ctx) {
    (void)ctx;
    return true;
}

static int process(const char *request, usb_cdc_protocol_config_t *config,
                   char *response, size_t response_size,
                   usb_cdc_protocol_effect_t *effect) {
    return usb_cdc_protocol_process_line(
        (const uint8_t *)request, strlen(request), response, response_size,
        config, effect);
}

int main(void) {
    usb_cdc_protocol_config_t config = {
        .release_accept = accept_release,
        .release_response_complete = response_complete,
        .command_admission_open = admission_open,
        .lifecycle_ctx = NULL,
    };
    usb_cdc_protocol_effect_t effect;
    usb_cdc_protocol_framer_t framer;
    char response[384];
    const uint8_t *line = NULL;
    size_t line_len = 0u;
    int n;

    g_result = USB_CDC_RELEASE_ACCEPTED;
    n = process("{\"id\":\"req-1\",\"cmd\":\"RELEASE_STORAGE\","
                "\"args\":{\"releaseAttemptId\":\"attempt-1\"}}",
                &config, response, sizeof(response), &effect);
    CHECK(n > 0);
    CHECK(strcmp(response,
                 "{\"id\":\"req-1\",\"ok\":true,\"result\":{"
                 "\"releaseAttemptId\":\"attempt-1\","
                 "\"accepted\":true}}") == 0);
    CHECK(g_accept_calls == 1);
    CHECK(strcmp(g_attempt, "attempt-1") == 0);
    CHECK(effect.release_accepted);
    CHECK(strcmp(effect.release_attempt_id, "attempt-1") == 0);

    n = process("{\"id\":\"bad\",\"cmd\":\"RELEASE_STORAGE\","
                "\"args\":{}}",
                &config, response, sizeof(response), &effect);
    CHECK(n > 0);
    CHECK(strstr(response, "\"code\":\"INVALID_ARGS\"") != NULL);
    CHECK(g_accept_calls == 1);
    CHECK(!effect.release_accepted);

    n = process("{\"id\":\"bad2\",\"cmd\":\"RELEASE_STORAGE\","
                "\"args\":{\"releaseAttemptId\":\"bad id\"}}",
                &config, response, sizeof(response), &effect);
    CHECK(n > 0);
    CHECK(strstr(response, "\"code\":\"INVALID_ARGS\"") != NULL);
    CHECK(g_accept_calls == 1);

    g_result = USB_CDC_RELEASE_WRONG_STATE;
    n = process("{\"id\":\"state\",\"cmd\":\"RELEASE_STORAGE\","
                "\"args\":{\"releaseAttemptId\":\"attempt-state\"}}",
                &config, response, sizeof(response), &effect);
    CHECK(n > 0);
    CHECK(strstr(response, "\"code\":\"BUSY\"") != NULL);
    CHECK(!effect.release_accepted);

    g_result = USB_CDC_RELEASE_CONFLICT;
    n = process("{\"id\":\"conflict\",\"cmd\":\"RELEASE_STORAGE\","
                "\"args\":{\"releaseAttemptId\":\"attempt-2\"}}",
                &config, response, sizeof(response), &effect);
    CHECK(n > 0);
    CHECK(strstr(response, "\"code\":\"BUSY\"") != NULL);
    CHECK(!effect.release_accepted);

    n = process("{\"id\":\"u1\",\"cmd\":\"PING\",\"args\":{}}",
                &config, response, sizeof(response), &effect);
    CHECK(n > 0);
    CHECK(strstr(response, "\"code\":\"UNKNOWN_COMMAND\"") != NULL);

    usb_cdc_protocol_framer_init(&framer);
    {
        const char *crlf =
            "{\"id\":\"f1\",\"cmd\":\"RELEASE_STORAGE\","
            "\"args\":{\"releaseAttemptId\":\"f-attempt\"}}\r\n";
        size_t i;
        usb_cdc_protocol_frame_result_t r = USB_CDC_PROTOCOL_FRAME_NONE;
        for (i = 0u; crlf[i] != '\0'; ++i) {
            r = usb_cdc_protocol_framer_feed(
                &framer, (uint8_t)crlf[i], &line, &line_len);
        }
        CHECK(r == USB_CDC_PROTOCOL_FRAME_LINE);
        CHECK(line_len > 0u);
        CHECK(line[line_len - 1u] == '}');
    }

    usb_cdc_protocol_framer_reset(&framer);
    {
        size_t i;
        usb_cdc_protocol_frame_result_t r = USB_CDC_PROTOCOL_FRAME_NONE;
        for (i = 0u; i < USB_CDC_PROTOCOL_MAX_LINE_BYTES + 1u; ++i) {
            r = usb_cdc_protocol_framer_feed(&framer, 'x', &line, &line_len);
            CHECK(r == USB_CDC_PROTOCOL_FRAME_NONE);
        }
        r = usb_cdc_protocol_framer_feed(&framer, '\n', &line, &line_len);
        CHECK(r == USB_CDC_PROTOCOL_FRAME_TOO_LARGE);
    }

    puts("release storage production protocol: PASS");
    return 0;
}
