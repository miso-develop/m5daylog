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
#define RECORDER_PART_SUFFIX ".part"

// One full 32KB slot at 16kHz/16bit/mono holds exactly:
//   32768 bytes / 2 bytes/sample = 16384 samples = 1.024 s of audio.
#define RECORDER_SAMPLES_PER_SLOT \
    (RECORDER_BUFFER_BYTES / RECORDER_BYTES_PER_SAMPLE)

// Sanity: slots must hold whole samples (even byte count for 16bit PCM).
_Static_assert((RECORDER_BUFFER_BYTES % RECORDER_BYTES_PER_SAMPLE) == 0,
               "recorder buffer must hold whole 16bit samples");

#ifdef __cplusplus
}
#endif
