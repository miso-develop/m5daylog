// Task #50: bounded sequential USB CDC JSON protocol v1.
//
// TinyUSB callbacks never parse JSON or synchronously flush TX. They only
// signal a dedicated worker, avoiding callback-context deadlock/latency while
// preserving strict request/response ordering. No request payload is logged.

#include "usb_cdc_protocol.h"

#ifdef ESP_PLATFORM

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "device_identity.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "recorder_config.h"
#include "rtc_correction.h"
#include "tinyusb_cdc_acm.h"

#define CDC_RX_CHUNK_BYTES 256u
#define CDC_RESPONSE_BYTES 768u
#define CDC_TX_FLUSH_TICKS pdMS_TO_TICKS(250)

static usb_cdc_protocol_config_t s_config;
static TaskHandle_t s_worker = NULL;
static volatile bool s_connected = false;
static volatile bool s_reset_line = false;
static bool s_initialized = false;
static bool s_started = false;

static bool cdc_id_valid(const char *id) {
    size_t i;
    size_t len;
    if (id == NULL) {
        return false;
    }
    len = strlen(id);
    if (len == 0u || len > USB_CDC_PROTOCOL_MAX_ID_BYTES) {
        return false;
    }
    for (i = 0; i < len; ++i) {
        char c = id[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
              (c >= '0' && c <= '9') || c == '.' || c == '_' || c == ':' ||
              c == '-')) {
            return false;
        }
    }
    return true;
}

