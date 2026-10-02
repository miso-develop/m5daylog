#pragma once

// M5Daylog recorder fixed audio contract — Tasks #44 (IM-007) / #45
// (IM-008) / #46 (IM-009) / #47 (IM-010) / #48 (IM-011).
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

#define RECORDER_SAMPLE_RATE_HZ 16000u
#define RECORDER_CHANNELS 1u
#define RECORDER_BITS_PER_SAMPLE 16u
#define RECORDER_BYTES_PER_SAMPLE 2u
#define RECORDER_BYTE_RATE \
    (RECORDER_SAMPLE_RATE_HZ * RECORDER_CHANNELS * RECORDER_BYTES_PER_SAMPLE)
#define RECORDER_BLOCK_ALIGN (RECORDER_CHANNELS * RECORDER_BYTES_PER_SAMPLE)

#define RECORDER_BUFFER_BYTES 32768u
#define RECORDER_BUFFER_SLOTS 2u

#define RECORDER_WAV_HEADER_SIZE 44u
#define RECORDER_PART_SUFFIX ".wav.part"
#define RECORDER_WAV_SUFFIX ".wav"

#define RECORDER_ROTATION_INTERVAL_SEC 1800u
#define RECORDER_ROTATION_PAYLOAD_BYTES \
    (RECORDER_BYTE_RATE * RECORDER_ROTATION_INTERVAL_SEC)
#define RECORDER_MAX_PATH_LEN 256u
#define RECORDER_DATE_STR_LEN 11u
#define RECORDER_TIME_STR_LEN 7u
#define RECORDER_SAMPLES_PER_SLOT \
    (RECORDER_BUFFER_BYTES / RECORDER_BYTES_PER_SAMPLE)

// --- M5Capsule v1.1 board baseline (Tasks #44 / #48) --------------------
// Official Capsule v1.1 requirements: HOLD G46 must be driven high after
// wake to keep the unit powered; VBAT is sensed through the Stamp-S3A ADC
// on GPIO6 with a 2:1 divider; RGB data is GPIO21 and its v1.1 power gate
// must be enabled with GPIO38 before the pixel is driven.
#ifndef RECORDER_POWER_HOLD_PIN
#define RECORDER_POWER_HOLD_PIN 46
#endif
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
#ifndef RECORDER_BATTERY_ADC_PIN
#define RECORDER_BATTERY_ADC_PIN 6
#endif
#ifndef RECORDER_STATUS_LED_DATA_PIN
#define RECORDER_STATUS_LED_DATA_PIN 21
#endif
#ifndef RECORDER_STATUS_LED_POWER_PIN
#define RECORDER_STATUS_LED_POWER_PIN 38
#endif

// Decision #30 calls for an initial ~10% low-battery safe-close threshold,
// then recalibration after measuring the actual discharge curve. Voltage is
// therefore the explicit configurable PoC boundary instead of pretending a
// raw ADC estimate is a precise state-of-charge percentage. Three
// consecutive low readings suppress transient load sag before safe stop.
#ifndef RECORDER_LOW_BATTERY_MV
#define RECORDER_LOW_BATTERY_MV 3600
#endif
#ifndef RECORDER_BATTERY_POLL_MS
#define RECORDER_BATTERY_POLL_MS 5000u
#endif
#ifndef RECORDER_LOW_BATTERY_CONFIRM_SAMPLES
#define RECORDER_LOW_BATTERY_CONFIRM_SAMPLES 3u
#endif

#define RECORDER_M5DAYLOG_DIR "/sdcard/M5DAYLOG"
#define RECORDER_RECORDINGS_DIR "/sdcard/M5DAYLOG/recordings"
#define RECORDER_QUARANTINE_DIR "/sdcard/M5DAYLOG/quarantine"
#define RECORDER_EVENTS_PATH "/sdcard/M5DAYLOG/events.jsonl"

#define RECORDER_METADATA_SCHEMA_VERSION 1u
#define RECORDER_MODEL "M5Capsule v1.1"
#define RECORDER_FIRMWARE_VERSION "0.1.0"
#define RECORDER_DEVICE_JSON_PATH "/sdcard/M5DAYLOG/device.json"
#define RECORDER_MANIFEST_PATH "/sdcard/M5DAYLOG/manifest.json"
#define RECORDER_MANIFEST_TMP_PATH "/sdcard/M5DAYLOG/manifest.tmp"
#define RECORDER_UUID_STR_LEN 37u
#define RECORDER_SHA256_HEX_LEN 65u
#define RECORDER_ISO8601_STR_LEN 32u
#define RECORDER_MANIFEST_ENTRY_MAX 1024u
#define RECORDER_MANIFEST_MAX_BYTES 131072u

_Static_assert((RECORDER_BUFFER_BYTES % RECORDER_BYTES_PER_SAMPLE) == 0,
               "recorder buffer must hold whole 16bit samples");
_Static_assert(RECORDER_LOW_BATTERY_CONFIRM_SAMPLES > 0,
               "low-battery confirmation must require at least one sample");

#ifdef __cplusplus
}
#endif
