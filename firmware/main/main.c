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
#include "sd_mount.h"
#include "sd_pcm_sink.h"
#include "wav_recovery.h"
#include "wav_rotation.h"

static const char *TAG = "m5daylog";

// Tasks #44/#45/#46/#47: SD mount -> RECOVER -> PDM capture + SD writer ->
// `.wav.part` with Task #45 rotation/finalize to `.wav`, Task #46 boot
// power-loss recovery (residual `.wav.part` -> recovered `.wav` or
// `quarantine/`, results to `events.jsonl`), and Task #47 device
// identity + manifest/integrity (NVS `deviceId`, `device.json`,
// per-segment UUIDv4 `recordingId`, incremental SHA-256 over the WAV
// file bytes entire, `manifest.json` via `manifest.tmp` atomic rename).
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
// (events, lock, mount, recovery, mic init, `.part` open, I2S/SD I/O,
// rotation path/dir/open, device identity, manifest) enters ERROR, never
// silent recording. Date subdirectories (`recordings/YYYY-MM-DD/`) are
// Task #45 scope; Task #46 owns the boot RECOVER step (scan residual
// `.wav.part`, rebuild headers from payload length with tail-only
// truncation, quarantine unrecoverable files without auto-delete, log
// counts to `events.jsonl`); Task #47 owns NVS `deviceId`,
// `device.json`, per-segment UUIDv4 `recordingId`, incremental SHA-256,
// and `manifest.json` (tmp + atomic rename, CONFLICT never overwrites).
// Processed-ACK retention stays out of scope (later Task).
#ifndef RECORDER_PART_PATH
#define RECORDER_PART_PATH \
    "/sdcard/M5DAYLOG/recordings/000000_pending.wav.part"
#endif
// NOTE: the default path above is a build-time fallback with the Spec #36
// `HHMMSS_<recordingId>.wav.part` shape. Task #45 runtime naming builds
// `recordings/YYYY-MM-DD/HHMMSS_<recordingId>.wav.part` from the wall
// clock; Task #47 fills `<recordingId>` with a fresh UUIDv4 per segment
// (122 hardware-RNG bits, NVS device identity is separate and stable).
// Midnight never mixes dates: the date directory switches with the new
// file. Only the `.wav.part` suffix is ever opened; PC syncs only the
// finalized `.wav` listed in `manifest.json`.

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

// Writer stack budget (Task #45 hardware-gate fix for the boot-without-SD
// stack overflow + reboot loop): the writer call path nests 256B path
// snprintf builds, date-dir mkdir, FATFS f_open/f_write/f_sync across the
// finalize/rename switch, and ESP_LOG formatting. The 4x256B segment path
// buffers therefore live in writer-owned static .bss instead of the task
// frame (capture never touches them; same single-owner discipline as
// s_sink, no lock needed). The task itself runs at
// RECORDER_WRITER_STACK_BYTES so FATFS/VFS/newlib keep headroom;
// uxTaskGetStackHighWaterMark is logged at every writer exit
// (stage ... result: stack) as high-water evidence for hardware
// validation. Overflow detection stays enabled; this sizes the budget
// instead of suppressing it.
#define RECORDER_WRITER_STACK_BYTES 6144

// Writer-owned segment path storage (see stack-budget note above).
static char s_seg_part[RECORDER_MAX_PATH_LEN];
static char s_seg_wav[RECORDER_MAX_PATH_LEN];
static char s_rot_part[RECORDER_MAX_PATH_LEN];
static char s_rot_wav[RECORDER_MAX_PATH_LEN];
static wav_rotation_state_t s_seg_state;

// Task #47 writer-owned metadata storage (same stack-budget discipline:
// the 1KB manifest entry plus identity/ISO buffers live in .bss, never
// in the writer task frame; the writer is the single owner, no lock).
static char s_device_id[RECORDER_UUID_STR_LEN];
static char s_manifest_entry[RECORDER_MANIFEST_ENTRY_MAX];
static char s_manifest_now[RECORDER_ISO8601_STR_LEN];
static char s_seg_rec_id[RECORDER_UUID_STR_LEN];
static char s_seg_started[RECORDER_ISO8601_STR_LEN];

// M5DAYLOG-relative prefix stripped when recording manifest filenames
// (`/sdcard/M5DAYLOG/recordings/...` -> `recordings/...`).
static const char *const k_m5daylog_prefix = "/sdcard/M5DAYLOG/";

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

// Task #47: fresh UUIDv4 recordingId per segment from the hardware RNG
// (122 random bits). Fail-loud false on any formatting failure so the
// caller never reuses a stale id (recordingId collisions are forbidden).
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

