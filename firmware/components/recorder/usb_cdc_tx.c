// Task #50: portable session-fenced CDC response writer.

#include "usb_cdc_tx.h"

#include <stddef.h>

static bool tx_is_current(const usb_cdc_tx_ops_t *ops,
                          uint32_t response_generation) {
    return ops->is_connected(ops->transport_ctx) &&
           ops->session_is_current(ops->session_ctx, response_generation);
}

bool usb_cdc_tx_final_in_complete(unsigned callbacks_before,
                                  unsigned callbacks_after,
                                  bool fifo_drained,
                                  bool endpoint_busy) {
    // Completion of a *previous* packet does not establish that a later,
    // multi-packet response has reached the host. Both class FIFO and physical
    // endpoint must have drained, and a TX callback must have occurred since
    // this response began queueing. Generation validity is checked by caller.
    return callbacks_after != callbacks_before &&
           fifo_drained && !endpoint_busy;
}

static bool tx_final_in_sample_ready(
    const usb_cdc_tx_final_wait_ops_t *ops) {
    unsigned after = 0u;
    bool drained = false;
    bool busy = true;
    return ops->sample(ops->ctx, &after, &drained, &busy) &&
           usb_cdc_tx_final_in_complete(
               ops->callbacks_before, after, drained, busy);
}

bool usb_cdc_tx_wait_final_in(const usb_cdc_tx_final_wait_ops_t *ops) {
    uint32_t start;
    if (ops == NULL || ops->session_current == NULL ||
        ops->sample == NULL || ops->now_ticks == NULL ||
        ops->delay_ticks == NULL || ops->timeout_ticks == 0u) {
        return false;
    }
    start = ops->now_ticks(ops->ctx);
    do {
        if (!ops->session_current(ops->ctx)) {
            return false;
        }
        if (tx_final_in_sample_ready(ops)) {
            // CDC callback may run before TinyUSB queues the next segment.
            ops->delay_ticks(ops->ctx, 1u);
            if (!ops->session_current(ops->ctx)) {
                return false;
            }
            if (!tx_final_in_sample_ready(ops)) {
                continue;
            }
            // A grace period helps the PC read its response; this is not a
            // host-application acknowledgement and does not replace one.
            ops->delay_ticks(ops->ctx, ops->host_grace_ticks);
            return ops->session_current(ops->ctx) &&
                   tx_final_in_sample_ready(ops);
        }
        ops->delay_ticks(ops->ctx, 1u);
    } while ((uint32_t)(ops->now_ticks(ops->ctx) - start) <
             ops->timeout_ticks);
    return false;
}

bool usb_cdc_tx_complete_accepted_release(
    bool release_accepted, bool response_complete,
    const char *attempt_id, usb_cdc_tx_release_complete_fn_t callback,
    void *ctx) {
    if (!release_accepted || !response_complete ||
        attempt_id == NULL || callback == NULL) {
        return false;
    }
    return callback(attempt_id, ctx);
}

bool usb_cdc_tx_write_response(const usb_cdc_tx_ops_t *ops,
                               const char *response, size_t len,
                               uint32_t response_generation) {
    size_t sent = 0u;

    if (ops == NULL || (response == NULL && len != 0u) ||
        ops->is_connected == NULL || ops->session_is_current == NULL ||
        ops->queue == NULL || ops->queue_char == NULL || ops->flush == NULL) {
        return false;
    }

    while (sent < len) {
        size_t queued;

        if (!tx_is_current(ops, response_generation)) {
            return false;
        }
        queued = ops->queue(ops->transport_ctx,
                            (const uint8_t *)response + sent, len - sent);
        if (queued > len - sent) {
            return false;
        }
        if (queued == 0u) {
            if (!tx_is_current(ops, response_generation)) {
                return false;
            }
            if (!ops->flush(ops->transport_ctx,
                            ops->retry_flush_timeout_ms) &&
                ops->wait != NULL) {
                ops->wait(ops->transport_ctx);
            }
            continue;
        }
        sent += queued;
    }

    for (;;) {
        if (!tx_is_current(ops, response_generation)) {
            return false;
        }
        if (ops->queue_char(ops->transport_ctx, (uint8_t)'\n') != 0u) {
            break;
        }
        (void)ops->flush(ops->transport_ctx, ops->retry_flush_timeout_ms);
        if (ops->wait != NULL) {
            ops->wait(ops->transport_ctx);
        }
    }

    if (!tx_is_current(ops, response_generation)) {
        return false;
    }
    if (!ops->flush(ops->transport_ctx, ops->final_flush_timeout_ms)) {
        return false;
    }
    // A successful FIFO flush can still leave the final USB IN transaction
    // outstanding. RELEASE_STORAGE supplies a transfer-completion barrier:
    // fail closed, with no teardown request, if the endpoint does not finish.
    if (ops->await_endpoint != NULL &&
        !ops->await_endpoint(ops->transport_ctx, ops->endpoint_timeout_ms)) {
        return false;
    }

    // A successful final flush, plus endpoint completion when required, is
    // the Device transport boundary; neither proves PC application receipt.
    // The host may close its CDC handle immediately after receiving the
    // response, dropping DTR/RTS before this task executes again. That
    // non-authoritative line-state change must not retract an already delivered
    // RELEASE_STORAGE response. Session identity remains authoritative, though:
    // if the originating generation was invalidated while the flush ran, do not
    // let a stale response authorize teardown.
    return ops->session_is_current(ops->session_ctx, response_generation);
}
