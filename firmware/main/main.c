#include <inttypes.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "esp_chip_info.h"
#include "esp_flash.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "device_identity.h"
#include "device_manifest.h"
#include "esp_random.h"
#include "i2s_pdm_capture.h"
#include "pcm_pipeline.h"
#include "recorder_config.h"
#include "recorder_power.h"
#include "recorder_state.h"
#include "recorder_status_led.h"
#include "sd_mount.h"
#include "sd_pcm_sink.h"
#include "wav_recovery.h"
#include "wav_rotation.h"

static const char *TAG = "m5daylog";

// Tasks #44/#45/#46/#47/#48: SD mount -> RECOVER -> PDM capture + SD
// writer -> `.wav.part`, rotation/finalize, power-loss recovery,
// identity/manifest integrity, and an explicit fail-loud lifecycle.
// Task #48 makes SD/mic/DMA failures enter ERROR, safely finalizes on the
// configurable low-battery threshold, records state/failure transitions to
// events.jsonl, and keeps RGB dark during normal recording while making
// ERROR and LOW_BATTERY_STOP visibly distinct.
//
// Regression contract notes retained for the older source-contract tests:
// - WRITER_READY is the storage/sink startup handshake; capture re-checks
//   sticky STOP after waking and before pdm_capture_init. RECORDING_READY is
//   published only after mic init and the RECOVER -> RECORDING transition.
// - Slow SD and rotation file I/O run WITHOUT the pipeline lock so capture
//   keeps filling the other slot; the intended rotation-gap bound is <=100ms.
// - Always preserve driver overflow evidence on every read. The producer
//   gate remains conceptually `if (got > 0)`; proven DMA loss now prevents
//   enqueue and immediately moves the lifecycle to ERROR.
// - Quiescent teardown disables RX before the final overflow drain.
// - Terminal finalize is idempotent: no double close and no double rename.
// - Midnight date directories never mix, and PC syncs only finalized `.wav`.
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
// - recorder_battery_task (priority 3) starts only after RECORDING_READY. It
//   samples the calibrated M5Capsule VBAT ADC every configured interval;
//   consecutive low readings transition RECORDING -> LOW_BATTERY_STOP and
//   set the same sticky STOP used by the idempotent finalize path.
// - Cross-task lifecycle uses ONE app-lifetime event group, never a
//   published TaskHandle: REC_BIT_SLOT_FULL wakes the writer,
//   REC_BIT_WRITER_READY means mount + directories + sink are ready so
//   capture may initialize the mic, REC_BIT_RECORDING_READY means mic init
//   succeeded and RECOVER -> RECORDING was published, and sticky
//   REC_BIT_STOP moves tasks to teardown. No handle is ever signalled after
//   its task is deleted, because no task handle is published at all.
// - s_rec_lock guards the shared pipeline only. s_state_lock separately
//   serializes the explicit lifecycle plus state-event JSONL appends.
#ifndef RECORDER_PART_PATH
#define RECORDER_PART_PATH \
    "/sdcard/M5DAYLOG/recordings/000000_pending.wav.part"
#endif

static uint8_t s_slot0[RECORDER_BUFFER_BYTES];
static uint8_t s_slot1[RECORDER_BUFFER_BYTES];
static uint8_t s_dma_scratch[4096];

static pcm_pipeline_t s_pipeline;
static sd_pcm_sink_t s_sink;
static SemaphoreHandle_t s_rec_lock = NULL;
static SemaphoreHandle_t s_state_lock = NULL;
static EventGroupHandle_t s_rec_events = NULL;
static recorder_state_machine_t s_rec_state;
static bool s_state_events_ready = false;
static bool s_status_led_ready = false;
static int s_last_battery_mv = 0;

#define REC_BIT_SLOT_FULL (1u << 0)
#define REC_BIT_STOP (1u << 1)
#define REC_BIT_WRITER_READY (1u << 2)
#define REC_BIT_RECORDING_READY (1u << 3)

// Flush the `.wav.part` header every N drained slots so the in-progress
// file stays decodeable (about every 4s of audio at 32KB/s).
#define RECORDER_FLUSH_EVERY_CHUNKS 4u

// Writer stack budget (Task #45 hardware-gate fix for the boot-without-SD
// stack overflow + reboot loop). Path buffers remain writer-owned static
// storage so FATFS/VFS/newlib retain stack headroom.
#define RECORDER_WRITER_STACK_BYTES 6144

static char s_seg_part[RECORDER_MAX_PATH_LEN];
static char s_seg_wav[RECORDER_MAX_PATH_LEN];
static char s_rot_part[RECORDER_MAX_PATH_LEN];
static char s_rot_wav[RECORDER_MAX_PATH_LEN];
static wav_rotation_state_t s_seg_state;

static char s_device_id[RECORDER_UUID_STR_LEN];
static char s_manifest_entry[RECORDER_MANIFEST_ENTRY_MAX];
static char s_manifest_now[RECORDER_ISO8601_STR_LEN];
static char s_seg_rec_id[RECORDER_UUID_STR_LEN];
static char s_seg_started[RECORDER_ISO8601_STR_LEN];

static const char *const k_m5daylog_prefix = "/sdcard/M5DAYLOG/";

