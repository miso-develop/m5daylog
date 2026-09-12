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
#include "freertos/task.h"

#include "i2s_pdm_capture.h"
#include "pcm_pipeline.h"
#include "recorder_config.h"
#include "sd_mount.h"
#include "sd_pcm_sink.h"

static const char *TAG = "m5daylog";

// Task #44: SD mount -> PDM -> DMA -> SD continuous PCM capture.
//
// Board pins and SD bus come from recorder_config.h (M5Capsule v1.1
// baseline: PDM CLK 40 / DAT 41, SD SPI CS 11 MOSI 12 CLK 14 MISO 39,
// mount /sdcard). Build flags may override them; any bring-up failure
// (mount, mic init, `.part` open) enters ERROR, never silent recording.
// Only live-recording directories are created; rotation/finalize,
// recovery, and manifest/retention state belong to later Tasks.
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

static void recorder_task(void *arg) {
    pdm_capture_config_t cfg = {
        .i2s_port = 0,
        .pdm_clk_pin = RECORDER_PDM_CLK_PIN,
        .pdm_data_pin = RECORDER_PDM_DATA_PIN,
    };
    pcm_pipeline_t pipeline;
    sd_pcm_sink_t sink;
    pdm_capture_t capture = NULL;
    uint32_t loop = 0;

    (void)arg;
    pcm_pipeline_init(&pipeline, s_slot0, s_slot1);
    memset(&sink, 0, sizeof(sink));

    if (sd_mount_recordings() != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: sd mount");
        vTaskDelete(NULL);
        return;
    }
    if (pdm_capture_init(&cfg, &capture) != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: mic init");
        vTaskDelete(NULL);
        return;
    }
    if (!sd_pcm_sink_open(&sink, RECORDER_PART_PATH)) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: part open");
        pdm_capture_deinit(capture);
        sd_mount_unmount();
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "stage: record, result: capturing, path_suffix: .part");

    while (true) {
        size_t got = 0;
        bool overrun = false;
        size_t dropped;
        esp_err_t err = pdm_capture_read(capture, s_dma_scratch,
                                         sizeof(s_dma_scratch), &got,
                                         &overrun);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "stage: record, result: error, reason: i2s read");
            break;
        }
        if (overrun) {
            // DMA could not keep up: counted downstream as drop on top of
            // the pipeline overflow counters (fail-loud, never silent).
            ESP_LOGW(TAG, "stage: record, result: dma overrun");
        }
        dropped = pcm_pipeline_produce(&pipeline, s_dma_scratch, got);
        if (dropped != 0) {
            ESP_LOGW(TAG, "stage: record, result: buffer overflow");
        }

        while (pcm_pipeline_has_full(&pipeline)) {
            size_t full_len = 0;
            const uint8_t *full =
                pcm_pipeline_peek_full(&pipeline, &full_len);
            uint32_t latency_us = 0;

            if (full == NULL) {
                break;
            }
            if (!sd_pcm_sink_write_chunk(&sink, full, full_len,
                                         &latency_us)) {
                ESP_LOGE(TAG,
                         "stage: record, result: error, reason: sd write");
                pcm_pipeline_note_sd_error(&pipeline);
                goto stop;
            }
            pcm_pipeline_note_sd_write(&pipeline, latency_us);
            pcm_pipeline_release_full(&pipeline);
        }

        loop++;
        if ((loop % 8u) == 0) {
            // Keep the `.part` header patched so it stays decodeable.
            // Recovery across power loss is Task #46; this only keeps the
            // in-progress file self-describing.
            if (!sd_pcm_sink_flush(&sink)) {
                ESP_LOGE(TAG,
                         "stage: record, result: error, reason: part flush");
                break;
            }
            const recorder_counters_t *c =
                pcm_pipeline_counters(&pipeline);
            ESP_LOGI(TAG,
                     "stage: record, result: running, captured: %" PRIu32
                     ", written: %" PRIu32 ", overflow: %" PRIu32
                     ", drop: %" PRIu32 ", sd_err: %" PRIu32,
                     (uint32_t)c->samples_captured,
                     (uint32_t)c->chunks_written,
                     (uint32_t)c->buffer_overflow,
                     (uint32_t)c->dma_drop_samples,
                     (uint32_t)c->sd_write_errors);
        }
    }

stop:
    sd_pcm_sink_close(&sink);
    pdm_capture_deinit(capture);
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

    // Task #44 recording path. Rotation/finalize (#45), recovery (#46),
    // manifest (#47), and state machine (#48) attach in later Tasks.
    if (xTaskCreate(recorder_task, "recorder", 4096, NULL, 5, NULL) !=
        pdPASS) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: task spawn");
    }

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        ESP_LOGI(TAG, "stage: scaffold, result: idle");
    }
}
