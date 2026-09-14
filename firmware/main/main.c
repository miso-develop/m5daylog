#include <inttypes.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

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
#include "wav_rotation.h"

static const char *TAG = "m5daylog";

// Tasks #44/#45: SD mount -> PDM capture + SD writer -> `.wav.part`
// with Task #45 rotation/finalize to `.wav`.
//
// Bounded producer/consumer over the 32KB x 2 ping-pong pipeline:
// - recorder_capture_task (priority 5) owns the PDM handle and DMA scratch.
//   It never blocks on the pipeline: when both slots are full the payload
//   is dropped and counted as software buffer loss (fail-loud, never
//   silently overwritten). Proven driver DMA loss arrives only from the
//   driver's RX queue-overflow callback and is drained exactly (event count
//   + byte sizes) on EVERY read — including timeouts, zero-byte reads,
//   STOP, and fatal reads — plus a final drain before deinit; read timeouts
//   count as stalls, never as fabricated drops.
// - recorder_writer_task (priority 4) owns the current segment sink. It
//   drains full slots; each slow SD write runs WITHOUT the pipeline lock
//   so capture keeps filling the other slot — long SD writes are exactly
//   what the second slot absorbs. Task #45 rotation (30min / midnight)
//   file ops (close + rename + new open) also run WITHOUT the pipeline
//   lock and WITHOUT stopping capture, so the other slot absorbs the
//   switch and the unintended rotation gap stays within <=100ms.
// - Cross-task lifecycle uses ONE app-lifetime event group, never a
//   published TaskHandle: REC_BIT_SLOT_FULL wakes the writer,
//   REC_BIT_WRITER_READY is the startup handshake (capture never starts
//   the mic until mount + directories + sink are ready, independent of
//   task creation order or SMP scheduling), and sticky REC_BIT_STOP moves
//   both tasks to teardown. STOP covers every terminal finalize family:
//   USB connection, low battery, and safe-stop request all funnel through
//   the same idempotent finalize (no double close, no double rename).
//   No handle is ever signalled after its task is deleted, because no
//   handle is published at all.
// - s_rec_lock guards the shared pipeline only. After a blocking I2S read
//   returns, capture re-checks STOP before producing, so no audio is
//   enqueued after the sink has failed or closed.
//
// Board pins and SD bus come from recorder_config.h (M5Capsule v1.1
// baseline: PDM CLK 40 / DAT 41, SD SPI CS 11 MOSI 12 CLK 14 MISO 39,
// mount /sdcard). Build flags may override them; any bring-up failure
// (events, lock, mount, mic init, `.part` open, I2S/SD I/O, rotation
// path/dir/open) enters ERROR, never silent recording. Date
// subdirectories (`recordings/YYYY-MM-DD/`) are Task #45 scope; later
// recovery/retention bookkeeping remains out of scope.
#ifndef RECORDER_PART_PATH
#define RECORDER_PART_PATH \
    "/sdcard/M5DAYLOG/recordings/000000_pending.wav.part"
#endif
// NOTE: the default path above is a build-time fallback with the Spec #36
// `HHMMSS_<recordingId>.wav.part` shape. Task #45 runtime naming builds
// `recordings/YYYY-MM-DD/HHMMSS_<recordingId>.wav.part` from the wall
// clock plus a per-boot segment counter (opaque recording-id placeholder;
// full identity is a later Task). Midnight never mixes dates: the date
// directory switches with the new file. Only the `.wav.part` suffix is
// ever opened; PC syncs only the finalized `.wav`.

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

// Task #45 terminal-stop entry points. USB ownership (later Task),
// low-battery monitor (later Task), and safe-stop requests all share one
// sticky STOP plus one idempotent finalize: simultaneous USB +
// low-battery + stop duplicates cause a single close and a single rename
// (no double close, no double rename). Detection itself stays in later
// Tasks; these wrappers are the finalize contract they call.
static void recorder_request_safe_stop(void) {
    recorder_request_stop();
}