static void recorder_request_stop(void) {
    if (s_rec_events != NULL) {
        xEventGroupSetBits(s_rec_events, REC_BIT_STOP | REC_BIT_SLOT_FULL);
    }
}

static bool recorder_stop_requested(void) {
    if (s_rec_events == NULL) {
        return true;
    }
    return (xEventGroupGetBits(s_rec_events) & REC_BIT_STOP) != 0;
}

static void recorder_request_safe_stop(void) {
    recorder_request_stop();
}

static void recorder_request_usb_stop(void) {
    recorder_request_stop();
}

static void recorder_request_low_battery_stop(void) {
    recorder_request_stop();
}

static void recorder_reference_stop_wrappers(void) {
    (void)recorder_request_safe_stop;
    (void)recorder_request_usb_stop;
    (void)recorder_request_low_battery_stop;
}

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

static bool recorder_new_recording_id(char out[RECORDER_UUID_STR_LEN]) {
    uint8_t rand16[16];
    uint32_t w;
    int i;
    if (out == NULL) {
        return false;
    }
    memset(out, 0, RECORDER_UUID_STR_LEN);
    for (i = 0; i < 4; ++i) {
        w = esp_random();
        rand16[i * 4 + 0] = (uint8_t)(w >> 24);
        rand16[i * 4 + 1] = (uint8_t)(w >> 16);
        rand16[i * 4 + 2] = (uint8_t)(w >> 8);
        rand16[i * 4 + 3] = (uint8_t)(w);
    }
    if (!device_identity_format_uuid_v4(rand16, out)) {
        memset(out, 0, RECORDER_UUID_STR_LEN);
        return false;
    }
    memset(rand16, 0, sizeof(rand16));
    return true;
}

static bool recorder_current_iso8601(char out[RECORDER_ISO8601_STR_LEN]) {
    char date[RECORDER_DATE_STR_LEN];
    char time6[RECORDER_TIME_STR_LEN];
    int n;
    if (out == NULL) {
        return false;
    }
    memset(out, 0, RECORDER_ISO8601_STR_LEN);
    recorder_current_date_time(date, time6);
    n = snprintf(out, RECORDER_ISO8601_STR_LEN,
                 "%.4s-%.2s-%.2sT%.2s:%.2s:%.2s+00:00",
                 date, date + 5, date + 8, time6, time6 + 2, time6 + 4);
    if (n < 0 || (size_t)n >= RECORDER_ISO8601_STR_LEN ||
        !device_manifest_is_valid_iso8601_offset(out)) {
        memset(out, 0, RECORDER_ISO8601_STR_LEN);
        return false;
    }
    return true;
}

static void recorder_set_events_ready(bool ready) {
    if (s_state_lock != NULL &&
        xSemaphoreTake(s_state_lock, portMAX_DELAY) == pdTRUE) {
        s_state_events_ready = ready;
        xSemaphoreGive(s_state_lock);
    }
}

static recorder_state_t recorder_current_state(void) {
    recorder_state_t state = RECORDER_STATE_ERROR;
    if (s_state_lock != NULL &&
        xSemaphoreTake(s_state_lock, portMAX_DELAY) == pdTRUE) {
        state = s_rec_state.state;
        xSemaphoreGive(s_state_lock);
    }
    return state;
}

static bool recorder_transition_state(recorder_state_t next,
                                      recorder_reason_t reason,
                                      int battery_mv) {
    recorder_state_t from;
    recorder_state_machine_t candidate;
    bool changed;
    bool event_write_failed = false;
    bool ok;
    char timestamp[RECORDER_ISO8601_STR_LEN];
    if (s_state_lock == NULL ||
        xSemaphoreTake(s_state_lock, portMAX_DELAY) != pdTRUE) {
        return false;
    }
    from = s_rec_state.state;
    candidate = s_rec_state;
    changed = from != next;
    ok = recorder_state_transition(&candidate, next, reason);
    if (battery_mv > 0) {
        s_last_battery_mv = battery_mv;
    }
    if (ok && changed && s_state_events_ready) {
        memset(timestamp, 0, sizeof(timestamp));
        (void)recorder_current_iso8601(timestamp);
        if (!recorder_state_append_event(RECORDER_EVENTS_PATH, timestamp,
                                         from, next, reason,
                                         battery_mv > 0 ? battery_mv :
                                                          s_last_battery_mv)) {
            event_write_failed = true;
            (void)recorder_state_transition(&s_rec_state,
                                            RECORDER_STATE_ERROR,
                                            RECORDER_REASON_SD_WRITE);
        } else {
            s_rec_state = candidate;
        }
    } else if (ok) {
        s_rec_state = candidate;
    }
    xSemaphoreGive(s_state_lock);
    if (event_write_failed) {
        ESP_LOGE(TAG,
                 "stage: state, result: error, reason: event append sd-write");
        recorder_request_stop();
        if (s_status_led_ready &&
            recorder_status_led_set_state(RECORDER_STATE_ERROR) != ESP_OK) {
            ESP_LOGE(TAG,
                     "stage: state, result: error, reason: status led");
        }
        return false;
    }
    if (!ok) {
        ESP_LOGE(TAG,
                 "stage: state, result: error, reason: illegal transition, from: %s, to: %s",
                 recorder_state_str(from), recorder_state_str(next));
        return false;
    }
    if (changed) {
        ESP_LOGI(TAG, "stage: state, result: ok, from: %s, to: %s, reason: %s",
                 recorder_state_str(from), recorder_state_str(next),
                 recorder_reason_str(reason));
    }
    if (s_status_led_ready && changed) {
        if (recorder_status_led_set_state(next) != ESP_OK) {
            ESP_LOGE(TAG,
                     "stage: state, result: error, reason: status led");
        }
    }
    return true;
}

