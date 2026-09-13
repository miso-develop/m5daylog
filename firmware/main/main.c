#include <inttypes.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "esp_chip_info.h"
#include "esp_flash.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "i2s_pdm_capture.h"
#include "pcm_pipeline.h"
#include "recorder_config.h"
#include "sd_mount.h"
#include "sd_pcm_sink.h"

static const char *TAG = "m5daylog";

// Task #44: SD mount -> PDM capture task + SD writer task -> `.wav.part`.
//
// Bounded producer/consumer over the 32KB x 2 ping-pong pipeline:
// - recorder_capture_task (priority 5) owns the PDM handle and DMA scratch.
//   It never blocks on the pipeline: when both slots are full the payload
//   is dropped and counted (fail-loud, never silently overwritten). Proven
//   DMA loss arrives only from the driver's RX queue-overflow callback and
//   is drained exactly (event count + byte sizes); read timeouts count as
//   stalls, never as fabricated drops.
// - recorder_writer_task (priority 4) owns the `.wav.part` sink. It drains
//   full slots; each slow SD write runs WITHOUT the pipeline lock so
//   capture keeps filling the other slot — long SD writes are exactly what
//   the second slot absorbs.
// - Cross-task lifecycle uses ONE app-lifetime event group, never a
//   published TaskHandle: REC_BIT_SLOT_FULL wakes the writer,
//   REC_BIT_WRITER_READY is the startup handshake (capture never starts
//   the mic until mount + directories + sink are ready, independent of
//   task creation order or SMP scheduling), and sticky REC_BIT_STOP moves
//   both tasks to ERROR teardown. No handle is ever signalled after its
//   task is deleted, because no handle is published at all.
// - s_rec_lock guards the shared pipeline only. After a blocking I2S read
//   returns, capture re-checks STOP before producing, so no audio is
//   enqueued after the sink has failed or closed.
//
// Board pins and SD bus come from recorder_config.h (M5Capsule v1.1
// baseline: PDM CLK 40 / DAT 41, SD SPI CS 11 MOSI 12 CLK 14 MISO 39,
// mount /sdcard). Build flags may override them; any bring-up failure
// (events, lock, mount, mic init, `.part` open, I2S/SD I/O) enters ERROR,
// never silent recording. Only live-recording directories are created;
// rotation/finalize, recovery, and retention bookkeeping are later Tasks.
#ifndef RECORDER_PART_PATH
#define RECORDER_PART_PATH \
    "/sdcard/M5DAYLOG/recordings/000000_pending.wav.part"
#endif
// NOTE: the default path above is a build-time fallback with the Spec #36
// `HHMMSS_<recordingId>.wav.part` shape. Runtime naming (RTC timestamp +
// recording id) is provided by later Tasks; this Task only guarantees the
// `.wav.part` suffix and continuous append.

// 32KB x 2 staging in DRAM. 64KB static is within ESP32-S3 SRAM; a later
// Task may revisit placement (PSRAM) only via an explicit Spec update.
static uint8_t s_slot0[RECORDER_BUFFER_BYTES];
static uint8_t s_slot1[RECORDER_BUFFER_BYTES];
static uint8_t s_dma_scratch[4096];

static pcm_pipeline_t s_pipeline;
static sd_pcm_sink_t s_sink;
static SemaphoreHandle_t s_rec_lock = NULL;
static EventGroupHandle_t s_rec_events = NULL;

#define REC_BIT_SLOT_FULL (1u << 0)
#define REC_BIT_STOP (1u << 1)
#define REC_BIT_WRITER_READY (1u << 2)

// Flush the `.wav.part` header every N drained slots so the in-progress
// file stays decodeable (about every 4s of audio at 32KB/s).
#define RECORDER_FLUSH_EVERY_CHUNKS 4u

static void recorder_request_stop(void) {
    // STOP is sticky and also wakes the writer promptly for drain-then-exit.
    xEventGroupSetBits(s_rec_events, REC_BIT_STOP | REC_BIT_SLOT_FULL);
}

static bool recorder_stop_requested(void) {
    return (xEventGroupGetBits(s_rec_events) & REC_BIT_STOP) != 0;
}

