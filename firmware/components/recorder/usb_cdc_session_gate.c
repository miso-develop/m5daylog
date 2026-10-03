// Task #50: portable USB CDC session/command execution gate.

#include "usb_cdc_session_gate.h"

#include <stddef.h>

void usb_cdc_session_gate_init(usb_cdc_session_gate_t *gate) {
    if (gate == NULL) {
        return;
    }
    atomic_init(&gate->generation, 1u);
    atomic_init(&gate->active_commands, 0u);
    atomic_init(&gate->accepting, false);
}

uint32_t usb_cdc_session_gate_generation(const usb_cdc_session_gate_t *gate) {
    if (gate == NULL) {
        return 0u;
    }
    return atomic_load_explicit(&gate->generation, memory_order_acquire);
}

bool usb_cdc_session_gate_is_open(const usb_cdc_session_gate_t *gate) {
    if (gate == NULL) {
        return false;
    }
    return atomic_load_explicit(&gate->accepting, memory_order_acquire);
}

void usb_cdc_session_gate_open(usb_cdc_session_gate_t *gate) {
    if (gate == NULL) {
        return;
    }
    atomic_store_explicit(&gate->accepting, true, memory_order_release);
}

void usb_cdc_session_gate_close(usb_cdc_session_gate_t *gate) {
    if (gate == NULL) {
        return;
    }
    atomic_store_explicit(&gate->accepting, false, memory_order_seq_cst);
    (void)atomic_fetch_add_explicit(&gate->generation, 1u,
                                    memory_order_seq_cst);
}

bool usb_cdc_session_gate_command_begin(usb_cdc_session_gate_t *gate,
                                        uint32_t frame_generation) {
    if (gate == NULL ||
        !atomic_load_explicit(&gate->accepting, memory_order_seq_cst) ||
        atomic_load_explicit(&gate->generation, memory_order_seq_cst) !=
            frame_generation) {
        return false;
    }

    (void)atomic_fetch_add_explicit(&gate->active_commands, 1u,
                                    memory_order_seq_cst);

    // Reset may race between the optimistic admission check and the active
    // count increment. Re-check after joining the active set: either reset sees
    // us and waits, or we observe the closed/new generation and back out before
    // any command-side mutation occurs.
    if (!atomic_load_explicit(&gate->accepting, memory_order_seq_cst) ||
        atomic_load_explicit(&gate->generation, memory_order_seq_cst) !=
            frame_generation) {
        (void)atomic_fetch_sub_explicit(&gate->active_commands, 1u,
                                        memory_order_seq_cst);
        return false;
    }
    return true;
}

void usb_cdc_session_gate_command_end(usb_cdc_session_gate_t *gate) {
    if (gate == NULL) {
        return;
    }
    (void)atomic_fetch_sub_explicit(&gate->active_commands, 1u,
                                    memory_order_release);
}

void usb_cdc_session_gate_reset(usb_cdc_session_gate_t *gate,
                                usb_cdc_session_gate_wait_fn_t wait_fn,
                                void *wait_ctx) {
    if (gate == NULL) {
        return;
    }

    atomic_store_explicit(&gate->accepting, false, memory_order_seq_cst);
    (void)atomic_fetch_add_explicit(&gate->generation, 1u,
                                    memory_order_seq_cst);

    while (atomic_load_explicit(&gate->active_commands,
                                memory_order_acquire) != 0u) {
        if (wait_fn != NULL) {
            wait_fn(wait_ctx);
        }
    }
}