static void recorder_enter_error(recorder_reason_t reason) {
    (void)recorder_transition_state(RECORDER_STATE_ERROR, reason, 0);
    recorder_request_stop();
}

static bool recorder_manifest_record_wav(const char *wav_path,
                                         const char *rec_id,
                                         const char *started_at,
                                         const char *state) {
    char sha[RECORDER_SHA256_HEX_LEN];
    uint32_t size_bytes = 0;
    uint32_t pcm_bytes = 0;
    uint32_t duration_ms = 0;
    const char *rel;
    size_t prefix_len;
    memset(s_manifest_entry, 0, sizeof(s_manifest_entry));
    memset(s_manifest_now, 0, sizeof(s_manifest_now));
    memset(sha, 0, sizeof(sha));
    if (wav_path == NULL || rec_id == NULL || started_at == NULL ||
        state == NULL) {
        ESP_LOGE(TAG, "stage: manifest, result: error, reason: manifest arg");
        return false;
    }
    prefix_len = strlen(k_m5daylog_prefix);
    if (strncmp(wav_path, k_m5daylog_prefix, prefix_len) != 0) {
        ESP_LOGE(TAG, "stage: manifest, result: error, reason: manifest path");
        return false;
    }
    rel = wav_path + prefix_len;
    if (!recorder_current_iso8601(s_manifest_now)) {
        ESP_LOGE(TAG, "stage: manifest, result: error, reason: manifest time");
        return false;
    }
    if (!device_manifest_hash_wav_file(wav_path, sha, &size_bytes)) {
        ESP_LOGE(TAG, "stage: manifest, result: error, reason: manifest hash");
        return false;
    }
    if (size_bytes < RECORDER_WAV_HEADER_SIZE) {
        ESP_LOGE(TAG, "stage: manifest, result: error, reason: manifest size");
        return false;
    }
    pcm_bytes = size_bytes - RECORDER_WAV_HEADER_SIZE;
    pcm_bytes -= (pcm_bytes % RECORDER_BYTES_PER_SAMPLE);
    duration_ms = device_manifest_duration_ms(pcm_bytes);
    if (!device_manifest_build_entry(rec_id, rel, started_at, duration_ms,
                                     size_bytes, sha, RECORDER_SAMPLE_RATE_HZ,
                                     RECORDER_BITS_PER_SAMPLE,
                                     RECORDER_CHANNELS, state, NULL,
                                     s_manifest_entry,
                                     sizeof(s_manifest_entry))) {
        ESP_LOGE(TAG, "stage: manifest, result: error, reason: manifest entry");
        return false;
    }
    {
        device_manifest_result_t r = device_manifest_upsert_file(
            RECORDER_MANIFEST_PATH, RECORDER_MANIFEST_TMP_PATH, s_device_id,
            s_manifest_now, s_manifest_entry);
        if (r == DEVICE_MANIFEST_OK) {
            ESP_LOGI(TAG, "stage: manifest, result: ok");
            return true;
        }
        if (r == DEVICE_MANIFEST_CONFLICT) {
            ESP_LOGE(TAG,
                     "stage: manifest, result: error, reason: manifest conflict");
        } else {
            ESP_LOGE(TAG,
                     "stage: manifest, result: error, reason: manifest write");
        }
        return false;
    }
}

static bool recorder_ensure_manifest_exists(void) {
    FILE *probe = NULL;
    if (!recorder_current_iso8601(s_manifest_now)) {
        ESP_LOGE(TAG, "stage: manifest, result: error, reason: manifest time");
        return false;
    }
    probe = fopen(RECORDER_MANIFEST_PATH, "rb");
    if (probe != NULL) {
        fclose(probe);
        return true;
    }
    if (!device_manifest_recover_tmp(RECORDER_MANIFEST_PATH,
                                     RECORDER_MANIFEST_TMP_PATH,
                                     s_device_id)) {
        FILE *tmp_probe = fopen(RECORDER_MANIFEST_TMP_PATH, "rb");
        if (tmp_probe != NULL) {
            fclose(tmp_probe);
            ESP_LOGE(TAG,
                     "stage: manifest, result: error, reason: manifest tmp");
            return false;
        }
    } else {
        probe = fopen(RECORDER_MANIFEST_PATH, "rb");
        if (probe != NULL) {
            fclose(probe);
            return true;
        }
    }
    {
        char empty[256];
        FILE *out = NULL;
        size_t len;
        memset(empty, 0, sizeof(empty));
        if (!device_manifest_build_empty(s_device_id, s_manifest_now, empty,
                                         sizeof(empty))) {
            ESP_LOGE(TAG,
                     "stage: manifest, result: error, reason: manifest empty");
            return false;
        }
        len = strlen(empty);
        out = fopen(RECORDER_MANIFEST_TMP_PATH, "wb");
        if (out == NULL) {
            ESP_LOGE(TAG,
                     "stage: manifest, result: error, reason: manifest tmp open");
            return false;
        }
        if (fwrite(empty, 1, len, out) != len || fflush(out) != 0) {
            fclose(out);
            ESP_LOGE(TAG,
                     "stage: manifest, result: error, reason: manifest tmp write");
            return false;
        }
        {
            int fd = fileno(out);
            if (fd >= 0) {
                (void)fsync(fd);
            }
        }
        if (fclose(out) != 0) {
            ESP_LOGE(TAG,
                     "stage: manifest, result: error, reason: manifest tmp close");
            return false;
        }
        if (rename(RECORDER_MANIFEST_TMP_PATH, RECORDER_MANIFEST_PATH) != 0) {
            ESP_LOGE(TAG,
                     "stage: manifest, result: error, reason: manifest rename");
            return false;
        }
        ESP_LOGI(TAG, "stage: manifest, result: ok");
        return true;
    }
}