static void recorder_request_usb_stop(void) {
    recorder_request_stop();
}

static void recorder_request_low_battery_stop(void) {
    recorder_request_stop();
}

// Reference the terminal-stop wrappers so the finalize contract stays
// wired for later USB/state tasks without unused-function warnings.
// All three share the sticky STOP + idempotent finalize above.
static void recorder_reference_stop_wrappers(void) {
    (void)recorder_request_safe_stop;
    (void)recorder_request_usb_stop;
    (void)recorder_request_low_battery_stop;
}

// Wall-clock date/time for Task #45 segment naming. Fills
// date_out "YYYY-MM-DD" (RECORDER_DATE_STR_LEN == 11) and time_out
// "HHMMSS" (RECORDER_TIME_STR_LEN == 7). Fixed-size outputs are formatted
// digit-by-digit with range-checked inputs so no snprintf truncation is
// possible under -Werror=format-truncation. Before the wall clock is
// plausible (RTC not yet corrected via later USB correction), falls back
// to a stable epoch file so recording never blocks on time: the later
// correction changes the date and triggers a midnight rotation into the
// correct date directory (dates are never mixed into one file).
static void recorder_current_date_time(char date_out[RECORDER_DATE_STR_LEN],
                                       char time_out[RECORDER_TIME_STR_LEN]) {
    time_t now = time(NULL);
    struct tm tm_now;
    int year;
    int mon;
    int mday;
    int hour;
    int min;
    int sec;
    if (now < (time_t)1577836800L) {
        memcpy(date_out, "1970-01-01", RECORDER_DATE_STR_LEN);
        memcpy(time_out, "000000", RECORDER_TIME_STR_LEN);
        return;
    }
    memset(&tm_now, 0, sizeof(tm_now));
    localtime_r(&now, &tm_now);
    year = tm_now.tm_year + 1900;
    mon = tm_now.tm_mon + 1;
    mday = tm_now.tm_mday;
    hour = tm_now.tm_hour;
    min = tm_now.tm_min;
    sec = tm_now.tm_sec;
    if (year < 2020 || year > 9999 || mon < 1 || mon > 12 || mday < 1 ||
        mday > 31 || hour < 0 || hour > 23 || min < 0 || min > 59 || sec < 0 ||
        sec > 60) {
        memcpy(date_out, "1970-01-01", RECORDER_DATE_STR_LEN);
        memcpy(time_out, "000000", RECORDER_TIME_STR_LEN);
        return;
    }
    date_out[0] = (char)('0' + (year / 1000) % 10);
    date_out[1] = (char)('0' + (year / 100) % 10);
    date_out[2] = (char)('0' + (year / 10) % 10);
    date_out[3] = (char)('0' + year % 10);
    date_out[4] = '-';
    date_out[5] = (char)('0' + (mon / 10) % 10);
    date_out[6] = (char)('0' + mon % 10);
    date_out[7] = '-';
    date_out[8] = (char)('0' + (mday / 10) % 10);
    date_out[9] = (char)('0' + mday % 10);
    date_out[10] = '\0';
    time_out[0] = (char)('0' + (hour / 10) % 10);
    time_out[1] = (char)('0' + hour % 10);
    time_out[2] = (char)('0' + (min / 10) % 10);
    time_out[3] = (char)('0' + min % 10);
    time_out[4] = (char)('0' + (sec / 10) % 10);
    time_out[5] = (char)('0' + sec % 10);
    time_out[6] = '\0';
}