static bool cdc_utf8_valid(const uint8_t *data, size_t len) {
    size_t i = 0;
    while (i < len) {
        uint8_t c = data[i++];
        uint32_t code;
        unsigned need;
        unsigned j;
        if (c == 0u) {
            return false;
        }
        if (c < 0x80u) {
            continue;
        }
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
        if (i + need > len) {
            return false;
        }
        for (j = 0; j < need; ++j) {
            uint8_t cc = data[i++];
            if ((cc & 0xc0u) != 0x80u) {
                return false;
            }
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

static bool cdc_args_empty(const cJSON *args) {
    return cJSON_IsObject(args) && args->child == NULL;
}

static const char *cdc_rtc_error_code(rtc_correction_result_t result) {
    switch (result) {
        case RTC_CORRECTION_INVALID_ARGS:
            return USB_CDC_ERROR_INVALID_ARGS;
        case RTC_CORRECTION_RANGE_ERROR:
            return USB_CDC_ERROR_RANGE_ERROR;
        case RTC_CORRECTION_BUSY:
            return USB_CDC_ERROR_BUSY;
        case RTC_CORRECTION_INTERNAL_ERROR:
        default:
            return USB_CDC_ERROR_INTERNAL_ERROR;
    }
}

static int cdc_protocol_process_line(const uint8_t *line, size_t len,
                                     char *out, size_t out_size) {
    cJSON *root = NULL;
    cJSON *id_item;
    cJSON *cmd_item;
    cJSON *args_item;
    const char *id = NULL;
    const char *cmd;
    int n = -1;

    if (line == NULL || out == NULL || out_size == 0u) {
        return -1;
    }
    if (len > USB_CDC_PROTOCOL_MAX_LINE_BYTES) {
        return cdc_error_response(out, out_size, NULL,
                                  USB_CDC_ERROR_REQUEST_TOO_LARGE);
    }
    if (!cdc_utf8_valid(line, len)) {
        return cdc_error_response(out, out_size, NULL,
                                  USB_CDC_ERROR_INVALID_JSON);
    }

    // Require the entire framed line to be one JSON value; do not accept a
    // valid prefix followed by trailing non-whitespace bytes.
    root = cJSON_ParseWithOpts((const char *)line, NULL, true);
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

    if (strcmp(cmd, "PING") == 0) {
        if (!cdc_args_empty(args_item)) {
            n = cdc_error_response(out, out_size, id,
                                   USB_CDC_ERROR_INVALID_ARGS);
        } else {
            n = snprintf(out, out_size,
                         "{\"id\":\"%s\",\"ok\":true,\"result\":{"
                         "\"pong\":true}}",
                         id);
        }
    } else if (strcmp(cmd, "GET_INFO") == 0) {
        if (!cdc_args_empty(args_item)) {
            n = cdc_error_response(out, out_size, id,
                                   USB_CDC_ERROR_INVALID_ARGS);
        } else {
            n = snprintf(
                out, out_size,
                "{\"id\":\"%s\",\"ok\":true,\"result\":{"
                "\"protocolMajor\":%u,\"schemaVersion\":%u,"
                "\"deviceId\":\"%s\",\"model\":\"%s\","
                "\"firmwareVersion\":\"%s\",\"audioCapabilities\":{"
                "\"sampleRate\":%u,\"bitDepth\":%u,\"channels\":%u,"
                "\"format\":\"pcm\"}}}",
                id, (unsigned)USB_CDC_PROTOCOL_MAJOR,
                (unsigned)RECORDER_METADATA_SCHEMA_VERSION, s_config.device_id,
                RECORDER_MODEL, RECORDER_FIRMWARE_VERSION,
                (unsigned)RECORDER_SAMPLE_RATE_HZ,
                (unsigned)RECORDER_BITS_PER_SAMPLE,
                (unsigned)RECORDER_CHANNELS);
        }
    } else if (strcmp(cmd, "GET_STATUS") == 0) {
        usb_cdc_protocol_status_t status;
        memset(&status, 0, sizeof(status));
        if (!cdc_args_empty(args_item)) {
            n = cdc_error_response(out, out_size, id,
                                   USB_CDC_ERROR_INVALID_ARGS);
        } else if (s_config.status_provider == NULL ||
                   !s_config.status_provider(&status, s_config.status_ctx) ||
                   status.state == NULL || status.reason == NULL) {
            n = cdc_error_response(out, out_size, id,
                                   USB_CDC_ERROR_INTERNAL_ERROR);
        } else if (status.battery_valid) {
            n = snprintf(
                out, out_size,
                "{\"id\":\"%s\",\"ok\":true,\"result\":{"
                "\"state\":\"%s\",\"reason\":\"%s\","
                "\"batteryMv\":%d,\"rtcCorrectionPending\":%s}}",
                id, status.state, status.reason, status.battery_mv,
                status.rtc_correction_pending ? "true" : "false");
        } else {
            n = snprintf(
                out, out_size,
                "{\"id\":\"%s\",\"ok\":true,\"result\":{"
                "\"state\":\"%s\",\"reason\":\"%s\","
                "\"batteryMv\":null,\"rtcCorrectionPending\":%s}}",
                id, status.state, status.reason,
                status.rtc_correction_pending ? "true" : "false");
        }
    } else if (strcmp(cmd, "SET_TIME") == 0) {
        cJSON *time_item = cJSON_GetObjectItemCaseSensitive(args_item, "time");
        char normalized[RECORDER_ISO8601_STR_LEN];
        rtc_correction_result_t result;
        if (cJSON_GetArraySize(args_item) != 1 || !cJSON_IsString(time_item) ||
            time_item->valuestring == NULL) {
            n = cdc_error_response(out, out_size, id,
                                   USB_CDC_ERROR_INVALID_ARGS);
        } else {
            memset(normalized, 0, sizeof(normalized));
            result = rtc_correction_apply(time_item->valuestring, normalized,
                                          sizeof(normalized));
            if (result != RTC_CORRECTION_OK) {
                n = cdc_error_response(out, out_size, id,
                                       cdc_rtc_error_code(result));
            } else {
                n = snprintf(out, out_size,
                             "{\"id\":\"%s\",\"ok\":true,\"result\":{"
                             "\"time\":\"%s\",\"eventPending\":true}}",
                             id, normalized);
            }
        }
    } else {
        n = cdc_error_response(out, out_size, id,
                               USB_CDC_ERROR_UNKNOWN_COMMAND);
    }

done:
    // `id` points into root, so perform any overflow fallback while root is
    // still alive. This keeps the error path free of use-after-free reads.
    if (n < 0 || (size_t)n >= out_size) {
        n = cdc_error_response(out, out_size, id,
                               USB_CDC_ERROR_INTERNAL_ERROR);
    }
    cJSON_Delete(root);
    return n;
}

static bool cdc_write_response(const char *response, size_t len) {
    size_t sent = 0;
    while (sent < len) {
        size_t queued = tinyusb_cdcacm_write_queue(
            TINYUSB_CDC_ACM_0, (const uint8_t *)response + sent, len - sent);
        if (queued == 0u) {
            if (!s_connected) {
                return false;
            }
            if (tinyusb_cdcacm_write_flush(TINYUSB_CDC_ACM_0,
                                           pdMS_TO_TICKS(20)) != ESP_OK) {
                vTaskDelay(1);
            }
            continue;
        }
        sent += queued;
    }
    while (tinyusb_cdcacm_write_queue_char(TINYUSB_CDC_ACM_0, '\n') == 0u) {
        if (!s_connected) {
            return false;
        }
        (void)tinyusb_cdcacm_write_flush(TINYUSB_CDC_ACM_0,
                                         pdMS_TO_TICKS(20));
        vTaskDelay(1);
    }
    return tinyusb_cdcacm_write_flush(TINYUSB_CDC_ACM_0,
                                      CDC_TX_FLUSH_TICKS) == ESP_OK;
}

static void usb_cdc_rx_callback(int itf, cdcacm_event_t *event) {
    (void)itf;
    (void)event;
    s_connected = true;
    if (s_worker != NULL) {
        xTaskNotifyGive(s_worker);
    }
}

static void usb_cdc_line_state_callback(int itf, cdcacm_event_t *event) {
    bool connected;
    (void)itf;
    if (event == NULL) {
        return;
    }
    connected = event->line_state_changed_data.dtr ||
                event->line_state_changed_data.rts;
    s_connected = connected;
    s_reset_line = true;
    if (s_worker != NULL) {
        xTaskNotifyGive(s_worker);
    }
}

static void usb_cdc_worker_task(void *arg) {
    uint8_t line[USB_CDC_PROTOCOL_MAX_LINE_BYTES + 2u];
    uint8_t chunk[CDC_RX_CHUNK_BYTES];
    size_t line_len = 0;
    bool oversized = false;
    (void)arg;

    memset(line, 0, sizeof(line));
    for (;;) {
        size_t got = 0;
        size_t i;
        (void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        if (s_reset_line) {
            line_len = 0;
            oversized = false;
            s_reset_line = false;
        }
        do {
            got = 0;
            if (tinyusb_cdcacm_read(TINYUSB_CDC_ACM_0, chunk, sizeof(chunk),
                                    &got) != ESP_OK) {
                break;
            }
            if (!s_connected) {
                line_len = 0;
                oversized = false;
                continue;  // drain stale bytes after disconnect; never execute
            }
            for (i = 0; i < got; ++i) {
                uint8_t b = chunk[i];
                if (b == '\n') {
                    size_t payload_len = line_len;
                    char response[CDC_RESPONSE_BYTES];
                    int response_len;
                    if (payload_len != 0u && line[payload_len - 1u] == '\r') {
                        payload_len--;
                    }
                    if (oversized ||
                        payload_len > USB_CDC_PROTOCOL_MAX_LINE_BYTES) {
                        response_len = cdc_error_response(
                            response, sizeof(response), NULL,
                            USB_CDC_ERROR_REQUEST_TOO_LARGE);
                    } else {
                        line[payload_len] = '\0';
                        response_len = cdc_protocol_process_line(
                            line, payload_len, response, sizeof(response));
                    }
                    if (response_len > 0 && s_connected) {
                        (void)cdc_write_response(response,
                                                 (size_t)response_len);
                    }
                    line_len = 0;
                    oversized = false;
                    continue;
                }
                if (oversized) {
                    continue;
                }
                // One extra byte is retained so a 1024-byte payload followed
                // by CRLF remains valid; effective size is checked at LF.
                if (line_len < USB_CDC_PROTOCOL_MAX_LINE_BYTES + 1u) {
                    line[line_len++] = b;
                } else {
                    oversized = true;
                }
            }
        } while (got != 0u);
    }
}

esp_err_t usb_cdc_protocol_init(const usb_cdc_protocol_config_t *config) {
    if (config == NULL || config->device_id == NULL ||
        !device_identity_is_valid_uuid(config->device_id) ||
        config->status_provider == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_initialized) {
        return ESP_OK;
    }
    s_config = *config;
    s_initialized = true;
    return ESP_OK;
}

esp_err_t usb_cdc_protocol_start(void) {
    tinyusb_config_cdcacm_t acm_cfg = {
        .cdc_port = TINYUSB_CDC_ACM_0,
        .callback_rx = usb_cdc_rx_callback,
        .callback_rx_wanted_char = NULL,
        .callback_line_state_changed = usb_cdc_line_state_callback,
        .callback_line_coding_changed = NULL,
    };
    esp_err_t err;

    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_started) {
        return ESP_OK;
    }
    if (xTaskCreate(usb_cdc_worker_task, "cdc_json", 6144, NULL, 3,
                    &s_worker) != pdPASS) {
        s_worker = NULL;
        return ESP_ERR_NO_MEM;
    }
    err = tinyusb_cdcacm_init(&acm_cfg);
    if (err != ESP_OK) {
        vTaskDelete(s_worker);
        s_worker = NULL;
        return err;
    }
    s_started = true;
    return ESP_OK;
}

#else  // !ESP_PLATFORM

esp_err_t usb_cdc_protocol_init(const usb_cdc_protocol_config_t *config) {
    (void)config;
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t usb_cdc_protocol_start(void) {
    return ESP_ERR_NOT_SUPPORTED;
}

#endif
