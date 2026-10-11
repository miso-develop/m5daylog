#pragma once

// Task #50 portable USB CDC session/command execution gate.
//
// Command admission is owned by the recorder lifecycle, not by CDC transport
// callbacks. Every RX batch is bound to the gate generation/state captured
// before the TinyUSB read, and reset establishes a non-reopenable teardown
// phase until it has waited for all admitted commands to finish.

#include <stdbool.h>
#include <stdint.h>
#include <stdatomic.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*usb_cdc_session_gate_wait_fn_t)(void *ctx);

typedef enum {
    USB_CDC_SESSION_GATE_CLOSED = 0,
    USB_CDC_SESSION_GATE_OPEN = 1,
    USB_CDC_SESSION_GATE_RESETTING = 2,
} usb_cdc_session_gate_phase_t;

typedef struct {
    uint32_t generation;
    bool admitted;
} usb_cdc_session_snapshot_t;

typedef struct {
    _Atomic uint32_t generation;
    _Atomic uint32_t active_commands;
    _Atomic uint32_t phase;
    // Lock-free RX admission word. The high bits encode whether RX belongs to
    // the published session and whether stale RX still needs a drain-to-empty;
    // the low bits count callbacks that have atomically joined stale-RX
    // classification. This lets lifecycle OPEN race callbacks without making a
    // TinyUSB callback wait on another task.
    _Atomic uint32_t rx_state;
    _Atomic uint32_t rx_discard_epoch;
    _Atomic uint32_t rx_drained_epoch;
} usb_cdc_session_gate_t;

void usb_cdc_session_gate_init(usb_cdc_session_gate_t *gate);
uint32_t usb_cdc_session_gate_generation(const usb_cdc_session_gate_t *gate);
bool usb_cdc_session_gate_is_open(const usb_cdc_session_gate_t *gate);

// Open command admission only from the recorder lifecycle's CDC-ready point.
// Returns false while RESETTING or while stale RX still requires a
// drain-to-empty pass. RX callbacks and OPEN compete on one atomic admission
// word, so a callback is classified wholly before or wholly after CDC-ready.
bool usb_cdc_session_gate_open(usb_cdc_session_gate_t *gate);

// Non-blocking close for callback context. Already running commands may finish,
// but old frames are immediately invalidated by the generation increment.
void usb_cdc_session_gate_close(usb_cdc_session_gate_t *gate);

// Snapshot command-admission identity before acquiring an RX batch. A batch
// may be consumed only while this snapshot remains current.
usb_cdc_session_snapshot_t usb_cdc_session_gate_snapshot(
    const usb_cdc_session_gate_t *gate);
bool usb_cdc_session_gate_snapshot_is_current(
    const usb_cdc_session_gate_t *gate,
    usb_cdc_session_snapshot_t snapshot);

// Track bytes signalled while the lifecycle has command admission closed. The
// worker clears only the epoch it observed before an empty TinyUSB read, so a
// concurrent later RX notification remains pending for another drain pass.
void usb_cdc_session_gate_note_rx(usb_cdc_session_gate_t *gate);
uint32_t usb_cdc_session_gate_rx_discard_epoch(
    const usb_cdc_session_gate_t *gate);
bool usb_cdc_session_gate_should_discard_rx(
    const usb_cdc_session_gate_t *gate);
void usb_cdc_session_gate_mark_rx_drained(usb_cdc_session_gate_t *gate,
                                          uint32_t observed_epoch);

// Enter the execution region for a frame tagged with frame_generation.
// Returns false if reset/close won the race or the frame belongs to an older
// session. Every true return must be paired with command_end().
bool usb_cdc_session_gate_command_begin(usb_cdc_session_gate_t *gate,
                                        uint32_t frame_generation);
void usb_cdc_session_gate_command_end(usb_cdc_session_gate_t *gate);

// Blocking physical-session teardown barrier. RESETTING is published before
// generation invalidation and remains published while active commands drain;
// therefore a racing reopen cannot defeat the cutoff. On return the gate is
// CLOSED and must be explicitly reopened by the owning recorder lifecycle.
void usb_cdc_session_gate_reset(usb_cdc_session_gate_t *gate,
                                usb_cdc_session_gate_wait_fn_t wait_fn,
                                void *wait_ctx);

#ifdef __cplusplus
}
#endif
