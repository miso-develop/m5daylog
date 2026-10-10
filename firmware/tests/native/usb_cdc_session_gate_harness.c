#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "usb_cdc_session_gate.h"

#define TEST_RX_STATE_OPEN (1u << 31)
#define TEST_RX_STATE_OPENING (1u << 29)

static usb_cdc_session_gate_t s_gate;
static atomic_bool s_command_entered;
static atomic_bool s_release_command;
static atomic_bool s_pending;
static atomic_bool s_reset_waiting;
static atomic_bool s_reset_done;
static uint32_t s_frame_generation;

static void fail(const char *message) {
    fprintf(stderr, "FAIL: %s\n", message);
    exit(1);
}

static void spin_until(atomic_bool *value) {
    unsigned spins = 0u;
    while (!atomic_load(value)) {
        if (++spins > 10000000u) {
            fail("synchronization timeout");
        }
        sched_yield();
    }
}

static void *command_thread(void *arg) {
    (void)arg;
    if (!usb_cdc_session_gate_command_begin(&s_gate, s_frame_generation)) {
        fail("pre-reset command was unexpectedly rejected");
    }
    atomic_store(&s_command_entered, true);
    while (!atomic_load(&s_release_command)) {
        sched_yield();
    }

    /* Model the SET_TIME RTC/NVS commit while the production command gate is
       held. The detach path is forbidden to cross the barrier before this. */
    atomic_store(&s_pending, true);
    usb_cdc_session_gate_command_end(&s_gate);
    return NULL;
}

static void reset_wait(void *ctx) {
    (void)ctx;
    atomic_store(&s_reset_waiting, true);
    sched_yield();
}

static void *reset_thread(void *arg) {
    (void)arg;
    usb_cdc_session_gate_reset(&s_gate, reset_wait, NULL);
    atomic_store(&s_reset_done, true);
    return NULL;
}

static void prepare_blocked_reset(pthread_t *command, pthread_t *reset) {
    usb_cdc_session_gate_init(&s_gate);
    usb_cdc_session_gate_open(&s_gate);
    s_frame_generation = usb_cdc_session_gate_generation(&s_gate);
    atomic_store(&s_command_entered, false);
    atomic_store(&s_release_command, false);
    atomic_store(&s_pending, false);
    atomic_store(&s_reset_waiting, false);
    atomic_store(&s_reset_done, false);

    if (pthread_create(command, NULL, command_thread, NULL) != 0) {
        fail("pthread_create command");
    }
    spin_until(&s_command_entered);

    if (pthread_create(reset, NULL, reset_thread, NULL) != 0) {
        fail("pthread_create reset");
    }
    spin_until(&s_reset_waiting);
}

static void test_inflight_command_completes_before_reset_returns(void) {
    pthread_t command;
    pthread_t reset;

    prepare_blocked_reset(&command, &reset);
    if (atomic_load(&s_reset_done)) {
        fail("reset returned while SET_TIME was still in flight");
    }
    if (usb_cdc_session_gate_is_open(&s_gate)) {
        fail("reset did not close command admission before waiting");
    }

    atomic_store(&s_release_command, true);
    pthread_join(command, NULL);
    pthread_join(reset, NULL);

    if (!atomic_load(&s_reset_done) || !atomic_load(&s_pending)) {
        fail("completed SET_TIME was not visible at reset boundary");
    }

    /* This represents remount -> pending-event flush -> recording restart.
       Because the command thread is joined before reset returns, no later
       command mutation can recreate pending after this cutoff. */
    atomic_store(&s_pending, false);
    if (atomic_load(&s_pending)) {
        fail("pending remained after simulated detach flush");
    }
}

