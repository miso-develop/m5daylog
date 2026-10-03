// Task #50: portable USB CDC session/command execution gate.

#include "usb_cdc_session_gate.h"

#include <stddef.h>

#define RX_STATE_OPEN (1u << 31)
#define RX_STATE_DIRTY (1u << 30)
#define RX_STATE_COUNT_MASK (RX_STATE_DIRTY - 1u)

static bool gate_has_pending_rx_discard(const usb_cdc_session_gate_t *gate) {
    return atomic_load_explicit(&gate->rx_discard_epoch,
                                memory_order_seq_cst) !=
           atomic_load_explicit(&gate->rx_drained_epoch,
                                memory_order_seq_cst);
}

static bool gate_rx_admission_is_open(const usb_cdc_session_gate_t *gate) {
    return atomic_load_explicit(&gate->rx_state, memory_order_seq_cst) ==
           RX_STATE_OPEN;
}

static void gate_try_clear_rx_dirty(usb_cdc_session_gate_t *gate) {
    uint32_t state;

    for (;;) {
        uint32_t desired;

        state = atomic_load_explicit(&gate->rx_state, memory_order_seq_cst);
        if ((state & RX_STATE_OPEN) != 0u ||
            (state & RX_STATE_DIRTY) == 0u ||
            (state & RX_STATE_COUNT_MASK) != 0u ||
            gate_has_pending_rx_discard(gate)) {
            return;
        }

        desired = state & ~RX_STATE_DIRTY;
        if (atomic_compare_exchange_weak_explicit(
                &gate->rx_state, &state, desired,
                memory_order_seq_cst, memory_order_seq_cst)) {
            return;
        }
    }
}

static void gate_mark_rx_boundary_stale(usb_cdc_session_gate_t *gate) {
    uint32_t state;

    // The boundary epoch is published before RX admission is closed. If a
    // worker reaches an empty read in between, that read is already after the
    // lifecycle cutoff and may legitimately satisfy this epoch.
    (void)atomic_fetch_add_explicit(&gate->rx_discard_epoch, 1u,
                                    memory_order_seq_cst);

    state = atomic_load_explicit(&gate->rx_state, memory_order_seq_cst);
    for (;;) {
        uint32_t desired = (state & RX_STATE_COUNT_MASK) | RX_STATE_DIRTY;
        if (atomic_compare_exchange_weak_explicit(
                &gate->rx_state, &state, desired,
                memory_order_seq_cst, memory_order_seq_cst)) {
            break;
        }
    }
    gate_try_clear_rx_dirty(gate);
}

static void gate_close_rx_admission(usb_cdc_session_gate_t *gate) {
    uint32_t expected = RX_STATE_OPEN;

    // RESETTING is followed by the transport-owned stale-queue note. If RX was
    // already closed/dirty, preserve that state and any in-flight classifiers.
    (void)atomic_compare_exchange_strong_explicit(
        &gate->rx_state, &expected, 0u,
        memory_order_seq_cst, memory_order_seq_cst);
}

void usb_cdc_session_gate_init(usb_cdc_session_gate_t *gate) {
    if (gate == NULL) {
        return;
    }
    atomic_init(&gate->generation, 1u);
    atomic_init(&gate->active_commands, 0u);
    atomic_init(&gate->phase, USB_CDC_SESSION_GATE_CLOSED);
    atomic_init(&gate->rx_state, 0u);
    atomic_init(&gate->rx_discard_epoch, 0u);
    atomic_init(&gate->rx_drained_epoch, 0u);
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
    return atomic_load_explicit(&gate->phase, memory_order_seq_cst) ==
               USB_CDC_SESSION_GATE_OPEN &&
           gate_rx_admission_is_open(gate) &&
           !gate_has_pending_rx_discard(gate);
}

