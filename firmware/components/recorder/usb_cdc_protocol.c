// Canonical bounded sequential USB CDC JSON transport.
//
// Task #87 lifecycle admission remains authoritative. Task #50 application
// commands execute inside the same session/command gate. RELEASE_STORAGE closes
// shared Device/MSC admission before its success response is written.

#include "usb_cdc_protocol.h"

#ifdef ESP_PLATFORM

#include <stdint.h>
#include <string.h>

#include "device_identity.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "tinyusb_cdc_acm.h"
#include "usb_cdc_protocol_core.h"
#include "usb_cdc_session_gate.h"
#include "usb_cdc_tx.h"

#define CDC_RX_CHUNK_BYTES 256u
#define CDC_RESPONSE_BYTES 768u
#define CDC_WORKER_STACK_BYTES 6144u

static usb_cdc_protocol_config_t s_config;
static TaskHandle_t s_worker = NULL;
static usb_cdc_session_gate_t s_session_gate;
static volatile bool s_connected = false;
static volatile bool s_reset_line = false;
static bool s_initialized = false;
static bool s_started = false;
static bool s_open_requested = false;

static bool cdc_tx_is_connected(void *ctx) {
    (void)ctx;
    return s_connected;
}

static bool cdc_tx_session_is_current(void *ctx,
                                      uint32_t response_generation) {
    usb_cdc_session_gate_t *gate = ctx;
    usb_cdc_session_snapshot_t response_snapshot = {
        .generation = response_generation,
        .admitted = true,
    };
    return usb_cdc_session_gate_snapshot_is_current(gate, response_snapshot);
}

static size_t cdc_tx_queue(void *ctx, const uint8_t *data, size_t len) {
    (void)ctx;
    return tinyusb_cdcacm_write_queue(TINYUSB_CDC_ACM_0, data, len);
}

static size_t cdc_tx_queue_char(void *ctx, uint8_t value) {
    (void)ctx;
    return tinyusb_cdcacm_write_queue_char(TINYUSB_CDC_ACM_0, (char)value);
}

static bool cdc_tx_flush(void *ctx, uint32_t timeout_ms) {
    (void)ctx;
    return tinyusb_cdcacm_write_flush(TINYUSB_CDC_ACM_0,
                                      pdMS_TO_TICKS(timeout_ms)) == ESP_OK;
}

static void cdc_tx_wait(void *ctx) {
    (void)ctx;
    vTaskDelay(1);
}

static bool cdc_write_response(const char *response, size_t len,
                               uint32_t response_generation) {
    usb_cdc_tx_ops_t ops = {
        .transport_ctx = NULL,
        .session_ctx = &s_session_gate,
        .is_connected = cdc_tx_is_connected,
        .session_is_current = cdc_tx_session_is_current,
        .queue = cdc_tx_queue,
        .queue_char = cdc_tx_queue_char,
        .flush = cdc_tx_flush,
        .wait = cdc_tx_wait,
        .retry_flush_timeout_ms = 20u,
        .final_flush_timeout_ms = 250u,
    };
    return usb_cdc_tx_write_response(&ops, response, len, response_generation);
}

static void usb_cdc_rx_callback(int itf, cdcacm_event_t *event) {
    (void)itf;
    (void)event;
    s_connected = true;
    usb_cdc_session_gate_note_rx(&s_session_gate);
    if (s_worker != NULL) {
        xTaskNotifyGive(s_worker);
    }
}

static void usb_cdc_line_state_callback(int itf, cdcacm_event_t *event) {
    (void)itf;
    if (event == NULL) {
        return;
    }
    s_connected = event->line_state_changed_data.dtr ||
                  event->line_state_changed_data.rts;
    s_reset_line = true;

    // DTR/RTS is transport hygiene only. It never calls release_accept and
    // never opens lifecycle admission.
    if (s_worker != NULL) {
        xTaskNotifyGive(s_worker);
    }
}

static void cdc_session_gate_wait(void *ctx) {
    (void)ctx;
    vTaskDelay(1);
}