static void recorder_capture_task(void *arg) {
    pdm_capture_config_t cfg = {
        .i2s_port = 0,
        .pdm_clk_pin = RECORDER_PDM_CLK_PIN,
        .pdm_data_pin = RECORDER_PDM_DATA_PIN,
    };
    pdm_capture_t capture = NULL;
    EventBits_t bits;

    (void)arg;
    // Startup handshake: the mic stays off until the writer has mount +
    // directories + open sink (or STOP on bring-up failure). Creation order
    // and priority decide nothing under FreeRTOS SMP.
    bits = xEventGroupWaitBits(s_rec_events,
                               REC_BIT_WRITER_READY | REC_BIT_STOP,
                               pdFALSE, pdFALSE, portMAX_DELAY);
    if ((bits & REC_BIT_WRITER_READY) == 0) {
        vTaskDelete(NULL);
        return;
    }
    if (pdm_capture_init(&cfg, &capture) != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: mic init");
        recorder_request_stop();
        vTaskDelete(NULL);
        return;
    }

    while (!recorder_stop_requested()) {
        size_t got = 0;
        esp_err_t err = pdm_capture_read(capture, s_dma_scratch,
                                         sizeof(s_dma_scratch), &got);
        if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
            ESP_LOGE(TAG, "stage: record, result: error, reason: i2s read");
            break;
        }
        // Re-check AFTER the blocking read: the sink may have failed or
        // closed while blocked — never enqueue past a dead sink.
        if (recorder_stop_requested()) {
            break;
        }
        bool full = false;
        if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
            if (err == ESP_ERR_TIMEOUT) {
                // Stall evidence only: no overflow event, no loss claim.
                pcm_pipeline_note_read_stall(&s_pipeline);
            }
            if (got > 0) {
                pdm_overflow_snapshot_t snap = { 0, 0 };
                pdm_capture_drain_overflow(capture, &snap);
                if (snap.events != 0 || snap.drop_bytes != 0) {
                    ESP_LOGW(TAG,
                             "stage: record, result: dma overrun, events: %" PRIu32
                             ", bytes: %" PRIu32,
                             (uint32_t)snap.events,
                             (uint32_t)snap.drop_bytes);
                    pcm_pipeline_note_driver_overflow(
                        &s_pipeline, snap.events, snap.drop_bytes);
                }
                size_t dropped = pcm_pipeline_produce(&s_pipeline,
                                                      s_dma_scratch, got);
                if (dropped != 0) {
                    ESP_LOGW(TAG, "stage: record, result: buffer overflow");
                }
            }
            full = pcm_pipeline_has_full(&s_pipeline);
            xSemaphoreGive(s_rec_lock);
        }
        if (full) {
            xEventGroupSetBits(s_rec_events, REC_BIT_SLOT_FULL);
        }
    }

    pdm_capture_deinit(capture);
    recorder_request_stop();
    vTaskDelete(NULL);
}

static void recorder_log_diagnostics(void) {
    uint32_t captured = 0;
    uint32_t written = 0;
    uint32_t overflow = 0;
    uint32_t drop = 0;
    uint32_t overrun = 0;
    uint32_t stalls = 0;
    uint32_t sd_err = 0;
    uint32_t max_lat = 0;

    if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
        const recorder_counters_t *c =
            pcm_pipeline_counters(&s_pipeline);
        captured = c->samples_captured;
        written = c->chunks_written;
        overflow = c->buffer_overflow;
        drop = c->dma_drop_samples;
        overrun = c->dma_overrun_events;
        stalls = c->dma_read_stalls;
        sd_err = c->sd_write_errors;
        max_lat = c->max_sd_latency_us;
        xSemaphoreGive(s_rec_lock);
    }
    ESP_LOGI(TAG,
             "stage: record, result: running, captured: %" PRIu32
             ", written: %" PRIu32 ", overflow: %" PRIu32
             ", drop: %" PRIu32 ", overrun: %" PRIu32
             ", stall: %" PRIu32 ", sd_err: %" PRIu32
             ", max_lat: %" PRIu32,
             captured, written, overflow, drop, overrun, stalls, sd_err,
             max_lat);
}

