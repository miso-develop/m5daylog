#pragma once

// Task #50 portable USB CDC session/command execution gate.
//
// A frame is tagged with the session generation in which its first byte was
// consumed. Reset closes admission, invalidates that generation, and waits for
// commands that already crossed the gate. This gives the detach path a precise
// cutoff before remount -> RTC-event flush -> recording restart.

#include <stdbool.h>
#include <stdint.h>
#include <stdatomic.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*usb_cdc_session_gate_wait_fn_t)(void *ctx);

typedef struct {
    _Atomic uint32_t generation;
    _Atomic uint32_t active_commands;
    _Atomic bool accepting;
} usb_cdc_session_gate_t;

void usb_cdc_session_gate_init(usb_cdc_session_gate_t *gate);
uint32_t usb_cdc_session_gate_generation(const usb_cdc_session_gate_t *gate);
bool usb_cdc_session_gate_is_open(const usb_cdc_session_gate_t *gate);

// Open command admission for the current generation. This does not create a
// new generation; only close/reset invalidates already-framed commands.
void usb_cdc_session_gate_open(usb_cdc_session_gate_t *gate);

// Non-blocking close for callback context. Already running commands may finish,
// but old frames are immediately invalidated by the generation increment.
void usb_cdc_session_gate_close(usb_cdc_session_gate_t *gate);

// Enter the execution region for a frame tagged with frame_generation.
// Returns false if reset/close won the race or the frame belongs to an older
// session. Every true return must be paired with command_end().
bool usb_cdc_session_gate_command_begin(usb_cdc_session_gate_t *gate,
                                        uint32_t frame_generation);
void usb_cdc_session_gate_command_end(usb_cdc_session_gate_t *gate);

// Blocking physical-session teardown barrier. Admission is closed and the
// current generation invalidated before waiting. When this returns, no command
// from the pre-reset generation can still be executing or begin later.
void usb_cdc_session_gate_reset(usb_cdc_session_gate_t *gate,
                                usb_cdc_session_gate_wait_fn_t wait_fn,
                                void *wait_ctx);

#ifdef __cplusplus
}
#endif