bool usb_cdc_session_gate_open(usb_cdc_session_gate_t *gate) {
    uint32_t phase;
    uint32_t expected;

    if (gate == NULL) {
        return false;
    }

    // Phase OPEN means the lifecycle owns an opening/open session; RX_STATE_OPEN
    // is the actual CDC-ready publication point. Keeping these separate lets the
    // worker drain stale TinyUSB bytes while command admission remains closed.
    phase = atomic_load_explicit(&gate->phase, memory_order_seq_cst);
    if (phase == USB_CDC_SESSION_GATE_CLOSED) {
        expected = USB_CDC_SESSION_GATE_CLOSED;
        if (!atomic_compare_exchange_strong_explicit(
                &gate->phase, &expected, USB_CDC_SESSION_GATE_OPEN,
                memory_order_seq_cst, memory_order_seq_cst) &&
            expected != USB_CDC_SESSION_GATE_OPEN) {
            return false;
        }
    } else if (phase != USB_CDC_SESSION_GATE_OPEN) {
        return false;
    }

    if (gate_rx_admission_is_open(gate)) {
        return !gate_has_pending_rx_discard(gate);
    }

    // CLOSED-clean (zero) competes atomically with stale-RX callback
    // registration. If a callback registers first, this CAS fails and the
    // worker must drain its epoch. If OPEN wins first, the callback observes
    // RX_STATE_OPEN and its bytes belong to the new session. No callback waits.
    expected = 0u;
    return atomic_compare_exchange_strong_explicit(
        &gate->rx_state, &expected, RX_STATE_OPEN,
        memory_order_seq_cst, memory_order_seq_cst);
}

void usb_cdc_session_gate_close(usb_cdc_session_gate_t *gate) {
    uint32_t expected;
    if (gate == NULL) {
        return;
    }

    expected = USB_CDC_SESSION_GATE_OPEN;
    if (atomic_compare_exchange_strong_explicit(
            &gate->phase, &expected, USB_CDC_SESSION_GATE_CLOSED,
            memory_order_seq_cst, memory_order_seq_cst)) {
        (void)atomic_fetch_add_explicit(&gate->generation, 1u,
                                        memory_order_seq_cst);
        gate_mark_rx_boundary_stale(gate);
    }
}

usb_cdc_session_snapshot_t usb_cdc_session_gate_snapshot(
    const usb_cdc_session_gate_t *gate) {
    usb_cdc_session_snapshot_t snapshot = {0};
    uint32_t generation;

    if (gate == NULL) {
        return snapshot;
    }

    generation = atomic_load_explicit(&gate->generation,
                                      memory_order_seq_cst);
    snapshot.generation = generation;
    snapshot.admitted =
        atomic_load_explicit(&gate->phase, memory_order_seq_cst) ==
            USB_CDC_SESSION_GATE_OPEN &&
        gate_rx_admission_is_open(gate) &&
        !gate_has_pending_rx_discard(gate);

    // Make the snapshot self-invalidating if teardown moved either state after
    // the optimistic reads above.
    if (snapshot.admitted &&
        (atomic_load_explicit(&gate->phase, memory_order_seq_cst) !=
             USB_CDC_SESSION_GATE_OPEN ||
         atomic_load_explicit(&gate->generation, memory_order_seq_cst) !=
             generation ||
         !gate_rx_admission_is_open(gate) ||
         gate_has_pending_rx_discard(gate))) {
        snapshot.admitted = false;
    }
    return snapshot;
}

bool usb_cdc_session_gate_snapshot_is_current(
    const usb_cdc_session_gate_t *gate,
    usb_cdc_session_snapshot_t snapshot) {
    if (gate == NULL || !snapshot.admitted) {
        return false;
    }
    return atomic_load_explicit(&gate->phase, memory_order_seq_cst) ==
               USB_CDC_SESSION_GATE_OPEN &&
           atomic_load_explicit(&gate->generation, memory_order_seq_cst) ==
               snapshot.generation &&
           gate_rx_admission_is_open(gate) &&
           !gate_has_pending_rx_discard(gate);
}

void usb_cdc_session_gate_note_rx(usb_cdc_session_gate_t *gate) {
    uint32_t state;

    if (gate == NULL) {
        return;
    }

    state = atomic_load_explicit(&gate->rx_state, memory_order_seq_cst);
    for (;;) {
        uint32_t count;
        uint32_t desired;

        if ((state & RX_STATE_OPEN) != 0u) {
            return;
        }

        count = state & RX_STATE_COUNT_MASK;
        if (count == RX_STATE_COUNT_MASK) {
            // Practically unreachable, but remain fail-closed rather than wrap
            // the callback count into a value that could be opened.
            (void)atomic_fetch_add_explicit(&gate->rx_discard_epoch, 1u,
                                            memory_order_seq_cst);
            (void)atomic_fetch_or_explicit(&gate->rx_state, RX_STATE_DIRTY,
                                           memory_order_seq_cst);
            return;
        }

        desired = state + 1u;
        if (atomic_compare_exchange_weak_explicit(
                &gate->rx_state, &state, desired,
                memory_order_seq_cst, memory_order_seq_cst)) {
            break;
        }
    }

    // Registration above prevents OPEN from winning until this stale callback
    // has published its epoch and dirty marker. The final decrement makes the
    // closed state eligible for clear only after publication is complete.
    (void)atomic_fetch_add_explicit(&gate->rx_discard_epoch, 1u,
                                    memory_order_seq_cst);
    (void)atomic_fetch_or_explicit(&gate->rx_state, RX_STATE_DIRTY,
                                   memory_order_seq_cst);
    (void)atomic_fetch_sub_explicit(&gate->rx_state, 1u,
                                    memory_order_seq_cst);
}

