#pragma once

// Portable ping-pong double buffer + failure counters — Task #44.
//
// No ESP-IDF dependency: the I2S/DMA producer calls pcm_pipeline_produce()
// with freshly captured PCM bytes, and the SD writer drains full slots via
// peek/release. When both slots are full, incoming bytes are DROPPED and
// counted (buffer_overflow / buffer_drop_*) — never silently overwritten.
// Driver DMA loss (dma_*) and software buffer loss (buffer_*) are strictly
// separate counter families. This is the Spec #36 "no silent failure" rule
// in code.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "recorder_config.h"

#ifdef __cplusplus
extern "C" {
#endif

// Cumulative diagnostics for the Task #44 completion criteria
// (DMA/sample drop, buffer overflow, SD write health).
//
// Overrun/drop semantics (Spec #36, no silent failure, no fabricated
// evidence, no double counting):
// - `dma_overrun_events` / `dma_drop_bytes` / `dma_drop_samples` arrive
//   ONLY from the ESP-IDF I2S RX queue-overflow callback
//   (`i2s_event_data_t.size` per event). Every callback is preserved and
//   accumulated exactly: event count, dropped bytes, and whole samples.
//   Callbacks are never collapsed into a single flag.
// - `buffer_overflow` / `buffer_drop_bytes` / `buffer_drop_samples` count
//   ONLY software ping-pong loss: payload dropped by pcm_pipeline_produce()
//   while both 32KB slots were full. Driver overflow bytes must never be
//   added here and software drops must never be added to dma_*.
// - `dma_read_stalls`: I2S reads that timed out or returned short WITHOUT
//   a driver overflow event. A stall is transport evidence, never loss
//   evidence: it must not be counted as dropped audio.
typedef struct {
    uint32_t samples_captured;   // whole 16bit samples accepted
    uint32_t chunks_written;     // full slots drained to SD
    uint32_t buffer_overflow;    // software both-full produce events
    uint32_t buffer_drop_bytes;  // software-dropped payload bytes
    uint32_t buffer_drop_samples;  // whole samples among software drops
    uint32_t dma_overrun_events;  // driver queue-overflow callbacks
    uint32_t dma_drop_bytes;     // driver-dropped payload bytes
    uint32_t dma_drop_samples;   // whole samples among driver drops
    uint32_t dma_read_stalls;    // read timeouts/short reads w/o overflow
    uint32_t sd_write_errors;    // failed SD chunk writes (fail-loud)
    uint32_t max_sd_latency_us;  // worst single-slot SD write latency
} recorder_counters_t;

typedef struct {
    uint8_t *slot[RECORDER_BUFFER_SLOTS];
    size_t fill[RECORDER_BUFFER_SLOTS];
    bool full[RECORDER_BUFFER_SLOTS];
    unsigned read_index;   // oldest full slot
    unsigned write_index;  // active fill slot
    recorder_counters_t counters;
} pcm_pipeline_t;

// Backing buffers are caller-provided (static DRAM in firmware, stack/heap
// in host tests) so this module never allocates.
void pcm_pipeline_init(pcm_pipeline_t *pipeline, uint8_t *backing0,
                       uint8_t *backing1);

// Append captured PCM. Whole samples are packed into the active fill slot;
// when a slot reaches RECORDER_BUFFER_BYTES it becomes full and the writer
// flips to the other slot. If both slots are full, the excess is dropped
// and counted. Returns the number of bytes dropped (0 on the fast path).
// An odd trailing byte is held back (not counted as captured or dropped)
// so sample framing is never split; the caller should prepend it to the
// next produce call.
size_t pcm_pipeline_produce(pcm_pipeline_t *pipeline, const uint8_t *data,
                            size_t len);

// True when at least one full slot is ready for the SD writer.
bool pcm_pipeline_has_full(const pcm_pipeline_t *pipeline);

// Oldest full slot payload (always RECORDER_BUFFER_BYTES). NULL when none.
const uint8_t *pcm_pipeline_peek_full(const pcm_pipeline_t *pipeline,
                                      size_t *out_len);

// Release the oldest full slot after a successful SD write. Increments
// chunks_written. No-op when no full slot exists.
void pcm_pipeline_release_full(pcm_pipeline_t *pipeline);

// Record one completed SD slot write (latency in microseconds).
void pcm_pipeline_note_sd_write(pcm_pipeline_t *pipeline,
                                uint32_t latency_us);

// Accumulate one drain of driver queue-overflow evidence: `events`
// callbacks carrying `drop_bytes` of driver-dropped payload. Exact sums,
// never collapsed: events add to dma_overrun_events, bytes (and their
// whole samples) add to dma_drop_*.
void pcm_pipeline_note_driver_overflow(pcm_pipeline_t *pipeline,
                                       uint32_t events,
                                       uint32_t drop_bytes);

// Record one I2S read timeout/short read with NO driver overflow event.
// Stall evidence only — never added to drop counters.
void pcm_pipeline_note_read_stall(pcm_pipeline_t *pipeline);

// Record one failed SD slot write (fail-loud counter).
void pcm_pipeline_note_sd_error(pcm_pipeline_t *pipeline);

// Read-only snapshot of the cumulative counters.
const recorder_counters_t *pcm_pipeline_counters(
    const pcm_pipeline_t *pipeline);

#ifdef __cplusplus
}
#endif