static bool cdc_lifecycle_admission_open(void) {
    return s_config.command_admission_open != NULL &&
           s_config.command_admission_open(s_config.lifecycle_ctx);
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
            usb_cdc_session_snapshot_t batch_snapshot;
            uint32_t discard_epoch;

            batch_snapshot = usb_cdc_session_gate_snapshot(&s_session_gate);
            discard_epoch =
                usb_cdc_session_gate_rx_discard_epoch(&s_session_gate);
            got = 0u;
            if (tinyusb_cdcacm_read(TINYUSB_CDC_ACM_0, chunk, sizeof(chunk),
                                    &got) != ESP_OK) {
                break;
            }
            if (got == 0u) {
                usb_cdc_session_gate_mark_rx_drained(&s_session_gate,
                                                     discard_epoch);
                break;
            }

            if (s_reset_line) {
                usb_cdc_protocol_framer_reset(&framer);
                s_reset_line = false;
            }
            if (!s_connected ||
                !usb_cdc_session_gate_snapshot_is_current(&s_session_gate,
                                                          batch_snapshot) ||
                usb_cdc_session_gate_should_discard_rx(&s_session_gate) ||
                !cdc_lifecycle_admission_open()) {
                usb_cdc_protocol_framer_reset(&framer);
                continue;
            }

            for (i = 0u; i < got; ++i) {
                const uint8_t *frame_line = NULL;
                size_t frame_len = 0u;
                usb_cdc_protocol_frame_result_t frame_result;
                usb_cdc_protocol_effect_t effect;
                char response[CDC_RESPONSE_BYTES];
                int response_len;
                bool response_complete = false;

                if (s_reset_line || !s_connected ||
                    !usb_cdc_session_gate_snapshot_is_current(
                        &s_session_gate, batch_snapshot) ||
                    !cdc_lifecycle_admission_open()) {
                    usb_cdc_protocol_framer_reset(&framer);
                    s_reset_line = false;
                    break;
                }

                if (framer.line_len == 0u && !framer.oversized) {
                    frame_generation = batch_snapshot.generation;
                }
                frame_result = usb_cdc_protocol_framer_feed(
                    &framer, chunk[i], &frame_line, &frame_len);
                if (frame_result == USB_CDC_PROTOCOL_FRAME_NONE) {
                    continue;
                }

                if (!usb_cdc_session_gate_command_begin(&s_session_gate,
                                                        frame_generation)) {
                    continue;
                }

                memset(&effect, 0, sizeof(effect));
                response_len = usb_cdc_protocol_process_line(
                    frame_line, frame_len, response, sizeof(response),
                    &s_config, &effect);
                if (response_len > 0 && s_connected) {
                    response_complete = cdc_write_response(
                        response, (size_t)response_len, frame_generation);
                }

                // End the ordinary command before teardown may be signalled.
                // The accepted ownership gate remains closed independently.
                usb_cdc_session_gate_command_end(&s_session_gate);

                if (effect.release_accepted && response_complete &&
                    s_config.release_response_complete != NULL) {
                    (void)s_config.release_response_complete(
                        effect.release_attempt_id, s_config.lifecycle_ctx);
                }

                // Once RELEASE_STORAGE is accepted, no later SET_TIME or other
                // command can execute in this publication session.
                if (!cdc_lifecycle_admission_open()) {
                    usb_cdc_protocol_framer_reset(&framer);
                    break;
                }
            }
        } while (got != 0u);
    }
}

void usb_cdc_protocol_reset_session(void) {
    s_open_requested = false;
    s_connected = false;
    s_reset_line = true;
    usb_cdc_session_gate_reset(&s_session_gate, cdc_session_gate_wait, NULL);
    usb_cdc_session_gate_note_rx(&s_session_gate);
    if (s_worker != NULL) {
        xTaskNotifyGive(s_worker);
    }
}

void usb_cdc_protocol_open_session(void) {
    s_open_requested = true;
    s_reset_line = true;
    if (s_worker == NULL) {
        return;
    }

    xTaskNotifyGive(s_worker);
    while (s_open_requested &&
           !usb_cdc_session_gate_open(&s_session_gate)) {
        xTaskNotifyGive(s_worker);
        vTaskDelay(1);
    }
}

esp_err_t usb_cdc_protocol_init(const usb_cdc_protocol_config_t *config) {
    if (config == NULL || config->device_id == NULL ||
        !device_identity_is_valid_uuid(config->device_id) ||
        config->status_provider == NULL || config->set_time == NULL ||
        config->release_accept == NULL ||
        config->release_response_complete == NULL ||
        config->command_admission_open == NULL) {
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
    bool open_requested;

    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_started) {
        return ESP_OK;
    }
    if (xTaskCreate(usb_cdc_worker_task, "cdc_json", CDC_WORKER_STACK_BYTES,
                    NULL, 3, &s_worker) != pdPASS) {
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

    // HOST_OWNED can race ahead of this late interface start.
    open_requested = s_open_requested;
    if (open_requested) {
        usb_cdc_protocol_open_session();
    }
    return ESP_OK;
}

#else

esp_err_t usb_cdc_protocol_init(const usb_cdc_protocol_config_t *config) {
    (void)config;
    return ESP_ERR_NOT_SUPPORTED;
}
void usb_cdc_protocol_reset_session(void) {}
void usb_cdc_protocol_open_session(void) {}
esp_err_t usb_cdc_protocol_start(void) { return ESP_ERR_NOT_SUPPORTED; }

#endif
