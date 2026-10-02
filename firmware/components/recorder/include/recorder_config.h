#pragma once

// M5Daylog recorder fixed audio contract — Tasks #44 (IM-007) / #45
// (IM-008) / #46 (IM-009).
//
// 16kHz / signed 16bit little-endian / mono PCM from the PDM mic via
// I2S/DMA, staged through a 32KB x 2 double buffer into a `.wav.part`
// file on microSD. These values are the PoC baseline (Decisions #6, Spec
// #36) and are asserted by firmware/tests/test_pcm_pipeline.py and
// firmware/tests/test_recording_contract.py — change only via an explicit
// Spec/Decision update, never opportunistically.

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// --- Audio format (fixed for PoC) -------------------------------------
#define RECORDER_SAMPLE_RATE_HZ 16000u
#define RECORDER_CHANNELS 1u
#define RECORDER_BITS_PER_SAMPLE 16u
#define RECORDER_BYTES_PER_SAMPLE 2u  // 16bit mono: one 2-byte frame
#define RECORDER_BYTE_RATE \
    (RECORDER_SAMPLE_RATE_HZ * RECORDER_CHANNELS * RECORDER_BYTES_PER_SAMPLE)
#define RECORDER_BLOCK_ALIGN (RECORDER_CHANNELS * RECORDER_BYTES_PER_SAMPLE)

// --- Double buffer (Spec #36 initial value) -----------------------------
#define RECORDER_BUFFER_BYTES 32768u
#define RECORDER_BUFFER_SLOTS 2u

// --- WAV framing ----------------------------------------------------------
#define RECORDER_WAV_HEADER_SIZE 44u
// Spec #36 recording shape: `HHMMSS_<recordingId>.wav.part`. Only this
// suffix is accepted for capture output — never a bare `.part`.
#define RECORDER_PART_SUFFIX ".wav.part"
// Finalized suffix after Task #45 rotation/finalize: `.wav.part` is
// header-finalized, flushed, closed, then renamed to `.wav` (PC syncs
// only `.wav`). The rename strips the trailing `.part`.
#define RECORDER_WAV_SUFFIX ".wav"

// --- WAV rotation / finalize (Task #45, IM-008) ---------------------------
// 30-minute rotation baseline (Decision #7, Spec #36). At the fixed
// 16kHz/16bit/mono byte rate (32000 B/s) one segment holds exactly
// 57,600,000 payload bytes. Midnight rotation switches the date
// directory (`recordings/YYYY-MM-DD/`) without mixing dates.
#define RECORDER_ROTATION_INTERVAL_SEC 1800u
#define RECORDER_ROTATION_PAYLOAD_BYTES \
    (RECORDER_BYTE_RATE * RECORDER_ROTATION_INTERVAL_SEC)
// Absolute path buffer for `recordings/YYYY-MM-DD/HHMMSS_<id>.wav.part`.
// 256 bytes covers the mount prefix plus LFN date/time/id segments.
#define RECORDER_MAX_PATH_LEN 256u
#define RECORDER_DATE_STR_LEN 11u  // "YYYY-MM-DD" + NUL
#define RECORDER_TIME_STR_LEN 7u   // "HHMMSS" + NUL

// One full 32KB slot at 16kHz/16bit/mono holds exactly:
//   32768 bytes / 2 bytes/sample = 16384 samples = 1.024 s of audio.
#define RECORDER_SAMPLES_PER_SLOT \
    (RECORDER_BUFFER_BYTES / RECORDER_BYTES_PER_SAMPLE)

