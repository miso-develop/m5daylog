#pragma once

// Task #50 portable CDC response writer.
//
// The recorder/session lifecycle owns admission and generation identity. This
// module owns only one response TX attempt and revalidates that identity at
// each transport boundary. Platform code supplies TinyUSB/RTOS operations;
// native tests supply deterministic transport operations for reset races.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef bool (*usb_cdc_tx_is_connected_fn_t)(void *ctx);
typedef bool (*usb_cdc_tx_session_is_current_fn_t)(void *ctx,
                                                    uint32_t generation);
typedef size_t (*usb_cdc_tx_queue_fn_t)(void *ctx, const uint8_t *data,
                                        size_t len);
typedef size_t (*usb_cdc_tx_queue_char_fn_t)(void *ctx, uint8_t value);
typedef bool (*usb_cdc_tx_flush_fn_t)(void *ctx, uint32_t timeout_ms);
typedef void (*usb_cdc_tx_wait_fn_t)(void *ctx);
// Called after the final FIFO flush, before a release can authorize teardown.
// Platform code must prove the last USB IN transfer completed, not merely
// that TinyUSB moved bytes from its software FIFO into an endpoint buffer.
typedef bool (*usb_cdc_tx_await_endpoint_fn_t)(void *ctx,
                                               uint32_t timeout_ms);

typedef struct {
    void *transport_ctx;
    void *session_ctx;
    usb_cdc_tx_is_connected_fn_t is_connected;
    usb_cdc_tx_session_is_current_fn_t session_is_current;
    usb_cdc_tx_queue_fn_t queue;
    usb_cdc_tx_queue_char_fn_t queue_char;
    usb_cdc_tx_flush_fn_t flush;
    usb_cdc_tx_wait_fn_t wait;
    usb_cdc_tx_await_endpoint_fn_t await_endpoint;
    uint32_t endpoint_timeout_ms;
    uint32_t retry_flush_timeout_ms;
    uint32_t final_flush_timeout_ms;
} usb_cdc_tx_ops_t;

// Proof for the final response USB-IN transaction, not merely any earlier
// completion callback. Call only after the final software FIFO flush completed.
// Caller must also fence the originating CDC generation while evaluating it.
bool usb_cdc_tx_final_in_complete(unsigned callbacks_before,
                                  unsigned callbacks_after,
                                  bool fifo_drained,
                                  bool endpoint_busy);

// The production release completion wait. The platform supplies its real
// TinyUSB CDC FIFO / bulk-IN endpoint observations and scheduler clock; native
// tests exercise the exact same wait and timeout logic with deterministic
// observations. All callback samples are relative to the pre-queue baseline.
typedef struct {
    void *ctx;
    bool (*session_current)(void *ctx);
    bool (*sample)(void *ctx, unsigned *callbacks_after,
                   bool *fifo_drained, bool *endpoint_busy);
    uint32_t (*now_ticks)(void *ctx);
    void (*delay_ticks)(void *ctx, uint32_t ticks);
    unsigned callbacks_before;
    uint32_t timeout_ticks;
    uint32_t host_grace_ticks;
} usb_cdc_tx_final_wait_ops_t;

bool usb_cdc_tx_wait_final_in(const usb_cdc_tx_final_wait_ops_t *ops);

// This is the production post-command gate for release coordinator dispatch.
// It must be called only after the originating command's session barrier ends.
// A response that failed the final-IN wait cannot authorize a teardown.
typedef bool (*usb_cdc_tx_release_complete_fn_t)(const char *attempt_id,
                                                 void *ctx);
bool usb_cdc_tx_complete_accepted_release(
    bool release_accepted, bool response_complete,
    const char *attempt_id, usb_cdc_tx_release_complete_fn_t callback,
    void *ctx);

// Queue response bytes + LF and flush them while the originating session is
// still current. Returns false as soon as disconnect/generation invalidation or
// a final flush failure makes completion impossible. The caller must keep this
// call inside the same command/session execution barrier as request handling.
bool usb_cdc_tx_write_response(const usb_cdc_tx_ops_t *ops,
                               const char *response, size_t len,
                               uint32_t response_generation);

#ifdef __cplusplus
}
#endif