uint32_t usb_cdc_session_gate_rx_discard_epoch(
    const usb_cdc_session_gate_t *gate) {
    if (gate == NULL) {
        return 0u;
    }
    return atomic_load_explicit(&gate->rx_discard_epoch,
                                memory_order_acquire);
}

bool usb_cdc_session_gate_should_discard_rx(
    const usb_cdc_session_gate_t *gate) {
    if (gate == NULL) {
        return true;
    }
    // Preserve the public meaning of this predicate: it reports an unresolved
    // stale-RX epoch, not whether lifecycle command admission is currently
    // closed. Snapshot/current checks independently enforce admission.
    return gate_has_pending_rx_discard(gate);
}

void usb_cdc_session_gate_mark_rx_drained(usb_cdc_session_gate_t *gate,
                                          uint32_t observed_epoch) {
    uint32_t current;
    if (gate == NULL) {
        return;
    }

    // Only advance to the epoch observed before the empty read. If a callback
    // increments rx_discard_epoch concurrently, that newer epoch remains
    // pending and requires another drain-to-empty pass.
    current = atomic_load_explicit(&gate->rx_drained_epoch,
                                   memory_order_acquire);
    while (current < observed_epoch &&
           !atomic_compare_exchange_weak_explicit(
               &gate->rx_drained_epoch, &current, observed_epoch,
               memory_order_release, memory_order_acquire)) {
    }
    gate_try_clear_rx_dirty(gate);
}

bool usb_cdc_session_gate_command_begin(usb_cdc_session_gate_t *gate,
                                        uint32_t frame_generation) {
    if (gate == NULL ||
        atomic_load_explicit(&gate->phase, memory_order_seq_cst) !=
            USB_CDC_SESSION_GATE_OPEN ||
        atomic_load_explicit(&gate->generation, memory_order_seq_cst) !=
            frame_generation ||
        !gate_rx_admission_is_open(gate) ||
        gate_has_pending_rx_discard(gate)) {
        return false;
    }

    (void)atomic_fetch_add_explicit(&gate->active_commands, 1u,
                                    memory_order_seq_cst);

    // Reset may race between the optimistic admission check and the active
    // count increment. Re-check after joining the active set: either reset sees
    // us and waits, or we observe the closed/new generation and back out before
    // any command-side mutation occurs.
    if (atomic_load_explicit(&gate->phase, memory_order_seq_cst) !=
            USB_CDC_SESSION_GATE_OPEN ||
        atomic_load_explicit(&gate->generation, memory_order_seq_cst) !=
            frame_generation ||
        !gate_rx_admission_is_open(gate) ||
        gate_has_pending_rx_discard(gate)) {
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

    // RESETTING is the teardown ownership state. gate_open() cannot change it,
    // so a reconnect/RX race cannot reopen admission while this function is
    // establishing and waiting on the cutoff. RX admission is atomically closed
    // without spinning; reset_session() then marks the transport queue stale.
    atomic_store_explicit(&gate->phase, USB_CDC_SESSION_GATE_RESETTING,
                          memory_order_seq_cst);
    gate_close_rx_admission(gate);
    (void)atomic_fetch_add_explicit(&gate->generation, 1u,
                                    memory_order_seq_cst);

    while (atomic_load_explicit(&gate->active_commands,
                                memory_order_acquire) != 0u) {
        if (wait_fn != NULL) {
            wait_fn(wait_ctx);
        }
    }

    atomic_store_explicit(&gate->phase, USB_CDC_SESSION_GATE_CLOSED,
                          memory_order_release);
}