static const char *recorder_rotate_reason_str(wav_rotate_reason_t reason) {
    switch (reason) {
        case WAV_ROTATE_TIME_30MIN:
            return "time-30min";
        case WAV_ROTATE_MIDNIGHT:
            return "midnight";
        case WAV_ROTATE_USB:
            return "usb";
        case WAV_ROTATE_LOW_BATTERY:
            return "low-battery";
        case WAV_ROTATE_STOP_REQUEST:
            return "stop-request";
        default:
            return "none";
    }
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
    // and priority decide nothing under FreeRTOS SMP. STOP is sticky and is
    // re-checked explicitly: if STOP is set the mic is never started, even
    // when WRITER_READY also happens to be set.
    bits = xEventGroupWaitBits(s_rec_events,
                               REC_BIT_WRITER_READY | REC_BIT_STOP,
                               pdFALSE, pdFALSE, portMAX_DELAY);
    if ((bits & REC_BIT_STOP) != 0) {
        vTaskDelete(NULL);
        return;
    }
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
        pdm_overflow_snapshot_t snap = { 0, 0 };
        bool snap_pending = false;
        bool full = false;
        esp_err_t err = pdm_capture_read(capture, s_dma_scratch,
                                         sizeof(s_dma_scratch), &got);
        // Always preserve driver evidence first: a timeout, zero-byte read,
        // STOP after the blocking read, or fatal read must not discard
        // already-fired overflow callbacks.
        pdm_capture_drain_overflow(capture, &snap);
        snap_pending =
            (snap.events != 0 || snap.drop_bytes != 0);
        if (snap_pending) {
            ESP_LOGW(TAG,
                     "stage: record, result: dma overrun, events: %" PRIu32
                     ", bytes: %" PRIu32,
                     (uint32_t)snap.events,
                     (uint32_t)snap.drop_bytes);
        }
        if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
            // Fatal transport error: account the drained overflow first,
            // then exit without producing.
            if (snap_pending &&
                xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
                pcm_pipeline_note_driver_overflow(
                    &s_pipeline, snap.events, snap.drop_bytes);
                xSemaphoreGive(s_rec_lock);
            }
            ESP_LOGE(TAG, "stage: record, result: error, reason: i2s read");
            break;
        }
        // Re-check AFTER the blocking read: the sink may have failed or
        // closed while blocked — account overflow/stall but never enqueue
        // past a dead sink. Stall rule (exact): timeout OR ESP_OK short
        // read counts ONLY when this same read carries no driver overflow
        // evidence (!snap_pending); overflow and stall are never
        // double-classified, and neither fabricates dropped samples.
        if (recorder_stop_requested()) {
            bool timeout = (err == ESP_ERR_TIMEOUT);
            bool short_read =
                (err == ESP_OK && got < sizeof(s_dma_scratch));
            if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
                if ((timeout || short_read) && !snap_pending) {
                    pcm_pipeline_note_read_stall(&s_pipeline);
                }
                if (snap_pending) {
                    pcm_pipeline_note_driver_overflow(
                        &s_pipeline, snap.events, snap.drop_bytes);
                }
                xSemaphoreGive(s_rec_lock);
            }
            break;
        }
        if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
            bool timeout = (err == ESP_ERR_TIMEOUT);
            bool short_read =
                (err == ESP_OK && got < sizeof(s_dma_scratch));
            if ((timeout || short_read) && !snap_pending) {
                // Stall evidence only: no overflow event, no loss claim.
                pcm_pipeline_note_read_stall(&s_pipeline);
            }
            if (snap_pending) {
                pcm_pipeline_note_driver_overflow(
                    &s_pipeline, snap.events, snap.drop_bytes);
            }
            if (got > 0) {
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

    // Quiescent teardown: disable the RX channel first so no further
    // on_recv_q_ovf callback can fire after the final drain, then account
    // the final snapshot, then delete the channel. This closes the
    // drain-while-running race (drain -> disable inside deinit). A failed
    // disable is fail-loud: the zeroed snapshot is NOT a quiescent final
    // drain, RX remains active (retryable), and only interim evidence
    // drained below is accounted before deinit retries disable.
    {
        pdm_overflow_snapshot_t snap = { 0, 0 };
        esp_err_t stop_err =
            pdm_capture_stop_and_drain_final(capture, &snap);
        if (stop_err != ESP_OK) {
            pdm_overflow_snapshot_t interim = { 0, 0 };
            ESP_LOGE(TAG, "stage: record, result: error, reason: i2s stop");
            // Interim (non-final) drain while RX remains active: preserve
            // what has arrived so far without claiming quiescence.
            pdm_capture_drain_overflow(capture, &interim);
            if ((interim.events != 0 || interim.drop_bytes != 0) &&
                xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
                pcm_pipeline_note_driver_overflow(
                    &s_pipeline, interim.events, interim.drop_bytes);
                xSemaphoreGive(s_rec_lock);
            }
        } else if ((snap.events != 0 || snap.drop_bytes != 0) &&
                   xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
            pcm_pipeline_note_driver_overflow(
                &s_pipeline, snap.events, snap.drop_bytes);
            xSemaphoreGive(s_rec_lock);
        }
    }
    // Fail-closed deinit with owner-task ownership: the capture task retains
    // `capture` until pdm_capture_deinit() succeeds, so a failing deinit
    // can never fall through to capture=NULL/task deletion with an active
    // channel. STOP stays asserted while cleanup is pending; the loop yields
    // and stays fail-loud with bounded logging (no tight loop, no spam).
    recorder_request_stop();
    {
        esp_err_t deinit_err = pdm_capture_deinit(capture);
        unsigned attempt = 0;
        while (deinit_err != ESP_OK) {
            if ((attempt % 20u) == 0u) {
                ESP_LOGE(TAG,
                         "stage: record, result: error, reason: pdm deinit");
            }
            vTaskDelay(pdMS_TO_TICKS(100));
            attempt++;
            deinit_err = pdm_capture_deinit(capture);
        }
    }
    capture = NULL;
    recorder_request_stop();
    vTaskDelete(NULL);
}