// Task #47: current UTC offset ISO-8601 (`YYYY-MM-DDTHH:MM:SS+00:00`)
// for manifest `updatedAt`/`startedAt`. Falls back to the stable epoch
// when the wall clock is not yet plausible (same rule as segment naming;
// never fabricates a local offset the device does not know).
static bool recorder_current_iso8601(char out[RECORDER_ISO8601_STR_LEN]) {
    char date[RECORDER_DATE_STR_LEN];
    char time6[RECORDER_TIME_STR_LEN];
    int n;
    if (out == NULL) {
        return false;
    }
    memset(out, 0, RECORDER_ISO8601_STR_LEN);
    recorder_current_date_time(date, time6);
    n = snprintf(out, RECORDER_ISO8601_STR_LEN, "%.4s-%.2s-%.2sT%.2s:%.2s:%.2s+00:00",
                 date, date + 5, date + 8, time6, time6 + 2, time6 + 4);
    if (n < 0 || (size_t)n >= RECORDER_ISO8601_STR_LEN ||
        !device_manifest_is_valid_iso8601_offset(out)) {
        memset(out, 0, RECORDER_ISO8601_STR_LEN);
        return false;
    }
    return true;
}

// Task #47: hash one finalized `.wav` and upsert its manifest entry.
// `wav_path` is the absolute finalized path, `rec_id`/`started_at` are the
// segment-start values, `state` is finalized/recovered. Uses the
// writer-owned static entry/ISO buffers (no task-frame growth). Returns
// true only on DEVICE_MANIFEST_OK (idempotent duplicates count as ok).
// Logs carry only stage/result metadata, never filenames, ids, or hashes.
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

// Task #47: ensure an empty manifest exists when neither the manifest nor
// its tmp exists (first boot). A present manifest is left untouched; a
// leftover tmp with a missing destination is promoted when valid.
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
        // No promotable tmp: distinguish first boot (neither file) from a
        // corrupt tmp alongside a missing destination (fail-closed).
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
    // First boot: write the canonical empty document via tmp + rename.
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

// Minimum-ever-free writer stack, in the same unit as the xTaskCreate
// stack depth. Logged at every writer exit (including the SD-mount
// failure path) so the boot-without-SD hardware scenario leaves
// high-water evidence instead of only a reboot loop. Metadata only.
static void recorder_log_writer_stack_hw(const char *reason) {
    UBaseType_t hw = uxTaskGetStackHighWaterMark(NULL);
    ESP_LOGI(TAG, "stage: record, result: stack, writer_hw: %u, reason: %s",
             (unsigned)hw, reason);
}