// --- M5Capsule v1.1 board baseline (Task #44 hardware revision) ---------
// PDM microphone data lines and microSD SPI bus pins below are the verified
// M5Capsule v1.1 bring-up values. Each is guarded so build flags
// (`-DRECORDER_...=...`) can override without editing source. Invalid pins
// fail init fail-loud; the firmware never reports recording while bring-up
// has failed (Spec #36 silent-state prohibition).
#ifndef RECORDER_PDM_CLK_PIN
#define RECORDER_PDM_CLK_PIN 40
#endif
#ifndef RECORDER_PDM_DATA_PIN
#define RECORDER_PDM_DATA_PIN 41
#endif
#ifndef RECORDER_SD_MOUNT_POINT
#define RECORDER_SD_MOUNT_POINT "/sdcard"
#endif
#ifndef RECORDER_SD_CS_PIN
#define RECORDER_SD_CS_PIN 11
#endif
#ifndef RECORDER_SD_MOSI_PIN
#define RECORDER_SD_MOSI_PIN 12
#endif
#ifndef RECORDER_SD_CLK_PIN
#define RECORDER_SD_CLK_PIN 14
#endif
#ifndef RECORDER_SD_MISO_PIN
#define RECORDER_SD_MISO_PIN 39
#endif

// Filesystem scope for Tasks #44/#45/#46: board bring-up creates the
// live-recording directories, per-date subdirectories
// (`recordings/YYYY-MM-DD/`) for rotation/finalize, and the quarantine
// directory for Task #46 power-loss recovery. Task #47 metadata files
// below live in the same M5DAYLOG root; later retention/ACK handling
// belongs to later Tasks and must not be created here.
#define RECORDER_M5DAYLOG_DIR "/sdcard/M5DAYLOG"
#define RECORDER_RECORDINGS_DIR "/sdcard/M5DAYLOG/recordings"
// Task #46 (IM-009): unrecoverable `.wav.part` files are isolated here
// with rename() only and never auto-deleted.
#define RECORDER_QUARANTINE_DIR "/sdcard/M5DAYLOG/quarantine"
// Task #46 (IM-009): boot recovery summary + per-file results are
// appended here as JSON lines (counts / sizes / result classes only).
#define RECORDER_EVENTS_PATH "/sdcard/M5DAYLOG/events.jsonl"

// --- Device metadata / manifest (Task #47, IM-010) ------------------------
// Spec #35 (S-002) identity + integrity contract: first-boot `deviceId`
// (UUIDv4, NVS-persisted) surfaces in `device.json`; every
// finalized/recovered `.wav` (WAV file bytes entire, incremental SHA-256,
// lowercase hex 64 chars) is reflected in `manifest.json` via
// `manifest.tmp` full-write + flush + atomic rename. PoC
// `schemaVersion=1`; unknown majors fail closed without writing.
// Timestamps are offset ISO-8601 (`YYYY-MM-DDTHH:MM:SS+00:00`, UTC).
#define RECORDER_METADATA_SCHEMA_VERSION 1u
#define RECORDER_MODEL "M5Capsule v1.1"
#define RECORDER_FIRMWARE_VERSION "0.1.0"
#define RECORDER_DEVICE_JSON_PATH "/sdcard/M5DAYLOG/device.json"
#define RECORDER_MANIFEST_PATH "/sdcard/M5DAYLOG/manifest.json"
#define RECORDER_MANIFEST_TMP_PATH "/sdcard/M5DAYLOG/manifest.tmp"
// UUIDv4 canonical string: 8-4-4-4-12 lowercase hex (36 chars + NUL).
#define RECORDER_UUID_STR_LEN 37u
// SHA-256 of the WAV file bytes entire: 64 lowercase hex chars + NUL.
#define RECORDER_SHA256_HEX_LEN 65u
// Offset ISO-8601 `YYYY-MM-DDTHH:MM:SS+HH:MM`: 25 chars + NUL.
#define RECORDER_ISO8601_STR_LEN 32u
// Single recording entry JSON bound (relative filename + UUIDs + hex).
#define RECORDER_MANIFEST_ENTRY_MAX 1024u
// Manifest file rewrite bound: 32 finalized/recovered segments per 16h
// day (~350 bytes each) fit comfortably; fail-loud above this bound
// rather than truncating integrity data.
#define RECORDER_MANIFEST_MAX_BYTES 131072u

// Sanity: slots must hold whole samples (even byte count for 16bit PCM).
_Static_assert((RECORDER_BUFFER_BYTES % RECORDER_BYTES_PER_SAMPLE) == 0,
               "recorder buffer must hold whole 16bit samples");

#ifdef __cplusplus
}
#endif
