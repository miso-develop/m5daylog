// Task #50: bounded sequential USB CDC JSON protocol v1 transport.
//
// TinyUSB callbacks never parse JSON or synchronously flush TX. They only
// update session admission and signal a dedicated worker. Complete commands are
// executed behind a generation-tagged session gate so physical teardown has a
// strict mutation cutoff before remount/event-flush/recording restart.

#include "usb_cdc_protocol.h"

#ifdef ESP_PLATFORM

#include <stdint.h>
#include <string.h>

#include "device_identity.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "rtc_correction.h"
#include "tinyusb_cdc_acm.h"
#include "usb_cdc_protocol_core.h"
#include "usb_cdc_session_gate.h"

#define CDC_RX_CHUNK_BYTES 256u
#define CDC_RESPONSE_BYTES 768u
#define CDC_TX_FLUSH_TICKS pdMS_TO_TICKS(250)

static usb_cdc_protocol_config_t s_config;
static TaskHandle_t s_worker = NULL;
static usb_cdc_session_gate_t s_session_gate;
static volatile bool s_connected = false;
static volatile bool s_reset_line = false;
static bool s_initialized = false;
static bool s_started = false;

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
    // RX itself is sufficient evidence that the current post-reset CDC session
    // is usable, even on hosts that do not assert DTR/RTS in the expected way.
    s_connected = true;
    usb_cdc_session_gate_open(&s_session_gate);
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
    if (connected) {
        usb_cdc_session_gate_open(&s_session_gate);
    } else {
        // Callback context must not block. Closing admission also increments the
        // generation, so a complete frame from the old session cannot execute
        // if a fast reconnect opens admission before the worker reaches it.
        usb_cdc_session_gate_close(&s_session_gate);
    }
    if (s_worker != NULL) {
        xTaskNotifyGive(s_worker);
    }
}

static void cdc_session_gate_wait(void *ctx) {
    (void)ctx;
    vTaskDelay(1);
}

static void usb_cdc_worker_task(void *arg) {
    usb_cdc_protocol_framer_t framer;
    uint8_t chunk[CDC_RX_CHUNK_BYTES];
    uint32_t frame_generation = 0u;
    (void)arg;

    usb_cdc_protocol_framer_init(&framer);
    for (;;) {
        size_t got = 0u;
        size_t i;
        (void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        if (s_reset_line) {
            usb_cdc_protocol_framer_reset(&framer);
            s_reset_line = false;
        }
        do {
            got = 0u;
            if (tinyusb_cdcacm_read(TINYUSB_CDC_ACM_0, chunk, sizeof(chunk),
                                    &got) != ESP_OK) {
                break;
            }
            if (!s_connected) {
                usb_cdc_protocol_framer_reset(&framer);
                continue;  // drain stale bytes after disconnect; never execute
            }
            for (i = 0u; i < got; ++i) {
                const uint8_t *frame_line = NULL;
                size_t frame_len = 0u;
                usb_cdc_protocol_frame_result_t frame_result;
                char response[CDC_RESPONSE_BYTES];
                int response_len = 0;

                // Tag the frame at its first byte. reset/close increments the
                // generation, permanently invalidating a complete old-session
                // frame even if the host reconnects before it executes.
                if (framer.line_len == 0u && !framer.oversized) {
                    frame_generation =
                        usb_cdc_session_gate_generation(&s_session_gate);
                }
                frame_result = usb_cdc_protocol_framer_feed(
                    &framer, chunk[i], &frame_line, &frame_len);
                if (frame_result == USB_CDC_PROTOCOL_FRAME_NONE) {
                    continue;
                }

                if (usb_cdc_session_gate_command_begin(&s_session_gate,
                                                       frame_generation)) {
                    response_len = usb_cdc_protocol_process_line(
                        frame_line, frame_len, response, sizeof(response),
                        &s_config, rtc_correction_apply);
                    usb_cdc_session_gate_command_end(&s_session_gate);
                }
                if (response_len > 0 && s_connected) {
                    (void)cdc_write_response(response, (size_t)response_len);
                }
            }
        } while (got != 0u);
    }
}

void usb_cdc_protocol_reset_session(void) {
    // Physical attach/detach uses a blocking reset. It closes admission and
    // invalidates pre-reset frames before waiting for any command already in
    // rtc_correction_apply() to finish its RTC/NVS transaction. Therefore the
    // caller may remount/flush only after all pre-boundary effects are fixed.
    s_connected = false;
    s_reset_line = true;
    if (s_worker != NULL) {
        xTaskNotifyGive(s_worker);
    }
    usb_cdc_session_gate_reset(&s_session_gate, cdc_session_gate_wait, NULL);
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
    usb_cdc_session_gate_init(&s_session_gate);
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

void usb_cdc_protocol_reset_session(void) {
}

esp_err_t usb_cdc_protocol_start(void) {
    return ESP_ERR_NOT_SUPPORTED;
}

#endif
