// #119: shared production/host terminal publication admission policy.
// Positive media writes alone do not prove a healthy recording session.
#include "recorder_terminal_gate.h"

bool recorder_terminal_can_finalize(const recorder_terminal_gate_t *gate,
                                    const pcm_pipeline_t *pipeline) {
    const recorder_counters_t *counts;
    if (gate == NULL || pipeline == NULL ||
        !gate->capture_started || !gate->capture_done ||
        !gate->writer_io_ok || !gate->lifecycle_healthy) {
        return false;
    }
    counts = pcm_pipeline_counters(pipeline);
    if (counts == NULL) {
        return false;
    }
    // A completed I2S read with a timeout but no loss is not a fault.
    // Actual DMA loss, staging loss, or ANY earlier sink/checkpoint failure
    // denies a healthy finalized WAV, regardless of subsequent I/O success.
    return counts->dma_overrun_events == 0 &&
           counts->dma_drop_bytes == 0 &&
           counts->buffer_overflow == 0 &&
           counts->buffer_drop_bytes == 0 &&
           counts->sd_write_errors == 0;
}