static void recorder_battery_task(void *arg) {
    EventBits_t bits;
    uint32_t low_samples = 0;
    (void)arg;
    bits = xEventGroupWaitBits(s_rec_events,
                               REC_BIT_RECORDING_READY | REC_BIT_STOP,
                               pdFALSE, pdFALSE, portMAX_DELAY);
    if ((bits & REC_BIT_STOP) != 0 ||
        (bits & REC_BIT_RECORDING_READY) == 0) {
        vTaskDelete(NULL);
        return;
    }
    if (recorder_power_init() != ESP_OK) {
        ESP_LOGE(TAG, "stage: battery, result: error, reason: adc init");
        recorder_enter_error(RECORDER_REASON_INTERNAL);
        vTaskDelete(NULL);
        return;
    }
    while (!recorder_stop_requested()) {
        int battery_mv = 0;
        if (recorder_power_read_battery_mv(&battery_mv) != ESP_OK) {
            ESP_LOGE(TAG, "stage: battery, result: error, reason: adc read");
            recorder_enter_error(RECORDER_REASON_INTERNAL);
            break;
        }
        if (battery_mv <= RECORDER_LOW_BATTERY_MV) {
            low_samples++;
        } else {
            low_samples = 0;
        }
        if (low_samples >= RECORDER_LOW_BATTERY_CONFIRM_SAMPLES) {
            ESP_LOGW(TAG,
                     "stage: battery, result: low, battery_mv: %d, threshold_mv: %d",
                     battery_mv, RECORDER_LOW_BATTERY_MV);
            if (!recorder_transition_state(RECORDER_STATE_LOW_BATTERY_STOP,
                                           RECORDER_REASON_LOW_BATTERY,
                                           battery_mv)) {
                recorder_enter_error(RECORDER_REASON_INTERNAL);
            } else {
                recorder_request_low_battery_stop();
            }
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(RECORDER_BATTERY_POLL_MS));
    }
    recorder_power_deinit();
    vTaskDelete(NULL);
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
        recorder_enter_error(RECORDER_REASON_MIC_INIT);
        vTaskDelete(NULL);
        return;
    }
    if (!recorder_transition_state(RECORDER_STATE_RECORDING,
                                   RECORDER_REASON_NONE, 0)) {
        recorder_enter_error(RECORDER_REASON_INTERNAL);
    } else {
        ESP_LOGI(TAG,
                 "stage: record, result: capturing, path_suffix: .wav.part");
        xEventGroupSetBits(s_rec_events, REC_BIT_RECORDING_READY);
    }

    while (!recorder_stop_requested()) {
        size_t got = 0;
        pdm_overflow_snapshot_t snap = { 0, 0 };
        bool snap_pending = false;
        bool buffer_loss = false;
        bool full = false;
        esp_err_t err = pdm_capture_read(capture, s_dma_scratch,
                                         sizeof(s_dma_scratch), &got);
        // Always preserve overflow evidence before classifying this read.
        pdm_capture_drain_overflow(capture, &snap);
        snap_pending = (snap.events != 0 || snap.drop_bytes != 0);
        if (snap_pending) {
            ESP_LOGW(TAG,
                     "stage: record, result: dma overrun, events: %" PRIu32
                     ", bytes: %" PRIu32,
                     (uint32_t)snap.events,
                     (uint32_t)snap.drop_bytes);
        }
        if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
            if (snap_pending &&
                xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
                pcm_pipeline_note_driver_overflow(
                    &s_pipeline, snap.events, snap.drop_bytes);
                xSemaphoreGive(s_rec_lock);
            }
            ESP_LOGE(TAG, "stage: record, result: error, reason: i2s read");
            recorder_enter_error(RECORDER_REASON_I2S_READ);
            break;
        }
        if (recorder_stop_requested()) {
            bool timeout = (err == ESP_ERR_TIMEOUT);
            bool short_read = (err == ESP_OK && got < sizeof(s_dma_scratch));
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
            if (snap_pending) {
                recorder_enter_error(RECORDER_REASON_DMA_OVERRUN);
            }
            break;
        }
        if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
            bool timeout = (err == ESP_ERR_TIMEOUT);
            bool short_read = (err == ESP_OK && got < sizeof(s_dma_scratch));
            if ((timeout || short_read) && !snap_pending) {
                pcm_pipeline_note_read_stall(&s_pipeline);
            }
            if (snap_pending) {
                pcm_pipeline_note_driver_overflow(
                    &s_pipeline, snap.events, snap.drop_bytes);
            }
            // `if (got > 0)` is the producer gate; Task #48 additionally
            // refuses to enqueue bytes from a read carrying proven DMA loss.
            if (got > 0) {
                if (!snap_pending) {
                    size_t dropped = pcm_pipeline_produce(&s_pipeline,
                                                          s_dma_scratch, got);
                    if (dropped != 0) {
                        ESP_LOGW(TAG,
                                 "stage: record, result: buffer overflow");
                        buffer_loss = true;
                    }
                }
            }
            full = pcm_pipeline_has_full(&s_pipeline);
            xSemaphoreGive(s_rec_lock);
        }
        if (snap_pending) {
            recorder_enter_error(RECORDER_REASON_DMA_OVERRUN);
            break;
        }
        if (buffer_loss) {
            recorder_enter_error(RECORDER_REASON_BUFFER_OVERFLOW);
            break;
        }
        if (full) {
            xEventGroupSetBits(s_rec_events, REC_BIT_SLOT_FULL);
        }
    }

    // Quiescent teardown: disable before the final driver-overflow drain so
    // no later callback can invalidate the final accounting snapshot.
    {
        pdm_overflow_snapshot_t snap = { 0, 0 };
        esp_err_t stop_err = pdm_capture_stop_and_drain_final(capture, &snap);
        if (stop_err != ESP_OK) {
            pdm_overflow_snapshot_t interim = { 0, 0 };
            ESP_LOGE(TAG, "stage: record, result: error, reason: i2s stop");
            pdm_capture_drain_overflow(capture, &interim);
            if ((interim.events != 0 || interim.drop_bytes != 0) &&
                xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
                pcm_pipeline_note_driver_overflow(
                    &s_pipeline, interim.events, interim.drop_bytes);
                xSemaphoreGive(s_rec_lock);
            }
            recorder_enter_error(RECORDER_REASON_I2S_READ);
        } else if ((snap.events != 0 || snap.drop_bytes != 0) &&
                   xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
            pcm_pipeline_note_driver_overflow(
                &s_pipeline, snap.events, snap.drop_bytes);
            xSemaphoreGive(s_rec_lock);
            recorder_enter_error(RECORDER_REASON_DMA_OVERRUN);
        }
    }
    recorder_request_stop();
    {
        esp_err_t deinit_err = pdm_capture_deinit(capture);
        unsigned attempt = 0;
        while (deinit_err != ESP_OK) {
            if ((attempt % 20u) == 0u) {
                ESP_LOGE(TAG,
                         "stage: record, result: error, reason: pdm deinit");
                recorder_enter_error(RECORDER_REASON_INTERNAL);
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
        const recorder_counters_t *c = pcm_pipeline_counters(&s_pipeline);
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

static void recorder_log_writer_stack_hw(const char *reason) {
    UBaseType_t hw = uxTaskGetStackHighWaterMark(NULL);
    ESP_LOGI(TAG, "stage: record, result: stack, writer_hw: %u, reason: %s",
             (unsigned)hw, reason);
}

static void recorder_writer_task(void *arg) {
    uint32_t since_flush = 0;
    bool running = true;
    char seg_date[RECORDER_DATE_STR_LEN];
    char cur_time[RECORDER_TIME_STR_LEN];
    char rec_id[RECORDER_UUID_STR_LEN];
    uint32_t seg_bytes = 0;
    TickType_t seg_start_tick = 0;

    (void)arg;
    if (sd_mount_recordings() != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: sd mount");
        recorder_enter_error(RECORDER_REASON_SD_MOUNT);
        recorder_log_writer_stack_hw("sd mount");
        vTaskDelete(NULL);
        return;
    }
    recorder_set_events_ready(true);
    if (!recorder_transition_state(RECORDER_STATE_RECOVER,
                                   RECORDER_REASON_RECOVERY, 0)) {
        recorder_enter_error(RECORDER_REASON_INTERNAL);
        recorder_set_events_ready(false);
        sd_mount_unmount();
        recorder_log_writer_stack_hw("state recover");
        vTaskDelete(NULL);
        return;
    }

    memset(s_device_id, 0, sizeof(s_device_id));
    if (device_identity_ensure_device_id(s_device_id) != ESP_OK) {
        ESP_LOGE(TAG, "stage: manifest, result: error, reason: device id");
        recorder_enter_error(RECORDER_REASON_MANIFEST);
        recorder_set_events_ready(false);
        sd_mount_unmount();
        recorder_log_writer_stack_hw("device id");
        vTaskDelete(NULL);
        return;
    }
    if (!device_identity_ensure_device_json(RECORDER_DEVICE_JSON_PATH,
                                            s_device_id, NULL, NULL)) {
        ESP_LOGE(TAG, "stage: manifest, result: error, reason: device json");
        recorder_enter_error(RECORDER_REASON_MANIFEST);
        recorder_set_events_ready(false);
        sd_mount_unmount();
        recorder_log_writer_stack_hw("device json");
        vTaskDelete(NULL);
        return;
    }
    if (!recorder_ensure_manifest_exists()) {
        ESP_LOGE(TAG, "stage: manifest, result: error, reason: manifest init");
        recorder_enter_error(RECORDER_REASON_MANIFEST);
        recorder_set_events_ready(false);
        sd_mount_unmount();
        recorder_log_writer_stack_hw("manifest init");
        vTaskDelete(NULL);
        return;
    }
    {
        wav_recovery_stats_t rec_stats;
        memset(&rec_stats, 0, sizeof(rec_stats));
        if (sd_mount_ensure_quarantine_dir() != ESP_OK) {
            ESP_LOGE(TAG,
                     "stage: recover, result: error, reason: mkdir quarantine");
            recorder_enter_error(RECORDER_REASON_SD_WRITE);
            recorder_set_events_ready(false);
            sd_mount_unmount();
            recorder_log_writer_stack_hw("mkdir quarantine");
            vTaskDelete(NULL);
            return;
        }
        if (!wav_recovery_scan_recordings(RECORDER_RECORDINGS_DIR,
                                          RECORDER_QUARANTINE_DIR,
                                          RECORDER_EVENTS_PATH, &rec_stats)) {
            ESP_LOGE(TAG, "stage: recover, result: error, reason: scan");
            recorder_enter_error(RECORDER_REASON_RECOVERY);
            recorder_set_events_ready(false);
            sd_mount_unmount();
            recorder_log_writer_stack_hw("recover scan");
            vTaskDelete(NULL);
            return;
        }
        ESP_LOGI(TAG,
                 "stage: recover, result: ok, scanned: %" PRIu32
                 ", recovered: %" PRIu32 ", quarantined: %" PRIu32
                 ", errors: %" PRIu32 ", pcm_bytes: %" PRIu32,
                 rec_stats.scanned, rec_stats.recovered,
                 rec_stats.quarantined, rec_stats.errors,
                 rec_stats.recovered_pcm_bytes);
        if (recorder_current_iso8601(s_manifest_now)) {
            uint32_t added = 0;
            uint32_t skipped = 0;
            if (!device_manifest_sync_wav_dir(
                    RECORDER_RECORDINGS_DIR, RECORDER_MANIFEST_PATH,
                    RECORDER_MANIFEST_TMP_PATH, s_device_id, s_manifest_now,
                    DEVICE_MANIFEST_STATE_RECOVERED, &added, &skipped)) {
                ESP_LOGE(TAG,
                         "stage: manifest, result: error, reason: manifest sync");
                recorder_enter_error(RECORDER_REASON_MANIFEST);
                recorder_set_events_ready(false);
                sd_mount_unmount();
                recorder_log_writer_stack_hw("manifest sync");
                vTaskDelete(NULL);
                return;
            }
            ESP_LOGI(TAG,
                     "stage: manifest, result: ok, added: %" PRIu32
                     ", skipped: %" PRIu32,
                     added, skipped);
        } else {
            ESP_LOGE(TAG,
                     "stage: manifest, result: error, reason: manifest time");
            recorder_enter_error(RECORDER_REASON_MANIFEST);
            recorder_set_events_ready(false);
            sd_mount_unmount();
            recorder_log_writer_stack_hw("manifest time");
            vTaskDelete(NULL);
            return;
        }
    }

    recorder_current_date_time(seg_date, cur_time);
    memset(rec_id, 0, sizeof(rec_id));
    memset(s_seg_rec_id, 0, sizeof(s_seg_rec_id));
    memset(s_seg_started, 0, sizeof(s_seg_started));
    if (!recorder_new_recording_id(rec_id) ||
        !recorder_current_iso8601(s_seg_started)) {
        ESP_LOGE(TAG, "stage: manifest, result: error, reason: manifest id");
        recorder_enter_error(RECORDER_REASON_MANIFEST);
        recorder_set_events_ready(false);
        sd_mount_unmount();
        recorder_log_writer_stack_hw("manifest id");
        vTaskDelete(NULL);
        return;
    }
    memcpy(s_seg_rec_id, rec_id, sizeof(s_seg_rec_id));
    if (!wav_rotation_build_part_path(s_seg_part, sizeof(s_seg_part),
                                      seg_date, cur_time, rec_id) ||
        !wav_rotation_build_wav_path(s_seg_wav, sizeof(s_seg_wav), seg_date,
                                     cur_time, rec_id)) {
        ESP_LOGE(TAG, "stage: rotate, result: error, reason: rotate path");
        recorder_enter_error(RECORDER_REASON_INTERNAL);
        recorder_set_events_ready(false);
        sd_mount_unmount();
        recorder_log_writer_stack_hw("rotate path");
        vTaskDelete(NULL);
        return;
    }
    if (sd_mount_ensure_date_dir(seg_date) != ESP_OK) {
        ESP_LOGE(TAG, "stage: rotate, result: error, reason: mkdir date");
        recorder_enter_error(RECORDER_REASON_SD_WRITE);
        recorder_set_events_ready(false);
        sd_mount_unmount();
        recorder_log_writer_stack_hw("mkdir date");
        vTaskDelete(NULL);
        return;
    }
    if (!sd_pcm_sink_open(&s_sink, s_seg_part)) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: part open");
        recorder_enter_error(RECORDER_REASON_SD_WRITE);
        recorder_set_events_ready(false);
        sd_mount_unmount();
        recorder_log_writer_stack_hw("part open");
        vTaskDelete(NULL);
        return;
    }
    wav_rotation_state_init(&s_seg_state, s_seg_part, s_seg_wav);
    seg_start_tick = xTaskGetTickCount();
    xEventGroupSetBits(s_rec_events, REC_BIT_WRITER_READY);

    while (running) {
        xEventGroupWaitBits(s_rec_events, REC_BIT_SLOT_FULL, pdTRUE,
                            pdFALSE, pdMS_TO_TICKS(1000));
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
                recorder_enter_error(RECORDER_REASON_SD_WRITE);
                running = false;
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
                if (!sd_pcm_sink_flush(&s_sink)) {
                    ESP_LOGE(TAG,
                             "stage: record, result: error, reason: part flush");
                    if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
                        pcm_pipeline_note_sd_error(&s_pipeline);
                        xSemaphoreGive(s_rec_lock);
                    }
                    recorder_enter_error(RECORDER_REASON_SD_FLUSH);
                    running = false;
                    break;
                }
                recorder_log_diagnostics();
            }
        }
        if (recorder_stop_requested()) {
            running = false;
            break;
        }
        {
            char now_date[RECORDER_DATE_STR_LEN];
            char now_time[RECORDER_TIME_STR_LEN];
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
                running = false;
                break;
            }
            if (reason == WAV_ROTATE_TIME_30MIN ||
                reason == WAV_ROTATE_MIDNIGHT) {
                if (!wav_rotation_finalize_once(&s_seg_state, &s_sink)) {
                    ESP_LOGE(TAG,
                             "stage: rotate, result: error, reason: finalize");
                    if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
                        pcm_pipeline_note_sd_error(&s_pipeline);
                        xSemaphoreGive(s_rec_lock);
                    }
                    recorder_enter_error(RECORDER_REASON_FINALIZE);
                    running = false;
                    break;
                }
                ESP_LOGI(TAG, "stage: rotate, result: ok, reason: %s",
                         recorder_rotate_reason_str(reason));
                if (!recorder_manifest_record_wav(
                        s_seg_wav, s_seg_rec_id, s_seg_started,
                        DEVICE_MANIFEST_STATE_FINALIZED)) {
                    if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
                        pcm_pipeline_note_sd_error(&s_pipeline);
                        xSemaphoreGive(s_rec_lock);
                    }
                    recorder_enter_error(RECORDER_REASON_MANIFEST);
                    running = false;
                    break;
                }
                memset(rec_id, 0, sizeof(rec_id));
                if (!recorder_new_recording_id(rec_id) ||
                    !recorder_current_iso8601(s_seg_started)) {
                    ESP_LOGE(TAG,
                             "stage: manifest, result: error, reason: manifest id");
                    recorder_enter_error(RECORDER_REASON_MANIFEST);
                    running = false;
                    break;
                }
                memcpy(s_seg_rec_id, rec_id, sizeof(s_seg_rec_id));
                if (!wav_rotation_build_part_path(s_rot_part,
                                                  sizeof(s_rot_part),
                                                  now_date, now_time,
                                                  rec_id) ||
                    !wav_rotation_build_wav_path(s_rot_wav,
                                                 sizeof(s_rot_wav),
                                                 now_date, now_time,
                                                 rec_id)) {
                    ESP_LOGE(TAG,
                             "stage: rotate, result: error, reason: rotate path");
                    recorder_enter_error(RECORDER_REASON_INTERNAL);
                    running = false;
                    break;
                }
                if (sd_mount_ensure_date_dir(now_date) != ESP_OK) {
                    ESP_LOGE(TAG,
                             "stage: rotate, result: error, reason: mkdir date");
                    recorder_enter_error(RECORDER_REASON_SD_WRITE);
                    running = false;
                    break;
                }
                if (!sd_pcm_sink_open(&s_sink, s_rot_part)) {
                    ESP_LOGE(TAG,
                             "stage: rotate, result: error, reason: part open");
                    recorder_enter_error(RECORDER_REASON_SD_WRITE);
                    running = false;
                    break;
                }
                memcpy(s_seg_part, s_rot_part, sizeof(s_seg_part));
                memcpy(s_seg_wav, s_rot_wav, sizeof(s_seg_wav));
                memcpy(seg_date, now_date, sizeof(seg_date));
                wav_rotation_state_init(&s_seg_state, s_seg_part, s_seg_wav);
                seg_bytes = 0;
                seg_start_tick = xTaskGetTickCount();
                since_flush = 0;
                ESP_LOGI(TAG,
                         "stage: record, result: capturing, path_suffix: .wav.part");
                recorder_log_writer_stack_hw("rotation");
            }
        }
        if (recorder_stop_requested()) {
            running = false;
        }
    }

    if (!wav_rotation_finalize_once(&s_seg_state, &s_sink)) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: part close");
        if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
            pcm_pipeline_note_sd_error(&s_pipeline);
            xSemaphoreGive(s_rec_lock);
        }
        recorder_enter_error(RECORDER_REASON_FINALIZE);
    } else {
        ESP_LOGI(TAG, "stage: finalize, result: ok");
        if (!recorder_manifest_record_wav(s_seg_wav, s_seg_rec_id,
                                          s_seg_started,
                                          DEVICE_MANIFEST_STATE_FINALIZED)) {
            if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
                pcm_pipeline_note_sd_error(&s_pipeline);
                xSemaphoreGive(s_rec_lock);
            }
            recorder_enter_error(RECORDER_REASON_MANIFEST);
        }
    }
    {
        recorder_state_t final_state = recorder_current_state();
        if (final_state == RECORDER_STATE_LOW_BATTERY_STOP) {
            ESP_LOGI(TAG,
                     "stage: record, result: low-battery-stop, battery_mv: %d",
                     s_last_battery_mv);
        } else if (final_state == RECORDER_STATE_ERROR) {
            ESP_LOGE(TAG, "stage: record, result: failure-stop");
        } else {
            ESP_LOGI(TAG, "stage: record, result: safe-stop");
        }
    }
    recorder_set_events_ready(false);
    sd_mount_unmount();
    recorder_log_writer_stack_hw("terminal");
    vTaskDelete(NULL);
}

