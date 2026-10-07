// D-031 / Task #87: portable canonical CDC v1 parser/dispatcher.

#include "usb_cdc_protocol_core.h"

#include <stdio.h>
#include <string.h>

#include "cJSON.h"

static bool cdc_id_valid(const char *id) {
    size_t i;
    size_t len;
    if (id == NULL) return false;
    len = strlen(id);
    if (len == 0u || len > USB_CDC_PROTOCOL_MAX_ID_BYTES) return false;
    for (i = 0u; i < len; ++i) {
        char c = id[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
              (c >= '0' && c <= '9') || c == '.' || c == '_' ||
              c == ':' || c == '-')) {
            return false;
        }
    }
    return true;
}

static bool cdc_utf8_valid(const uint8_t *data, size_t len) {
    size_t i = 0u;
    while (i < len) {
        uint8_t c = data[i++];
        uint32_t code;
        unsigned need;
        unsigned j;
        if (c == 0u) return false;
        if (c < 0x80u) continue;
        if (c >= 0xc2u && c <= 0xdfu) {
            code = c & 0x1fu;
            need = 1u;
        } else if (c >= 0xe0u && c <= 0xefu) {
            code = c & 0x0fu;
            need = 2u;
        } else if (c >= 0xf0u && c <= 0xf4u) {
            code = c & 0x07u;
            need = 3u;
        } else {
            return false;
        }
        if (i + need > len) return false;
        for (j = 0u; j < need; ++j) {
            uint8_t cc = data[i++];
            if ((cc & 0xc0u) != 0x80u) return false;
            code = (code << 6u) | (cc & 0x3fu);
        }
        if ((need == 1u && code < 0x80u) ||
            (need == 2u && code < 0x800u) ||
            (need == 3u && code < 0x10000u) || code > 0x10ffffu ||
            (code >= 0xd800u && code <= 0xdfffu)) {
            return false;
        }
    }
    return true;
}

static int cdc_error_response(char *out, size_t out_size, const char *id,
                              const char *code) {
    if (id != NULL && cdc_id_valid(id)) {
        return snprintf(out, out_size,
                        "{\"id\":\"%s\",\"ok\":false,\"error\":{"
                        "\"code\":\"%s\",\"message\":\"%s\"}}",
                        id, code, code);
    }
    return snprintf(out, out_size,
                    "{\"id\":null,\"ok\":false,\"error\":{"
                    "\"code\":\"%s\",\"message\":\"%s\"}}",
                    code, code);
}

static const char *cdc_release_error_code(usb_cdc_release_result_t result) {
    switch (result) {
        case USB_CDC_RELEASE_INVALID_ARGS:
            return USB_CDC_ERROR_INVALID_ARGS;
        case USB_CDC_RELEASE_WRONG_STATE:
        case USB_CDC_RELEASE_CONFLICT:
            return USB_CDC_ERROR_BUSY;
        case USB_CDC_RELEASE_INTERNAL_ERROR:
        default:
            return USB_CDC_ERROR_INTERNAL_ERROR;
    }
}

void usb_cdc_protocol_framer_init(usb_cdc_protocol_framer_t *framer) {
    usb_cdc_protocol_framer_reset(framer);
}

void usb_cdc_protocol_framer_reset(usb_cdc_protocol_framer_t *framer) {
    if (framer == NULL) return;
    framer->line_len = 0u;
    framer->oversized = false;
}

usb_cdc_protocol_frame_result_t usb_cdc_protocol_framer_feed(
    usb_cdc_protocol_framer_t *framer,
    uint8_t byte,
    const uint8_t **out_line,
    size_t *out_len) {
    size_t payload_len;
    bool too_large;

    if (out_line != NULL) *out_line = NULL;
    if (out_len != NULL) *out_len = 0u;
    if (framer == NULL || out_line == NULL || out_len == NULL) {
        return USB_CDC_PROTOCOL_FRAME_NONE;
    }
    if (byte != '\n') {
        if (!framer->oversized) {
            if (framer->line_len < USB_CDC_PROTOCOL_MAX_LINE_BYTES + 1u) {
                framer->line[framer->line_len++] = byte;
            } else {
                framer->oversized = true;
            }
        }
        return USB_CDC_PROTOCOL_FRAME_NONE;
    }

    payload_len = framer->line_len;
    if (payload_len != 0u && framer->line[payload_len - 1u] == '\r') {
        payload_len--;
    }
    too_large = framer->oversized ||
                payload_len > USB_CDC_PROTOCOL_MAX_LINE_BYTES;
    *out_line = framer->line;
    *out_len = too_large ? USB_CDC_PROTOCOL_MAX_LINE_BYTES + 1u : payload_len;
    framer->line_len = 0u;
    framer->oversized = false;
    return too_large ? USB_CDC_PROTOCOL_FRAME_TOO_LARGE
                     : USB_CDC_PROTOCOL_FRAME_LINE;
}

