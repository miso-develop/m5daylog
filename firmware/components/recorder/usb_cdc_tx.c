// Task #50: portable session-fenced CDC response writer.

#include "usb_cdc_tx.h"

#include <stddef.h>

static bool tx_is_current(const usb_cdc_tx_ops_t *ops,
                          uint32_t response_generation) {
    return ops->is_connected(ops->transport_ctx) &&
           ops->session_is_current(ops->session_ctx, response_generation);
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

    // A successful final flush is the transport response-completion boundary.
    // The host may close its CDC handle immediately after receiving the
    // response, dropping DTR/RTS before this task executes again. That
    // non-authoritative line-state change must not retract an already delivered
    // RELEASE_STORAGE response. Session identity remains authoritative, though:
    // if the originating generation was invalidated while the flush ran, do not
    // let a stale response authorize teardown.
    return ops->session_is_current(ops->session_ctx, response_generation);
}