void app_main(void) {
    // M5Capsule v1.1 requires HOLD=High immediately after wake or battery-
    // powered execution returns to sleep. Do this before filesystem/tasks.
    esp_err_t hold_err = recorder_power_enable_hold();

    ESP_LOGI(TAG, "m5daylog firmware scaffold boot");

    esp_chip_info_t chip_info;
    esp_chip_info(&chip_info);
    ESP_LOGI(TAG, "chip cores: %d, revision: %d", chip_info.cores,
             chip_info.revision);

    uint32_t flash_size = 0;
    if (esp_flash_get_size(esp_flash_default_chip, &flash_size) == ESP_OK) {
        ESP_LOGI(TAG, "flash size: %" PRIu32 " MB",
                 flash_size / (1024 * 1024));
    } else {
        ESP_LOGW(TAG, "flash size: unknown");
    }

    ESP_LOGI(TAG, "idf version: %s", esp_get_idf_version());
    ESP_LOGI(TAG, "stage: scaffold, result: boot ok");

    recorder_reference_stop_wrappers();
    recorder_state_machine_init(&s_rec_state);
    pcm_pipeline_init(&s_pipeline, s_slot0, s_slot1);
    memset(&s_sink, 0, sizeof(s_sink));

    s_state_lock = xSemaphoreCreateMutex();
    if (s_state_lock == NULL) {
        ESP_LOGE(TAG, "stage: state, result: error, reason: state lock");
    } else {
        s_rec_events = xEventGroupCreate();
        if (s_rec_events == NULL) {
            ESP_LOGE(TAG, "stage: record, result: error, reason: rec events");
            (void)recorder_transition_state(RECORDER_STATE_ERROR,
                                            RECORDER_REASON_INTERNAL, 0);
        } else if (hold_err != ESP_OK) {
            ESP_LOGE(TAG, "stage: power, result: error, reason: hold init");
            recorder_enter_error(RECORDER_REASON_INTERNAL);
        } else {
            s_rec_lock = xSemaphoreCreateMutex();
            if (s_rec_lock == NULL) {
                ESP_LOGE(TAG, "stage: record, result: error, reason: rec lock");
                recorder_enter_error(RECORDER_REASON_INTERNAL);
            } else if (recorder_status_led_init() != ESP_OK) {
                ESP_LOGE(TAG,
                         "stage: state, result: error, reason: status led init");
                recorder_enter_error(RECORDER_REASON_INTERNAL);
            } else {
                s_status_led_ready = true;
                if (xTaskCreate(recorder_writer_task, "rec_writer",
                                RECORDER_WRITER_STACK_BYTES, NULL, 4,
                                NULL) != pdPASS) {
                    ESP_LOGE(TAG,
                             "stage: record, result: error, reason: task spawn");
                    recorder_enter_error(RECORDER_REASON_INTERNAL);
                } else if (xTaskCreate(recorder_capture_task, "rec_capture",
                                       4096, NULL, 5, NULL) != pdPASS) {
                    ESP_LOGE(TAG,
                             "stage: record, result: error, reason: task spawn");
                    recorder_enter_error(RECORDER_REASON_INTERNAL);
                } else if (xTaskCreate(recorder_battery_task, "rec_battery",
                                       3072, NULL, 3, NULL) != pdPASS) {
                    ESP_LOGE(TAG,
                             "stage: battery, result: error, reason: task spawn");
                    recorder_enter_error(RECORDER_REASON_INTERNAL);
                }
            }
        }
    }

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        ESP_LOGI(TAG, "stage: scaffold, result: idle, state: %s",
                 recorder_state_str(recorder_current_state()));
    }
}
