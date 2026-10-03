// Task #50 / REV-83-10 executable regression harness for production CDC TX.

#include "usb_cdc_session_gate.h"
#include "usb_cdc_tx.h"

#include <assert.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define TEST_RESPONSE "{\"id\":\"req-1\",\"ok\":true,\"result\":{\"pong\":true}}"
#define TEST_RESPONSE_LEN (sizeof(TEST_RESPONSE) - 1u)
#define FAKE_BUFFER_BYTES 512u

typedef struct {
    pthread_mutex_t lock;
    pthread_cond_t cond;
    bool connected;
    bool block_final_flush;
    bool final_flush_entered;
    bool release_final_flush;
    unsigned session_index;
    uint8_t pending[FAKE_BUFFER_BYTES];
    size_t pending_len;
    uint8_t delivered[2][FAKE_BUFFER_BYTES];
    size_t delivered_len[2];
} fake_transport_t;

typedef struct {
    usb_cdc_session_gate_t *gate;
    fake_transport_t *transport;
    atomic_bool done;
} reset_ctx_t;

typedef struct {
    usb_cdc_session_gate_t *gate;
    const usb_cdc_tx_ops_t *ops;
    uint32_t generation;
    atomic_bool done;
    bool result;
} writer_ctx_t;

static void fake_transport_init(fake_transport_t *transport) {
    memset(transport, 0, sizeof(*transport));
    assert(pthread_mutex_init(&transport->lock, NULL) == 0);
    assert(pthread_cond_init(&transport->cond, NULL) == 0);
    transport->connected = true;
}

static void fake_transport_destroy(fake_transport_t *transport) {
    assert(pthread_cond_destroy(&transport->cond) == 0);
    assert(pthread_mutex_destroy(&transport->lock) == 0);
}

static bool fake_is_connected(void *ctx) {
    fake_transport_t *transport = ctx;
    bool connected;
    assert(pthread_mutex_lock(&transport->lock) == 0);
    connected = transport->connected;
    assert(pthread_mutex_unlock(&transport->lock) == 0);
    return connected;
}

static size_t fake_queue(void *ctx, const uint8_t *data, size_t len) {
    fake_transport_t *transport = ctx;
    size_t room;
    size_t queued;

    assert(pthread_mutex_lock(&transport->lock) == 0);
    if (!transport->connected) {
        assert(pthread_mutex_unlock(&transport->lock) == 0);
        return 0u;
    }
    room = sizeof(transport->pending) - transport->pending_len;
    queued = len < room ? len : room;
    if (queued > 0u) {
        memcpy(transport->pending + transport->pending_len, data, queued);
        transport->pending_len += queued;
    }
    assert(pthread_mutex_unlock(&transport->lock) == 0);
    return queued;
}

static size_t fake_queue_char(void *ctx, uint8_t value) {
    return fake_queue(ctx, &value, 1u);
}

static bool fake_flush(void *ctx, uint32_t timeout_ms) {
    fake_transport_t *transport = ctx;
    bool ok;

    assert(pthread_mutex_lock(&transport->lock) == 0);
    if (transport->block_final_flush && timeout_ms >= 100u) {
        transport->final_flush_entered = true;
        assert(pthread_cond_broadcast(&transport->cond) == 0);
        while (!transport->release_final_flush) {
            assert(pthread_cond_wait(&transport->cond, &transport->lock) == 0);
        }
    }

    ok = transport->connected;
    if (ok && transport->pending_len > 0u) {
        unsigned index = transport->session_index;
        size_t room = sizeof(transport->delivered[index]) -
                      transport->delivered_len[index];
        assert(index < 2u);
        assert(transport->pending_len <= room);
        memcpy(transport->delivered[index] + transport->delivered_len[index],
               transport->pending, transport->pending_len);
        transport->delivered_len[index] += transport->pending_len;
    }
    transport->pending_len = 0u;
    assert(pthread_mutex_unlock(&transport->lock) == 0);
    return ok;
}

static void fake_wait(void *ctx) {
    (void)ctx;
    sched_yield();
}

static void fake_disconnect(fake_transport_t *transport) {
    assert(pthread_mutex_lock(&transport->lock) == 0);
    transport->connected = false;
    // Model the physical USB session boundary: TinyUSB class/FIFO state from
    // the detached bus is not carried into the next USB session.
    transport->pending_len = 0u;
    assert(pthread_mutex_unlock(&transport->lock) == 0);
}

static void fake_reconnect(fake_transport_t *transport) {
    assert(pthread_mutex_lock(&transport->lock) == 0);
    assert(transport->session_index == 0u);
    transport->session_index = 1u;
    transport->connected = true;
    assert(pthread_mutex_unlock(&transport->lock) == 0);
}

static void fake_release_final_flush(fake_transport_t *transport) {
    assert(pthread_mutex_lock(&transport->lock) == 0);
    transport->release_final_flush = true;
    assert(pthread_cond_broadcast(&transport->cond) == 0);
    assert(pthread_mutex_unlock(&transport->lock) == 0);
}

static void fake_wait_for_final_flush(fake_transport_t *transport) {
    assert(pthread_mutex_lock(&transport->lock) == 0);
    while (!transport->final_flush_entered) {
        assert(pthread_cond_wait(&transport->cond, &transport->lock) == 0);
    }
    assert(pthread_mutex_unlock(&transport->lock) == 0);
}

static void gate_wait(void *ctx) {
    (void)ctx;
    sched_yield();
}

static bool tx_session_is_current(void *ctx, uint32_t generation) {
    usb_cdc_session_gate_t *gate = ctx;
    usb_cdc_session_snapshot_t snapshot = {
        .generation = generation,
        .admitted = true,
    };
    return usb_cdc_session_gate_snapshot_is_current(gate, snapshot);
}