static void recorder_log_diagnostics(void) {
    uint32_t captured = 0;
    uint32_t written = 0;
    uint32_t overflow = 0;
    uint32_t dma_drop = 0;
    uint32_t buf_drop = 0;
    uint32_t dma_bytes = 0;
    uint32_t buf_bytes = 0;
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
        dma_drop = c->dma_drop_samples;
        buf_drop = c->buffer_drop_samples;
        dma_bytes = c->dma_drop_bytes;
        buf_bytes = c->buffer_drop_bytes;
        overrun = c->dma_overrun_events;
        stalls = c->dma_read_stalls;
        sd_err = c->sd_write_errors;
        max_lat = c->max_sd_latency_us;
        xSemaphoreGive(s_rec_lock);
    }
    // Driver DMA loss (dma_*) and software buffer loss (buf_*) are reported
    // separately; buffer_overflow and sd_err are preserved verbatim.
    ESP_LOGI(TAG,
             "stage: record, result: running, captured: %" PRIu32
             ", written: %" PRIu32 ", overflow: %" PRIu32
             ", dma_drop: %" PRIu32 ", buf_drop: %" PRIu32
             ", dma_bytes: %" PRIu32 ", buf_bytes: %" PRIu32
             ", overrun: %" PRIu32
             ", stall: %" PRIu32 ", sd_err: %" PRIu32
             ", max_lat: %" PRIu32,
             captured, written, overflow, dma_drop, buf_drop, dma_bytes,
             buf_bytes, overrun, stalls, sd_err, max_lat);
}

