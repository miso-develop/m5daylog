#pragma once

// microSD `.wav.part` file sink + write-latency diagnostics — Task #44.
//
// Owns one open `.wav.part` file (see wav_part.h). Each drained 32KB slot
// from the double buffer is appended via sd_pcm_sink_write_chunk(), which
// returns false on any I/O failure so the caller goes fail-loud (ERROR,
// never a silent recording state). Per-chunk latency is reported so the
// Task #44 evidence can show SD overflow health (target: overflow 0).
//
// Mounting is the sibling `sd_mount` module's job (`main.c` mounts before
// opening the sink): the caller passes an absolute path on the mounted
// filesystem.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "recorder_config.h"
#include "wav_part.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    wav_part_t wav;
    bool open;
    uint32_t chunks_written;
    uint32_t write_errors;
    uint32_t max_latency_us;
} sd_pcm_sink_t;

// Create/truncate `path` (must end in `.part`) and write the placeholder
// WAV header. False = fail-loud open error.
bool sd_pcm_sink_open(sd_pcm_sink_t *sink, const char *path);

// Append one drained pipeline slot. `len` must equal
// RECORDER_BUFFER_BYTES. On success increments chunks_written, tracks the
// worst latency in max_latency_us, and returns true. On failure increments
// write_errors and returns false.
bool sd_pcm_sink_write_chunk(sd_pcm_sink_t *sink, const uint8_t *data,
                             size_t len, uint32_t *out_latency_us);

// Patch header sizes mid-recording so the `.part` stays decodeable.
bool sd_pcm_sink_flush(sd_pcm_sink_t *sink);

// Patch header sizes and close. Safe on non-open handles (no-op true).
// Never deletes or renames: finalize/rotation is Task #45.
bool sd_pcm_sink_close(sd_pcm_sink_t *sink);

#ifdef __cplusplus
}
#endif