static usb_cdc_tx_ops_t make_ops(fake_transport_t *transport,
                                 usb_cdc_session_gate_t *gate) {
    usb_cdc_tx_ops_t ops = {
        .transport_ctx = transport,
        .session_ctx = gate,
        .is_connected = fake_is_connected,
        .session_is_current = tx_session_is_current,
        .queue = fake_queue,
        .queue_char = fake_queue_char,
        .flush = fake_flush,
        .wait = fake_wait,
        .retry_flush_timeout_ms = 20u,
        .final_flush_timeout_ms = 250u,
    };
    return ops;
}

static void open_clean_session(usb_cdc_session_gate_t *gate) {
    uint32_t epoch = usb_cdc_session_gate_rx_discard_epoch(gate);
    usb_cdc_session_gate_mark_rx_drained(gate, epoch);
    assert(usb_cdc_session_gate_open(gate));
}

static void wait_for_generation_change(usb_cdc_session_gate_t *gate,
                                       uint32_t old_generation) {
    unsigned spins = 0u;
    while (usb_cdc_session_gate_generation(gate) == old_generation) {
        assert(spins++ < 1000000u);
        sched_yield();
    }
}

static void *reset_thread(void *arg) {
    reset_ctx_t *ctx = arg;
    fake_disconnect(ctx->transport);
    usb_cdc_session_gate_reset(ctx->gate, gate_wait, NULL);
    atomic_store_explicit(&ctx->done, true, memory_order_release);
    return NULL;
}

static void *writer_thread(void *arg) {
    writer_ctx_t *ctx = arg;
    ctx->result = usb_cdc_tx_write_response(
        ctx->ops, TEST_RESPONSE, TEST_RESPONSE_LEN, ctx->generation);
    usb_cdc_session_gate_command_end(ctx->gate);
    atomic_store_explicit(&ctx->done, true, memory_order_release);
    return NULL;
}

static void test_reset_after_generation_before_first_tx(void) {
    usb_cdc_session_gate_t gate;
    fake_transport_t transport;
    usb_cdc_tx_ops_t ops;
    reset_ctx_t reset_ctx;
    pthread_t reset_tid;
    uint32_t generation;
    bool wrote;

    usb_cdc_session_gate_init(&gate);
    fake_transport_init(&transport);
    open_clean_session(&gate);
    generation = usb_cdc_session_gate_generation(&gate);
    assert(usb_cdc_session_gate_command_begin(&gate, generation));
    ops = make_ops(&transport, &gate);

    reset_ctx.gate = &gate;
    reset_ctx.transport = &transport;
    atomic_init(&reset_ctx.done, false);
    assert(pthread_create(&reset_tid, NULL, reset_thread, &reset_ctx) == 0);
    wait_for_generation_change(&gate, generation);

    // The response already exists, but teardown won before the first TX call.
    wrote = usb_cdc_tx_write_response(&ops, TEST_RESPONSE, TEST_RESPONSE_LEN,
                                      generation);
    assert(!wrote);
    usb_cdc_session_gate_command_end(&gate);
    assert(pthread_join(reset_tid, NULL) == 0);
    assert(atomic_load_explicit(&reset_ctx.done, memory_order_acquire));

    open_clean_session(&gate);
    fake_reconnect(&transport);
    assert(transport.delivered_len[0] == 0u);
    assert(transport.delivered_len[1] == 0u);
    fake_transport_destroy(&transport);
}

static void test_reset_during_final_flush_cannot_leak_into_reopened_session(void) {
    usb_cdc_session_gate_t gate;
    fake_transport_t transport;
    usb_cdc_tx_ops_t ops;
    writer_ctx_t writer_ctx;
    reset_ctx_t reset_ctx;
    pthread_t writer_tid;
    pthread_t reset_tid;
    uint32_t generation;

    usb_cdc_session_gate_init(&gate);
    fake_transport_init(&transport);
    open_clean_session(&gate);
    generation = usb_cdc_session_gate_generation(&gate);
    assert(usb_cdc_session_gate_command_begin(&gate, generation));

    transport.block_final_flush = true;
    ops = make_ops(&transport, &gate);
    writer_ctx.gate = &gate;
    writer_ctx.ops = &ops;
    writer_ctx.generation = generation;
    writer_ctx.result = true;
    atomic_init(&writer_ctx.done, false);
    assert(pthread_create(&writer_tid, NULL, writer_thread, &writer_ctx) == 0);
    fake_wait_for_final_flush(&transport);

    reset_ctx.gate = &gate;
    reset_ctx.transport = &transport;
    atomic_init(&reset_ctx.done, false);
    assert(pthread_create(&reset_tid, NULL, reset_thread, &reset_ctx) == 0);
    wait_for_generation_change(&gate, generation);

    // Teardown must be waiting on the active command/TX region; it may not
    // reopen a later session while this old response flush is still in flight.
    assert(!atomic_load_explicit(&reset_ctx.done, memory_order_acquire));
    fake_release_final_flush(&transport);

    assert(pthread_join(writer_tid, NULL) == 0);
    assert(atomic_load_explicit(&writer_ctx.done, memory_order_acquire));
    assert(!writer_ctx.result);
    assert(pthread_join(reset_tid, NULL) == 0);
    assert(atomic_load_explicit(&reset_ctx.done, memory_order_acquire));

    open_clean_session(&gate);
    fake_reconnect(&transport);
    assert(transport.delivered_len[0] == 0u);
    assert(transport.delivered_len[1] == 0u);
    fake_transport_destroy(&transport);
}

int main(void) {
    test_reset_after_generation_before_first_tx();
    test_reset_during_final_flush_cannot_leak_into_reopened_session();
    puts("production CDC TX session boundary: PASS");
    return 0;
}