static void recorder_writer_task(void *arg) {
    uint32_t since_flush = 0;
    bool running = true;

    (void)arg;
    if (sd_mount_recordings() != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: sd mount");
        recorder_request_stop();
        vTaskDelete(NULL);
        return;
    }
    if (!sd_pcm_sink_open(&s_sink, RECORDER_PART_PATH)) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: part open");
        sd_mount_unmount();
        recorder_request_stop();
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG,
             "stage: record, result: capturing, path_suffix: .wav.part");
    // Handshake: only now may capture start the microphone.
    xEventGroupSetBits(s_rec_events, REC_BIT_WRITER_READY);

    while (running) {
        // Wake on a new full slot; the 1s bound means a lost wakeup can
        // never wedge the writer. STOP is sticky and checked separately so
        // it is never cleared by this wait.
        xEventGroupWaitBits(s_rec_events, REC_BIT_SLOT_FULL, pdTRUE,
                            pdFALSE, pdMS_TO_TICKS(1000));
        // Drain every full slot. Each slow SD write runs WITHOUT the
        // pipeline lock so capture keeps filling the other slot.
        while (running) {
            const uint8_t *full = NULL;
            size_t full_len = 0;
            uint32_t latency_us = 0;

            if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
                full = pcm_pipeline_peek_full(&s_pipeline, &full_len);
                xSemaphoreGive(s_rec_lock);
            }
            if (full == NULL) {
                break;
            }
            if (!sd_pcm_sink_write_chunk(&s_sink, full, full_len,
                                         &latency_us)) {
                ESP_LOGE(TAG,
                         "stage: record, result: error, reason: sd write");
                if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
                    pcm_pipeline_note_sd_error(&s_pipeline);
                    xSemaphoreGive(s_rec_lock);
                }
                running = false;
                recorder_request_stop();
                break;
            }
            if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
                pcm_pipeline_note_sd_write(&s_pipeline, latency_us);
                pcm_pipeline_release_full(&s_pipeline);
                xSemaphoreGive(s_rec_lock);
            }
            since_flush++;
            if (since_flush >= RECORDER_FLUSH_EVERY_CHUNKS) {
                since_flush = 0;
                // Keep the `.wav.part` header patched so it stays
                // decodeable. Recovery across power loss is Task #46; this
                // only keeps the in-progress file self-describing.
                if (!sd_pcm_sink_flush(&s_sink)) {
                    ESP_LOGE(TAG,
                             "stage: record, result: error, reason: part flush");
                    if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) ==
                        pdTRUE) {
                        pcm_pipeline_note_sd_error(&s_pipeline);
                        xSemaphoreGive(s_rec_lock);
                    }
                    running = false;
                    recorder_request_stop();
                    break;
                }
                recorder_log_diagnostics();
            }
        }
        if (recorder_stop_requested()) {
            // Drain-then-exit: the inner loop above already drained every
            // full slot observed before the stop flag.
            running = false;
        }
    }

    if (!sd_pcm_sink_close(&s_sink)) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: part close");
        if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
            pcm_pipeline_note_sd_error(&s_pipeline);
            xSemaphoreGive(s_rec_lock);
        }
    }
    sd_mount_unmount();
    ESP_LOGE(TAG, "stage: record, result: error, reason: stopped");
    vTaskDelete(NULL);
}

void app_main(void) {
    ESP_LOGI(TAG, "m5daylog firmware scaffold boot");

    esp_chip_info_t chip_info;
    esp_chip_info(&chip_info);
    ESP_LOGI(TAG, "chip cores: %d, revision: %d", chip_info.cores, chip_info.revision);

    uint32_t flash_size = 0;
    if (esp_flash_get_size(esp_flash_default_chip, &flash_size) == ESP_OK) {
        ESP_LOGI(TAG, "flash size: %" PRIu32 " MB", flash_size / (1024 * 1024));
    } else {
        ESP_LOGW(TAG, "flash size: unknown");
    }

    ESP_LOGI(TAG, "idf version: %s", esp_get_idf_version());
    ESP_LOGI(TAG, "stage: scaffold, result: boot ok");

    // Task #44 recording path. The event group is the only cross-task
    // channel (no published task handles); the writer-ready handshake makes
    // creation order irrelevant. Rotation/finalize (#45), recovery (#46),
    // manifest (#47), and state machine (#48) attach in later Tasks.
    pcm_pipeline_init(&s_pipeline, s_slot0, s_slot1);
    memset(&s_sink, 0, sizeof(s_sink));
    s_rec_events = xEventGroupCreate();
    if (s_rec_events == NULL) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: rec events");
    } else {
        s_rec_lock = xSemaphoreCreateMutex();
        if (s_rec_lock == NULL) {
            ESP_LOGE(TAG, "stage: record, result: error, reason: rec lock");
        } else if (xTaskCreate(recorder_writer_task, "rec_writer", 4096,
                               NULL, 4, NULL) != pdPASS) {
            ESP_LOGE(TAG, "stage: record, result: error, reason: task spawn");
        } else if (xTaskCreate(recorder_capture_task, "rec_capture", 4096,
                               NULL, 5, NULL) != pdPASS) {
            ESP_LOGE(TAG, "stage: record, result: error, reason: task spawn");
            recorder_request_stop();
        }
    }

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        ESP_LOGI(TAG, "stage: scaffold, result: idle");
    }
}