static void recorder_writer_task(void *arg) {
    uint32_t since_flush = 0;
    bool running = true;
    // Task #45 rotation segment tracking. Small scalars stay in this frame;
    // the 256B path buffers + finalize state live in writer-owned static
    // storage (see stack-budget note above), so even this early
    // mount-failure exit runs on a small frame. Capture keeps producing
    // into the pipeline across a rotation; only terminal STOP tears capture
    // down. seg_bytes counts payload bytes of the CURRENT segment for the
    // 30-minute size trigger (equivalent to 1800s at 32000 B/s).
    char seg_date[RECORDER_DATE_STR_LEN];
    char cur_time[RECORDER_TIME_STR_LEN];
    char rec_id[RECORDER_UUID_STR_LEN];
    uint32_t seg_bytes = 0;
    TickType_t seg_start_tick = 0;

    (void)arg;
    if (sd_mount_recordings() != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: sd mount");
        recorder_request_stop();
        recorder_log_writer_stack_hw("sd mount");
        vTaskDelete(NULL);
        return;
    }
    // Task #47 identity (Spec #35 S-002): stable NVS `deviceId` (first
    // boot generates a UUIDv4, later boots reuse it), `device.json`
    // present and matching (mismatch or unknown schema never
    // auto-overwritten, fail-closed), and a manifest shell (promote a
    // valid `manifest.tmp` when the destination is missing, else create
    // the canonical empty document on first boot). Any failure enters
    // ERROR before recovery or capture so the device never records under
    // a forked or unlisted identity.
    memset(s_device_id, 0, sizeof(s_device_id));
    if (device_identity_ensure_device_id(s_device_id) != ESP_OK) {
        ESP_LOGE(TAG, "stage: manifest, result: error, reason: device id");
        sd_mount_unmount();
        recorder_request_stop();
        recorder_log_writer_stack_hw("device id");
        vTaskDelete(NULL);
        return;
    }
    if (!device_identity_ensure_device_json(RECORDER_DEVICE_JSON_PATH,
                                            s_device_id, NULL, NULL)) {
        ESP_LOGE(TAG, "stage: manifest, result: error, reason: device json");
        sd_mount_unmount();
        recorder_request_stop();
        recorder_log_writer_stack_hw("device json");
        vTaskDelete(NULL);
        return;
    }
    if (!recorder_ensure_manifest_exists()) {
        ESP_LOGE(TAG, "stage: manifest, result: error, reason: manifest init");
        sd_mount_unmount();
        recorder_request_stop();
        recorder_log_writer_stack_hw("manifest init");
        vTaskDelete(NULL);
        return;
    }
    // Task #46 RECOVER (Spec #36 BOOT -> HW_INIT -> SD_MOUNT -> RECOVER ->
    // RTC_CHECK -> RECORDING): scan residual `.wav.part` files left by a
    // power loss, rebuild each WAV header from the written PCM payload
    // length (odd tail truncated to the sample boundary only), rename
    // recovered files `.wav.part` -> `.wav`, and isolate unrecoverable
    // files into `quarantine/` with rename() only (never auto-deleted).
    // Results (counts/sizes only, no audio or credential content) are
    // appended to `events.jsonl` by the scan itself; the serial log below
    // carries the same counts for hardware evidence. A fatal scan failure
    // (recordings unreadable / quarantine uncreatable) enters ERROR and
    // never starts recording, so a silent unrecovered state is impossible.
    // Recovery runs before the new segment opens so the fresh capture
    // never collides with a residual file.
    {
        wav_recovery_stats_t rec_stats;
        memset(&rec_stats, 0, sizeof(rec_stats));
        if (sd_mount_ensure_quarantine_dir() != ESP_OK) {
            ESP_LOGE(TAG,
                     "stage: recover, result: error, reason: mkdir quarantine");
            sd_mount_unmount();
            recorder_request_stop();
            recorder_log_writer_stack_hw("mkdir quarantine");
            vTaskDelete(NULL);
            return;
        }
        if (!wav_recovery_scan_recordings(RECORDER_RECORDINGS_DIR,
                                          RECORDER_QUARANTINE_DIR,
                                          RECORDER_EVENTS_PATH, &rec_stats)) {
            ESP_LOGE(TAG,
                     "stage: recover, result: error, reason: scan");
            sd_mount_unmount();
            recorder_request_stop();
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
        // Task #47: recovered `.wav` files enter the manifest with
        // state=recovered (recordingId from the filename, startedAt from
        // the date directory + HHMMSS prefix, incremental file hash).
        // Unparseable names are skipped without failing the boot; a
        // manifest I/O failure enters ERROR so recovered audio is never
        // left unlisted.
        if (recorder_current_iso8601(s_manifest_now)) {
            uint32_t added = 0;
            uint32_t skipped = 0;
            if (!device_manifest_sync_wav_dir(
                    RECORDER_RECORDINGS_DIR, RECORDER_MANIFEST_PATH,
                    RECORDER_MANIFEST_TMP_PATH, s_device_id, s_manifest_now,
                    DEVICE_MANIFEST_STATE_RECOVERED, &added, &skipped)) {
                ESP_LOGE(TAG,
                         "stage: manifest, result: error, reason: manifest sync");
                sd_mount_unmount();
                recorder_request_stop();
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
            sd_mount_unmount();
            recorder_request_stop();
            recorder_log_writer_stack_hw("manifest time");
            vTaskDelete(NULL);
            return;
        }
    }
    // Initial segment: runtime date/time naming with the Spec #36
    // `HHMMSS_<recordingId>.wav.part` shape inside the date directory.
    // Task #47 fills `<recordingId>` with a fresh UUIDv4 (opaque random
    // identifier, no private content) and records its UTC startedAt for
    // the manifest entry written at finalize.
    recorder_current_date_time(seg_date, cur_time);
    memset(rec_id, 0, sizeof(rec_id));
    memset(s_seg_rec_id, 0, sizeof(s_seg_rec_id));
    memset(s_seg_started, 0, sizeof(s_seg_started));
    if (!recorder_new_recording_id(rec_id) ||
        !recorder_current_iso8601(s_seg_started)) {
        ESP_LOGE(TAG, "stage: manifest, result: error, reason: manifest id");
        sd_mount_unmount();
        recorder_request_stop();
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
        sd_mount_unmount();
        recorder_request_stop();
        recorder_log_writer_stack_hw("rotate path");
        vTaskDelete(NULL);
        return;
    }
    if (sd_mount_ensure_date_dir(seg_date) != ESP_OK) {
        ESP_LOGE(TAG, "stage: rotate, result: error, reason: mkdir date");
        sd_mount_unmount();
        recorder_request_stop();
        recorder_log_writer_stack_hw("mkdir date");
        vTaskDelete(NULL);
        return;
    }
    if (!sd_pcm_sink_open(&s_sink, s_seg_part)) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: part open");
        sd_mount_unmount();
        recorder_request_stop();
        recorder_log_writer_stack_hw("part open");
        vTaskDelete(NULL);
        return;
    }
    wav_rotation_state_init(&s_seg_state, s_seg_part, s_seg_wav);
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
                if (!wav_rotation_finalize_once(&s_seg_state, &s_sink)) {
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
                // Task #47: the just-finalized segment enters the manifest
                // (incremental SHA-256 over the `.wav` bytes entire, tmp +
                // atomic rename, CONFLICT never overwrites). A manifest
                // failure stops rotation fail-loud like a finalize
                // failure, so finalized audio is never left unlisted.
                if (!recorder_manifest_record_wav(
                        s_seg_wav, s_seg_rec_id, s_seg_started,
                        DEVICE_MANIFEST_STATE_FINALIZED)) {
                    if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) ==
                        pdTRUE) {
                        pcm_pipeline_note_sd_error(&s_pipeline);
                        xSemaphoreGive(s_rec_lock);
                    }
                    running = false;
                    recorder_request_stop();
                    break;
                }
                // Open the next segment in the (possibly new) date
                // directory. Date directories never mix: midnight rotates
                // into the new date dir with a fresh file and a fresh
                // UUIDv4 recordingId (collisions forbidden).
                memset(rec_id, 0, sizeof(rec_id));
                if (!recorder_new_recording_id(rec_id) ||
                    !recorder_current_iso8601(s_seg_started)) {
                    ESP_LOGE(TAG,
                             "stage: manifest, result: error, reason: manifest id");
                    running = false;
                    recorder_request_stop();
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
                if (!sd_pcm_sink_open(&s_sink, s_rot_part)) {
                    ESP_LOGE(TAG,
                             "stage: rotate, result: error, reason: part open");
                    running = false;
                    recorder_request_stop();
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
                // High-water evidence for the worst-case writer call path
                // (finalize + rename + date-dir mkdir + FATFS open above),
                // emitted after the next segment is open and recording
                // continues, so the 30-minute run shows stack margin
                // without terminating the writer.
                recorder_log_writer_stack_hw("rotation");
            }
        }
        if (recorder_stop_requested()) {
            running = false;
        }
    }

    // Terminal finalize (USB connection / low battery / safe-stop request
    // and any error teardown): header finalize then rename to `.wav`.
    // Idempotent: simultaneous stop duplicates return the cached result
    // with no double close and no double rename. PC syncs only `.wav`
    // entries listed in `manifest.json` (Task #47 records the finalized
    // segment the same way as rotation; a manifest failure is fail-loud
    // like a close failure).
    if (!wav_rotation_finalize_once(&s_seg_state, &s_sink)) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: part close");
        if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
            pcm_pipeline_note_sd_error(&s_pipeline);
            xSemaphoreGive(s_rec_lock);
        }
    } else {
        ESP_LOGI(TAG, "stage: finalize, result: ok");
        if (!recorder_manifest_record_wav(s_seg_wav, s_seg_rec_id,
                                          s_seg_started,
                                          DEVICE_MANIFEST_STATE_FINALIZED)) {
            if (xSemaphoreTake(s_rec_lock, portMAX_DELAY) == pdTRUE) {
                pcm_pipeline_note_sd_error(&s_pipeline);
                xSemaphoreGive(s_rec_lock);
            }
        }
    }
    sd_mount_unmount();
    ESP_LOGE(TAG, "stage: record, result: error, reason: stopped");
    recorder_log_writer_stack_hw("stopped");
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

    // Tasks #44/#45/#46/#47 recording path. The event group is the only
    // cross-task channel (no published task handles); the writer-ready
    // handshake makes creation order irrelevant. Task #45
    // rotation/finalize, Task #46 boot RECOVER, and Task #47
    // identity/manifest (NVS deviceId, device.json, per-segment UUIDv4,
    // SHA-256, manifest.json via tmp + atomic rename) are wired into the
    // writer task above; later retention/state tasks attach without
    // changing the capture contract.
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
        } else if (xTaskCreate(recorder_writer_task, "rec_writer",
                               RECORDER_WRITER_STACK_BYTES, NULL, 4,
                               NULL) != pdPASS) {
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