static void recorder_writer_task(void *arg) {
    uint32_t since_flush = 0;
    bool running = true;
    // Task #45 rotation segment tracking (writer-owned, no lock needed:
    // only this task touches these locals). Capture keeps producing into
    // the pipeline across a rotation; only terminal STOP tears capture
    // down. seg_bytes counts payload bytes of the CURRENT segment for the
    // 30-minute size trigger (equivalent to 1800s at 32000 B/s).
    char seg_date[RECORDER_DATE_STR_LEN];
    char seg_part[RECORDER_MAX_PATH_LEN];
    char seg_wav[RECORDER_MAX_PATH_LEN];
    char cur_time[RECORDER_TIME_STR_LEN];
    char rec_id[16];
    uint32_t seg_seq = 0;
    uint32_t seg_bytes = 0;
    TickType_t seg_start_tick = 0;
    wav_rotation_state_t seg_state;

    (void)arg;
    if (sd_mount_recordings() != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: sd mount");
        recorder_request_stop();
        vTaskDelete(NULL);
        return;
    }
    // Initial segment: runtime date/time naming with the Spec #36
    // `HHMMSS_<recordingId>.wav.part` shape inside the date directory.
    // The recording-id field is a per-boot segment counter placeholder
    // (opaque, no private content); full identity arrives in a later Task.
    recorder_current_date_time(seg_date, cur_time);
    snprintf(rec_id, sizeof(rec_id), "s%04u", (unsigned)(seg_seq & 0xFFFFu));
    if (!wav_rotation_build_part_path(seg_part, sizeof(seg_part), seg_date,
                                      cur_time, rec_id) ||
        !wav_rotation_build_wav_path(seg_wav, sizeof(seg_wav), seg_date,
                                     cur_time, rec_id)) {
        ESP_LOGE(TAG, "stage: rotate, result: error, reason: rotate path");
        sd_mount_unmount();
        recorder_request_stop();
        vTaskDelete(NULL);
        return;
    }
    if (sd_mount_ensure_date_dir(seg_date) != ESP_OK) {
        ESP_LOGE(TAG, "stage: rotate, result: error, reason: mkdir date");
        sd_mount_unmount();
        recorder_request_stop();
        vTaskDelete(NULL);
        return;
    }
    if (!sd_pcm_sink_open(&s_sink, seg_part)) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: part open");
        sd_mount_unmount();
        recorder_request_stop();
        vTaskDelete(NULL);
        return;
    }
    wav_rotation_state_init(&seg_state, seg_part, seg_wav);
    seg_start_tick = xTaskGetTickCount();
    ESP_LOGI(TAG,
             "stage: record, result: capturing, path_suffix: .wav.part");
    // Handshake: only now may capture start the microphone.
    xEventGroupSetBits(s_rec_events, REC_BIT_WRITER_READY);

    while (running) {
        // Wake on a new full slot; the 1s bound means a lost wakeup can
        // never wedge the writer and gives the 30min/midnight poll a
        // bounded latency. STOP is sticky and checked separately so
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
            seg_bytes += (uint32_t)full_len;
            since_flush++;
            if (since_flush >= RECORDER_FLUSH_EVERY_CHUNKS) {
                since_flush = 0;
                // Keep the `.wav.part` header patched so it stays
                // decodeable. Later power-loss handling rebuilds from the
                // payload length; this only keeps the in-progress file
                // self-describing.
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
            // full slot observed before the stop flag. Terminal finalize
            // below (USB / low battery / safe-stop) is idempotent.
            running = false;
            break;
        }
        // Task #45 periodic rotation poll (30min elapsed/size, midnight
        // date change). Runs WITHOUT the pipeline lock and WITHOUT
        // stopping capture: the other 32KB slot absorbs the file switch
        // so the unintended gap stays <=100ms. A concurrent STOP wins:
        // rotation is skipped and the terminal finalize path runs once.
        {
            char now_date[RECORDER_DATE_STR_LEN];
            char now_time[RECORDER_TIME_STR_LEN];
            char new_part[RECORDER_MAX_PATH_LEN];
            char new_wav[RECORDER_MAX_PATH_LEN];
            uint32_t elapsed_sec;
            TickType_t now_tick;
            bool date_changed = false;
            wav_rotate_events_t ev = { false, false, false };
            wav_rotate_reason_t reason = WAV_ROTATE_NONE;

            recorder_current_date_time(now_date, now_time);
            now_tick = xTaskGetTickCount();
            elapsed_sec =
                (uint32_t)((now_tick - seg_start_tick) / configTICK_RATE_HZ);
            date_changed = (strcmp(now_date, seg_date) != 0);
            reason = wav_rotation_should_rotate(elapsed_sec, seg_bytes,
                                                date_changed, &ev);
            if (reason == WAV_ROTATE_USB ||
                reason == WAV_ROTATE_LOW_BATTERY ||
                reason == WAV_ROTATE_STOP_REQUEST) {
                // Should not happen here (STOP checked above); treat as
                // terminal stop without opening a new file.
                running = false;
                break;
            }
            if (reason == WAV_ROTATE_TIME_30MIN ||
                reason == WAV_ROTATE_MIDNIGHT) {
                // Idempotent finalize of the old segment: header finalize
                // (patch + media sync) then rename `.wav.part` -> `.wav`.
                // First call does I/O; duplicates return cached result
                // with no double close and no double rename.
                if (!wav_rotation_finalize_once(&seg_state, &s_sink)) {
                    ESP_LOGE(TAG,
                             "stage: rotate, result: error, reason: finalize");
                    if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) ==
                        pdTRUE) {
                        pcm_pipeline_note_sd_error(&s_pipeline);
                        xSemaphoreGive(s_rec_lock);
                    }
                    running = false;
                    recorder_request_stop();
                    break;
                }
                ESP_LOGI(TAG, "stage: rotate, result: ok, reason: %s",
                         recorder_rotate_reason_str(reason));
                // Open the next segment in the (possibly new) date
                // directory. Date directories never mix: midnight rotates
                // into the new date dir with a fresh file.
                seg_seq++;
                snprintf(rec_id, sizeof(rec_id), "s%04u",
                         (unsigned)(seg_seq & 0xFFFFu));
                if (!wav_rotation_build_part_path(new_part, sizeof(new_part),
                                                  now_date, now_time,
                                                  rec_id) ||
                    !wav_rotation_build_wav_path(new_wav, sizeof(new_wav),
                                                 now_date, now_time,
                                                 rec_id)) {
                    ESP_LOGE(TAG,
                             "stage: rotate, result: error, reason: rotate path");
                    running = false;
                    recorder_request_stop();
                    break;
                }
                if (sd_mount_ensure_date_dir(now_date) != ESP_OK) {
                    ESP_LOGE(TAG,
                             "stage: rotate, result: error, reason: mkdir date");
                    running = false;
                    recorder_request_stop();
                    break;
                }
                if (!sd_pcm_sink_open(&s_sink, new_part)) {
                    ESP_LOGE(TAG,
                             "stage: rotate, result: error, reason: part open");
                    running = false;
                    recorder_request_stop();
                    break;
                }
                memcpy(seg_part, new_part, sizeof(seg_part));
                memcpy(seg_wav, new_wav, sizeof(seg_wav));
                memcpy(seg_date, now_date, sizeof(seg_date));
                wav_rotation_state_init(&seg_state, seg_part, seg_wav);
                seg_bytes = 0;
                seg_start_tick = xTaskGetTickCount();
                since_flush = 0;
                ESP_LOGI(TAG,
                         "stage: record, result: capturing, path_suffix: .wav.part");
            }
        }
        if (recorder_stop_requested()) {
            running = false;
        }
    }

    // Terminal finalize (USB connection / low battery / safe-stop request
    // and any error teardown): header finalize then rename to `.wav`.
    // Idempotent: simultaneous stop duplicates return the cached result
    // with no double close and no double rename. PC syncs only `.wav`.
    if (!wav_rotation_finalize_once(&seg_state, &s_sink)) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: part close");
        if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
            pcm_pipeline_note_sd_error(&s_pipeline);
            xSemaphoreGive(s_rec_lock);
        }
    } else {
        ESP_LOGI(TAG, "stage: finalize, result: ok");
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

    // Tasks #44/#45 recording path. The event group is the only cross-task
    // channel (no published task handles); the writer-ready handshake makes
    // creation order irrelevant. Task #45 rotation/finalize is wired into
    // the writer task above; later recovery/retention/state tasks attach
    // without changing the capture contract.
    recorder_reference_stop_wrappers();
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
