// Portable ping-pong double buffer — Task #44.
//
// Depends only on recorder_config.h / pcm_pipeline.h (no ESP-IDF), so host
// contract tests compile this file directly.

#include "pcm_pipeline.h"

void pcm_pipeline_init(pcm_pipeline_t *pipeline, uint8_t *backing0,
                       uint8_t *backing1) {
    if (pipeline == NULL) {
        return;
    }
    pipeline->slot[0] = backing0;
    pipeline->slot[1] = backing1;
    pipeline->fill[0] = 0;
    pipeline->fill[1] = 0;
    pipeline->full[0] = false;
    pipeline->full[1] = false;
    pipeline->read_index = 0;
    pipeline->write_index = 0;
    pipeline->counters.samples_captured = 0;
    pipeline->counters.chunks_written = 0;
    pipeline->counters.buffer_overflow = 0;
    pipeline->counters.dma_drop_bytes = 0;
    pipeline->counters.dma_drop_samples = 0;
    pipeline->counters.sd_write_errors = 0;
    pipeline->counters.max_sd_latency_us = 0;
}

size_t pcm_pipeline_produce(pcm_pipeline_t *pipeline, const uint8_t *data,
                            size_t len) {
    if (pipeline == NULL || data == NULL || len == 0) {
        return 0;
    }
    if (pipeline->slot[0] == NULL || pipeline->slot[1] == NULL) {
        return len;
    }

    // Keep 16bit sample framing: hold back a lone trailing byte so samples
    // are never split across produce calls.
    size_t usable = len - (len % RECORDER_BYTES_PER_SAMPLE);
    size_t offset = 0;

    while (offset < usable) {
        // Both slots full: nothing can be staged — drop the remainder and
        // count it. This is the DMA overrun/drop signal (fail-loud, never
        // silent overwrite).
        if (pipeline->full[0] && pipeline->full[1]) {
            size_t rest = usable - offset;
            pipeline->counters.buffer_overflow++;
            pipeline->counters.dma_drop_bytes += (uint32_t)rest;
            pipeline->counters.dma_drop_samples +=
                (uint32_t)(rest / RECORDER_BYTES_PER_SAMPLE);
            return len - offset;
        }

        // Advance past a full active slot (the other one must be free here
        // because the both-full case returned above).
        if (pipeline->full[pipeline->write_index]) {
            pipeline->write_index = (unsigned)(1u - pipeline->write_index);
        }

        unsigned w = pipeline->write_index;
        size_t room = RECORDER_BUFFER_BYTES - pipeline->fill[w];
        size_t rest = usable - offset;
        size_t take = rest < room ? rest : room;

        for (size_t i = 0; i < take; i++) {
            pipeline->slot[w][pipeline->fill[w] + i] = data[offset + i];
        }
        pipeline->fill[w] += take;
        offset += take;
        pipeline->counters.samples_captured +=
            (uint32_t)(take / RECORDER_BYTES_PER_SAMPLE);

        if (pipeline->fill[w] == RECORDER_BUFFER_BYTES) {
            pipeline->full[w] = true;
            pipeline->write_index = (unsigned)(1u - w);
        }
    }

    // A held-back odd byte is neither captured nor dropped.
    return len - offset;
}

bool pcm_pipeline_has_full(const pcm_pipeline_t *pipeline) {
    if (pipeline == NULL) {
        return false;
    }
    return pipeline->full[0] || pipeline->full[1];
}

const uint8_t *pcm_pipeline_peek_full(const pcm_pipeline_t *pipeline,
                                      size_t *out_len) {
    if (pipeline == NULL) {
        return NULL;
    }
    unsigned index = pipeline->read_index;
    if (!pipeline->full[index]) {
        index = (unsigned)(1u - index);
        if (!pipeline->full[index]) {
            return NULL;
        }
    }
    if (out_len != NULL) {
        *out_len = RECORDER_BUFFER_BYTES;
    }
    return pipeline->slot[index];
}

void pcm_pipeline_release_full(pcm_pipeline_t *pipeline) {
    if (pipeline == NULL) {
        return;
    }
    unsigned index = pipeline->read_index;
    if (!pipeline->full[index]) {
        index = (unsigned)(1u - index);
        if (!pipeline->full[index]) {
            return;
        }
    }
    pipeline->full[index] = false;
    pipeline->fill[index] = 0;
    pipeline->read_index = (unsigned)(1u - index);
    pipeline->counters.chunks_written++;
}

void pcm_pipeline_note_sd_write(pcm_pipeline_t *pipeline,
                                uint32_t latency_us) {
    if (pipeline == NULL) {
        return;
    }
    if (latency_us > pipeline->counters.max_sd_latency_us) {
        pipeline->counters.max_sd_latency_us = latency_us;
    }
}

void pcm_pipeline_note_sd_error(pcm_pipeline_t *pipeline) {
    if (pipeline == NULL) {
        return;
    }
    pipeline->counters.sd_write_errors++;
}

const recorder_counters_t *pcm_pipeline_counters(
    const pcm_pipeline_t *pipeline) {
    if (pipeline == NULL) {
        return NULL;
    }
    return &pipeline->counters;
}