static void test_reset_cannot_be_reopened_while_cutoff_is_in_progress(void) {
    pthread_t command;
    pthread_t reset;
    uint32_t current_generation;

    prepare_blocked_reset(&command, &reset);

    /* Model a reconnect/RX callback racing the blocking physical reset. The
       lifecycle must own reopening, so this attempt must not defeat reset. */
    usb_cdc_session_gate_open(&s_gate);
    if (usb_cdc_session_gate_is_open(&s_gate)) {
        fail("concurrent reopen defeated reset-in-progress cutoff");
    }

    atomic_store(&s_release_command, true);
    pthread_join(command, NULL);
    pthread_join(reset, NULL);
    if (usb_cdc_session_gate_is_open(&s_gate)) {
        fail("reset returned with command admission reopened");
    }

    current_generation = usb_cdc_session_gate_generation(&s_gate);
    if (usb_cdc_session_gate_command_begin(&s_gate, current_generation)) {
        usb_cdc_session_gate_command_end(&s_gate);
        fail("command executed before explicit lifecycle reopen");
    }

    usb_cdc_session_gate_open(&s_gate);
    if (!usb_cdc_session_gate_command_begin(&s_gate, current_generation)) {
        fail("explicit lifecycle reopen did not enable the current session");
    }
    usb_cdc_session_gate_command_end(&s_gate);
}

static void test_reset_cancels_an_open_publication_reserved_before_cutoff(void) {
    uint32_t delayed_expected;
    uint32_t discard_before;

    usb_cdc_session_gate_init(&s_gate);

    /* White-box the exact race boundary: an opener has reserved the clean RX
       admission word but has not yet published CDC-ready. Reset must cancel
       that reservation so a delayed final CAS cannot reopen transport after
       the physical-session cutoff. */
    atomic_store(&s_gate.rx_state, TEST_RX_STATE_OPENING);
    usb_cdc_session_gate_reset(&s_gate, NULL, NULL);

    delayed_expected = TEST_RX_STATE_OPENING;
    if (atomic_compare_exchange_strong(&s_gate.rx_state, &delayed_expected,
                                       TEST_RX_STATE_OPEN)) {
        fail("delayed opener published CDC-ready after reset cutoff");
    }

    /* The transport's mandatory post-reset queue note must still classify RX
       as stale rather than observing a resurrected OPEN admission word. */
    discard_before = usb_cdc_session_gate_rx_discard_epoch(&s_gate);
    usb_cdc_session_gate_note_rx(&s_gate);
    if (usb_cdc_session_gate_rx_discard_epoch(&s_gate) == discard_before) {
        fail("post-reset RX was not classified stale after opening race");
    }
}

static void test_pre_reset_frame_cannot_execute_after_new_session_opens(void) {
    uint32_t stale_generation;

    usb_cdc_session_gate_init(&s_gate);
    usb_cdc_session_gate_open(&s_gate);
    stale_generation = usb_cdc_session_gate_generation(&s_gate);

    usb_cdc_session_gate_reset(&s_gate, NULL, NULL);
    atomic_store(&s_pending, false); /* detach flush/restart cutoff */
    usb_cdc_session_gate_open(&s_gate); /* later lifecycle-owned session */

    if (usb_cdc_session_gate_command_begin(&s_gate, stale_generation)) {
        atomic_store(&s_pending, true);
        usb_cdc_session_gate_command_end(&s_gate);
        fail("stale complete request executed after reset/new-session cutoff");
    }
    if (atomic_load(&s_pending)) {
        fail("stale request recreated pending after detach flush");
    }
}

static void test_acquired_batch_snapshot_is_invalid_after_reset_and_reopen(void) {
    usb_cdc_session_snapshot_t snapshot;

    usb_cdc_session_gate_init(&s_gate);
    usb_cdc_session_gate_open(&s_gate);

    /* The worker takes this snapshot before dequeuing a TinyUSB RX batch. */
    snapshot = usb_cdc_session_gate_snapshot(&s_gate);
    if (!usb_cdc_session_gate_snapshot_is_current(&s_gate, snapshot)) {
        fail("fresh open-session RX snapshot was unexpectedly invalid");
    }

    usb_cdc_session_gate_reset(&s_gate, NULL, NULL);
    usb_cdc_session_gate_open(&s_gate);
    if (usb_cdc_session_gate_snapshot_is_current(&s_gate, snapshot)) {
        fail("old-session RX batch was retagged as the reopened generation");
    }
}

