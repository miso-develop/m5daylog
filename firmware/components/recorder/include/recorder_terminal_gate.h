#pragma once

// #119: terminal WAV publication admission. Portable for native fault tests.
// Caller must hold the recorder lifecycle/ownership gate and wait for
// CAPTURE_DONE before sealing, draining, finalizing or indexing the segment.

#include <stdbool.h>
#include "pcm_pipeline.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool capture_started;   // mic init succeeded; fresh RECORDING_READY
    bool capture_done;      // producer/I2S teardown completed
    bool writer_io_ok;      // sticky false after ANY earlier write/flush error
    bool lifecycle_healthy; // no ERROR / invalid storage ownership
} recorder_terminal_gate_t;

// Authorize healthy WAV+manifest publication. Valid genuinely empty sessions
// are allowed only when capture actually started; a header alone proves none
// of these conditions. Never turn a failed .part into a healthy .wav.
bool recorder_terminal_can_finalize(const recorder_terminal_gate_t *gate,
                                    const pcm_pipeline_t *pipeline);

#ifdef __cplusplus
}
#endif