int usb_cdc_protocol_process_line(
    const uint8_t *line,
    size_t len,
    char *out,
    size_t out_size,
    const usb_cdc_protocol_config_t *config,
    usb_cdc_protocol_effect_t *effect) {
    char json[USB_CDC_PROTOCOL_MAX_LINE_BYTES + 1u];
    cJSON *root = NULL;
    cJSON *id_item;
    cJSON *cmd_item;
    cJSON *args_item;
    const char *id = NULL;
    const char *cmd;
    int n = -1;

    if (effect != NULL) memset(effect, 0, sizeof(*effect));
    if (out == NULL || out_size == 0u) return -1;
    if (len > USB_CDC_PROTOCOL_MAX_LINE_BYTES) {
        return cdc_error_response(out, out_size, NULL,
                                  USB_CDC_ERROR_REQUEST_TOO_LARGE);
    }
    if (line == NULL) return -1;
    if (!cdc_utf8_valid(line, len)) {
        return cdc_error_response(out, out_size, NULL,
                                  USB_CDC_ERROR_INVALID_JSON);
    }
    memcpy(json, line, len);
    json[len] = '\0';

    root = cJSON_ParseWithOpts(json, NULL, true);
    if (root == NULL) {
        return cdc_error_response(out, out_size, NULL,
                                  USB_CDC_ERROR_INVALID_JSON);
    }
    if (!cJSON_IsObject(root)) {
        n = cdc_error_response(out, out_size, NULL,
                               USB_CDC_ERROR_INVALID_REQUEST);
        goto done;
    }

    id_item = cJSON_GetObjectItemCaseSensitive(root, "id");
    if (!cJSON_IsString(id_item) || id_item->valuestring == NULL ||
        !cdc_id_valid(id_item->valuestring)) {
        n = cdc_error_response(out, out_size, NULL,
                               USB_CDC_ERROR_INVALID_REQUEST);
        goto done;
    }
    id = id_item->valuestring;
    cmd_item = cJSON_GetObjectItemCaseSensitive(root, "cmd");
    args_item = cJSON_GetObjectItemCaseSensitive(root, "args");
    if (!cJSON_IsString(cmd_item) || cmd_item->valuestring == NULL ||
        !cJSON_IsObject(args_item)) {
        n = cdc_error_response(out, out_size, id,
                               USB_CDC_ERROR_INVALID_REQUEST);
        goto done;
    }
    cmd = cmd_item->valuestring;

    if (strcmp(cmd, "RELEASE_STORAGE") == 0) {
        cJSON *attempt_item =
            cJSON_GetObjectItemCaseSensitive(args_item, "releaseAttemptId");
        const char *attempt;
        usb_cdc_release_result_t result;

        if (cJSON_GetArraySize(args_item) != 1 ||
            !cJSON_IsString(attempt_item) ||
            attempt_item->valuestring == NULL ||
            !cdc_id_valid(attempt_item->valuestring)) {
            n = cdc_error_response(out, out_size, id,
                                   USB_CDC_ERROR_INVALID_ARGS);
            goto done;
        }
        if (config == NULL || config->release_accept == NULL) {
            n = cdc_error_response(out, out_size, id,
                                   USB_CDC_ERROR_INTERNAL_ERROR);
            goto done;
        }

        attempt = attempt_item->valuestring;
        result = config->release_accept(attempt, config->lifecycle_ctx);
        if (result != USB_CDC_RELEASE_ACCEPTED) {
            n = cdc_error_response(out, out_size, id,
                                   cdc_release_error_code(result));
            goto done;
        }

        n = snprintf(out, out_size,
                     "{\"id\":\"%s\",\"ok\":true,\"result\":{"
                     "\"releaseAttemptId\":\"%s\",\"accepted\":true}}",
                     id, attempt);
        if (effect != NULL) {
            effect->release_accepted = true;
            memcpy(effect->release_attempt_id, attempt, strlen(attempt) + 1u);
        }
    } else {
        n = cdc_error_response(out, out_size, id,
                               USB_CDC_ERROR_UNKNOWN_COMMAND);
    }

done:
    if (n < 0 || (size_t)n >= out_size) {
        if (effect != NULL) memset(effect, 0, sizeof(*effect));
        n = cdc_error_response(out, out_size, id,
                               USB_CDC_ERROR_INTERNAL_ERROR);
    }
    cJSON_Delete(root);
    return n;
}