static void test_callback_close_invalidates_generation_before_reconnect(void) {
    uint32_t old_generation;
    uint32_t discard_epoch;
    usb_cdc_session_snapshot_t old_snapshot;

    usb_cdc_session_gate_init(&s_gate);
    if (!usb_cdc_session_gate_open(&s_gate)) {
        fail("initial session did not open");
    }
    old_generation = usb_cdc_session_gate_generation(&s_gate);
    old_snapshot = usb_cdc_session_gate_snapshot(&s_gate);
    if (!usb_cdc_session_gate_command_begin(&s_gate, old_generation)) {
        fail("old-session command was not admitted");
    }

    /* This is the callback-safe DETACHED seam used by production: close must
       return immediately even with an active command, while invalidating both
       RX snapshots and TX generation identity before physical reconnect. */
    usb_cdc_session_gate_close(&s_gate);
    if (usb_cdc_session_gate_is_open(&s_gate)) {
        fail("callback close left CDC admission open");
    }
    if (usb_cdc_session_gate_snapshot_is_current(&s_gate, old_snapshot)) {
        fail("old RX snapshot survived physical detach cutoff");
    }
    if (usb_cdc_session_gate_generation(&s_gate) == old_generation) {
        fail("physical detach did not invalidate CDC generation");
    }

    usb_cdc_session_gate_command_end(&s_gate);
    usb_cdc_session_gate_reset(&s_gate, NULL, NULL);

    /* Drain the synthetic stale boundary before the coordinator may reopen. */
    discard_epoch = usb_cdc_session_gate_rx_discard_epoch(&s_gate);
    usb_cdc_session_gate_mark_rx_drained(&s_gate, discard_epoch);
    if (!usb_cdc_session_gate_open(&s_gate)) {
        fail("fresh session did not reopen after stale RX drain");
    }
    if (usb_cdc_session_gate_command_begin(&s_gate, old_generation)) {
        usb_cdc_session_gate_command_end(&s_gate);
        fail("pre-detach generation executed after reconnect");
    }
}

static void test_lifecycle_open_waits_for_stale_rx_drain_before_new_request(void) {
    uint32_t discard_epoch;
    uint32_t current_generation;
    usb_cdc_session_snapshot_t snapshot;

    usb_cdc_session_gate_init(&s_gate);

    /* Model queued stale bytes left by the previous physical session. CDC-ready
       must not become visible while that old queue still needs drain-to-empty. */
    usb_cdc_session_gate_note_rx(&s_gate);
    discard_epoch = usb_cdc_session_gate_rx_discard_epoch(&s_gate);
    if (!usb_cdc_session_gate_should_discard_rx(&s_gate)) {
        fail("pre-ready RX was not marked for discard");
    }

    usb_cdc_session_gate_open(&s_gate);
    if (usb_cdc_session_gate_is_open(&s_gate)) {
        fail("CDC-ready opened before stale RX drain completed");
    }

    /* The worker reaches an empty read and clears exactly the observed stale
       epoch. Only then may lifecycle publication make later RX executable. */
    usb_cdc_session_gate_mark_rx_drained(&s_gate, discard_epoch);
    if (usb_cdc_session_gate_should_discard_rx(&s_gate)) {
        fail("drained pre-ready RX remained permanently blocked");
    }

    usb_cdc_session_gate_open(&s_gate);
    if (!usb_cdc_session_gate_is_open(&s_gate)) {
        fail("CDC-ready did not open after stale RX drain completed");
    }

    /* This models the first complete post-ready request. It must be admitted
       normally rather than consumed by the older discard epoch. */
    snapshot = usb_cdc_session_gate_snapshot(&s_gate);
    if (!usb_cdc_session_gate_snapshot_is_current(&s_gate, snapshot)) {
        fail("first post-ready RX batch was not admitted");
    }
    current_generation = usb_cdc_session_gate_generation(&s_gate);
    if (snapshot.generation != current_generation ||
        !usb_cdc_session_gate_command_begin(&s_gate, snapshot.generation)) {
        fail("first post-ready request was silently discarded");
    }
    usb_cdc_session_gate_command_end(&s_gate);
}

int main(void) {
    test_inflight_command_completes_before_reset_returns();
    test_reset_cannot_be_reopened_while_cutoff_is_in_progress();
    test_reset_cancels_an_open_publication_reserved_before_cutoff();
    test_pre_reset_frame_cannot_execute_after_new_session_opens();
    test_acquired_batch_snapshot_is_invalid_after_reset_and_reopen();
    test_callback_close_invalidates_generation_before_reconnect();
    test_lifecycle_open_waits_for_stale_rx_drain_before_new_request();
    puts("production session gate lifecycle overlap: PASS");
    return 0;
}
