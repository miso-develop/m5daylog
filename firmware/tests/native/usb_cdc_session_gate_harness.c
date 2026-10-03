#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "usb_cdc_session_gate.h"

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

static void test_inflight_command_completes_before_reset_returns(void) {
    pthread_t command;
    pthread_t reset;

    usb_cdc_session_gate_init(&s_gate);
    usb_cdc_session_gate_open(&s_gate);
    s_frame_generation = usb_cdc_session_gate_generation(&s_gate);
    atomic_store(&s_command_entered, false);
    atomic_store(&s_release_command, false);
    atomic_store(&s_pending, false);
    atomic_store(&s_reset_waiting, false);
    atomic_store(&s_reset_done, false);

    if (pthread_create(&command, NULL, command_thread, NULL) != 0) {
        fail("pthread_create command");
    }
    spin_until(&s_command_entered);

    if (pthread_create(&reset, NULL, reset_thread, NULL) != 0) {
        fail("pthread_create reset");
    }
    spin_until(&s_reset_waiting);
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

static void test_pre_reset_frame_cannot_execute_after_new_session_opens(void) {
    uint32_t stale_generation;

    usb_cdc_session_gate_init(&s_gate);
    usb_cdc_session_gate_open(&s_gate);
    stale_generation = usb_cdc_session_gate_generation(&s_gate);

    usb_cdc_session_gate_reset(&s_gate, NULL, NULL);
    atomic_store(&s_pending, false); /* detach flush/restart cutoff */
    usb_cdc_session_gate_open(&s_gate); /* later physical session */

    if (usb_cdc_session_gate_command_begin(&s_gate, stale_generation)) {
        atomic_store(&s_pending, true);
        usb_cdc_session_gate_command_end(&s_gate);
        fail("stale complete request executed after reset/new-session cutoff");
    }
    if (atomic_load(&s_pending)) {
        fail("stale request recreated pending after detach flush");
    }
}

int main(void) {
    test_inflight_command_completes_before_reset_returns();
    test_pre_reset_frame_cannot_execute_after_new_session_opens();
    puts("production session gate overlap: PASS");
    return 0;
}
