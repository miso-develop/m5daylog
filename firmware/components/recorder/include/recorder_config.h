#pragma once

// M5Daylog recorder fixed audio contract — Task #44 (IM-007).
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

// Filesystem scope for Task #44: only the live-recording directories are
// created by board bring-up. Finalized-file handling and anything else
// (rotation/finalize, recovery, retention bookkeeping) belong to later
// Tasks and must not be created here.
#define RECORDER_M5DAYLOG_DIR "/sdcard/M5DAYLOG"
#define RECORDER_RECORDINGS_DIR "/sdcard/M5DAYLOG/recordings"

// Sanity: slots must hold whole samples (even byte count for 16bit PCM).
_Static_assert((RECORDER_BUFFER_BYTES % RECORDER_BYTES_PER_SAMPLE) == 0,
               "recorder buffer must hold whole 16bit samples");

#ifdef __cplusplus
}
#endif
