// REV-83-14: real production response writer -> final-IN wait ->
// post-command release-coordinator gate, with scripted TinyUSB observations.
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "usb_cdc_tx.h"

#define CHECK(x) do { if (!(x)) { \
    fprintf(stderr, "CHECK failed line %d: %s\n", __LINE__, #x); exit(2); \
} } while (0)

typedef enum {
    OLD_CALLBACK_ONLY,
    SPLIT_FIRST_ONLY,
    FINAL_IN_BUSY,
    FINAL_AFTER_TIMEOUT,
    NO_CALLBACK,
    SESSION_INVALIDATED,
    INVALID_DURING_GRACE,
    SUCCESS
} case_t;

typedef struct {
    case_t which;
    uint32_t ticks;
    uint32_t generation;
    unsigned before;
    unsigned completion_calls;
    unsigned sample_calls;
    unsigned authorization_calls;
    bool connected;
    bool did_flush;
} fixture_t;

static bool valid(void *opaque) {
    fixture_t *f = opaque;
    if ((f->which == SESSION_INVALIDATED && f->ticks >= 2u) ||
        (f->which == INVALID_DURING_GRACE && f->ticks >= 6u)) {
        return false;
    }
    return f->connected;
}

static bool generation_current(void *opaque, uint32_t generation) {
    fixture_t *f = opaque;
    return generation == f->generation && valid(opaque);
}

static uint32_t now_ticks(void *opaque) {
    return ((fixture_t *)opaque)->ticks;
}

static void delay_ticks(void *opaque, uint32_t ticks) {
    fixture_t *f = opaque;
    f->ticks += ticks;
}

static bool sample_in(void *opaque, unsigned *after,
                      bool *fifo_drained, bool *endpoint_busy) {
    fixture_t *f = opaque;
    f->sample_calls++;
    *after = f->before;
    *fifo_drained = false;
    *endpoint_busy = true;
    switch (f->which) {
    case OLD_CALLBACK_ONLY:
        // A previous response's callback arrives while release is busy.
        if (f->ticks >= 1u) *after += 1u;
        *fifo_drained = true;
        break;
    case SPLIT_FIRST_ONLY:
        // First release chunk completed; later bytes remain in FIFO.
        if (f->ticks >= 1u) *after += 1u;
        *endpoint_busy = false;
        break;
    case FINAL_IN_BUSY:
        if (f->ticks >= 1u) *after += 2u;
        *fifo_drained = true;
        break;
    case FINAL_AFTER_TIMEOUT:
        if (f->ticks >= 15u) {
            *after += 1u;
            *fifo_drained = true;
            *endpoint_busy = false;
        }
        break;
    case NO_CALLBACK:
        *fifo_drained = true;
        *endpoint_busy = false;
        break;
    case SESSION_INVALIDATED:
        if (f->ticks >= 4u) {
            *after += 1u;
            *fifo_drained = true;
            *endpoint_busy = false;
        }
        break;
    case INVALID_DURING_GRACE:
    case SUCCESS:
        // First chunk completed early, then *final* completed at tick 4.
        if (f->ticks >= 1u) *after += 1u;
        if (f->ticks >= 4u) {
            *after += 1u;
            *fifo_drained = true;
            *endpoint_busy = false;
        }
        break;
    }
    return true;
}

static bool await_final(void *opaque, uint32_t timeout_ms) {
    fixture_t *f = opaque;
    usb_cdc_tx_final_wait_ops_t ops = {
        .ctx = f,
        .session_current = valid,
        .sample = sample_in,
        .now_ticks = now_ticks,
        .delay_ticks = delay_ticks,
        .callbacks_before = f->before,
        .timeout_ticks = timeout_ms,
        .host_grace_ticks = 3u,
    };
    CHECK(f->did_flush);
    CHECK(timeout_ms == 10u);
    return usb_cdc_tx_wait_final_in(&ops);
}

static size_t queue_bytes(void *opaque, const uint8_t *data, size_t len) {
    (void)opaque;
    CHECK(data != NULL);
    return len;
}

static size_t queue_char(void *opaque, uint8_t ch) {
    (void)opaque;
    CHECK(ch == (uint8_t)'\n');
    return 1u;
}

static bool flush(void *opaque, uint32_t timeout_ms) {
    fixture_t *f = opaque;
    CHECK(timeout_ms == 250u);
    f->did_flush = true;
    return true;
}

static bool authorized(const char *attempt, void *opaque) {
    fixture_t *f = opaque;
    CHECK(strcmp(attempt, "release-83") == 0);
    f->authorization_calls++;
    return true;
}

static void run(case_t which, bool expected) {
    fixture_t fixture = {
        .which = which,
        .ticks = 0u,
        .generation = 9u,
        .before = 20u,
        .connected = true,
    };
    usb_cdc_tx_ops_t transport = {
        .transport_ctx = &fixture,
        .session_ctx = &fixture,
        .is_connected = valid,
        .session_is_current = generation_current,
        .queue = queue_bytes,
        .queue_char = queue_char,
        .flush = flush,
        .await_endpoint = await_final,
        .endpoint_timeout_ms = 10u,
        .final_flush_timeout_ms = 250u,
    };
    // Mirrors the production worker: write/await, end command barrier,
    // then call production post-command release coordinator authorization.
    bool completed = usb_cdc_tx_write_response(
        &transport, "{\"id\":\"r\",\"ok\":true}", 20u, 9u);
    CHECK(completed == expected);
    CHECK(fixture.did_flush);
    CHECK(fixture.sample_calls > 0u || which == SESSION_INVALIDATED);
    bool notified = usb_cdc_tx_complete_accepted_release(
        true, completed, "release-83", authorized, &fixture);
    CHECK(notified == expected);
    CHECK(fixture.authorization_calls == (expected ? 1u : 0u));
    // Neither a missing release acceptance nor failed response completion
    // may cause a coordinator handoff, even if a callback exists.
    CHECK(!usb_cdc_tx_complete_accepted_release(
        false, true, "release-83", authorized, &fixture));
    CHECK(fixture.authorization_calls == (expected ? 1u : 0u));
    // A failed attempt remains within the timeout, not an infinite poll.
    CHECK(fixture.ticks <= 13u);
}

int main(void) {
    run(OLD_CALLBACK_ONLY, false);
    run(SPLIT_FIRST_ONLY, false);
    run(FINAL_IN_BUSY, false);
    run(FINAL_AFTER_TIMEOUT, false);
    run(NO_CALLBACK, false);
    run(SESSION_INVALIDATED, false);
    run(INVALID_DURING_GRACE, false);
    run(SUCCESS, true);
    puts("production final-IN wait and release authorization: PASS");
    return 0;
}
