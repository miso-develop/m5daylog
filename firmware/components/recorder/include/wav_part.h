#pragma once

// Portable RIFF/WAVE `.part` framing — Task #44.
//
// Recording lifecycle (Spec #36): while capturing, audio lives in
// `HHMMSS_<recordingId>.wav.part`; rotation/finalize renames it to `.wav`
// (Task #45, not here). This module writes the `.part` side only: a
// standard 44-byte PCM header at open (sizes zeroed), the PCM payload
// appended as capture proceeds, and header patch-up on flush/close so the
// file is decodeable by any standard WAV decoder after every flush.
// Power-loss recovery is Task #46 and lives outside this module.
//
// Portable: stdio only, no ESP-IDF dependency. Works over ESP-IDF FATFS
// (fopen/fseek/fwrite on the SD mount) and on the host for tests.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "recorder_config.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    FILE *fp;
    uint32_t pcm_bytes;
    uint32_t sample_rate_hz;
    uint16_t channels;
    uint16_t bits_per_sample;
    bool open;
} wav_part_t;

// Build a 44-byte RIFF/WAVE PCM header for `pcm_bytes` of payload.
// Layout (little-endian): RIFF, ChunkSize=36+pcm, WAVE, fmt␣ (16, PCM),
// AudioFormat=1, channels, sample rate, byte rate, block align, bits,
// data, Subchunk2Size=pcm.
void wav_part_build_header(uint8_t header[RECORDER_WAV_HEADER_SIZE],
                           uint32_t pcm_bytes, uint32_t sample_rate_hz,
                           uint16_t channels, uint16_t bits_per_sample);

// Open (create/truncate) a `.part` file and write the placeholder header.
// Returns false when the path is NULL, lacks the `.part` suffix, or the
// file cannot be created. Callers treat failure as fail-loud ERROR.
bool wav_part_open(wav_part_t *part, const char *path,
                   uint32_t sample_rate_hz, uint16_t channels,
                   uint16_t bits_per_sample);

// Append PCM payload. An odd trailing byte is truncated to the 16bit
// sample boundary (only the tail is discarded, per Spec #36). Updates the
// payload counter. Returns false on write failure or when not open.
bool wav_part_write(wav_part_t *part, const uint8_t *pcm, size_t len);

// Patch ChunkSize/Subchunk2Size in place from the payload counter and
// flush to storage, WITHOUT closing: keeps the `.part` decodeable
// mid-recording. Returns false when not open or on I/O failure.
bool wav_part_flush(wav_part_t *part);

// Patch sizes and close. Safe to call on a non-open handle (no-op true).
// After a successful close the `.part` is a valid WAV stream.
bool wav_part_close(wav_part_t *part);

// Payload bytes accepted so far.
uint32_t wav_part_pcm_bytes(const wav_part_t *part);

#ifdef __cplusplus
}
#endif
