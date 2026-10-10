"""D-031 / Task #87 production-C RELEASE_STORAGE regressions."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
INCLUDE = COMP / "include"
CORE = COMP / "usb_cdc_protocol_core.c"
TRANSPORT = COMP / "usb_cdc_protocol.c"
TX = COMP / "usb_cdc_tx.c"
OWNERSHIP = COMP / "usb_msc_ownership.c"
CJSON_STUB = REPO / "firmware/tests/native/cjson_stub.c"
STUB_INCLUDE = REPO / "firmware/tests/native/include"
HARNESS = REPO / "firmware/tests/native/release_storage_protocol_harness.c"
GATE = COMP / "usb_cdc_session_gate.c"
GATE_HARNESS = REPO / "firmware/tests/native/usb_cdc_session_gate_harness.c"


def test_release_storage_vectors_execute_production_dispatcher(tmp_path: Path) -> None:
    executable = tmp_path / "release_storage_protocol"
    result = subprocess.run(
        [
            "cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L",
            "-Wall", "-Wextra", "-Werror", "-include", "stdio.h",
            "-I", str(STUB_INCLUDE), "-I", str(INCLUDE),
            str(CORE), str(CJSON_STUB), str(HARNESS),
            "-o", str(executable),
        ],
        cwd=REPO, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    run = subprocess.run(
        [str(executable)], cwd=REPO, text=True, capture_output=True, check=False
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "release storage production protocol: PASS" in run.stdout


def test_canonical_session_gate_executes_teardown_race_harness(tmp_path: Path) -> None:
    executable = tmp_path / "usb_cdc_session_gate"
    result = subprocess.run(
        [
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
            "-I", str(INCLUDE), str(GATE), str(GATE_HARNESS),
            "-o", str(executable),
        ],
        cwd=REPO, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    run = subprocess.run(
        [str(executable)], cwd=REPO, text=True, capture_output=True, check=False
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "production session gate lifecycle overlap: PASS" in run.stdout


def test_tx_final_flush_is_release_response_completion_boundary(tmp_path: Path) -> None:
    """A host close after successful flush must not retract accepted release."""

    harness = tmp_path / "usb_cdc_tx_flush_boundary.c"
    harness.write_text(
        r"""
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "usb_cdc_tx.h"

#define CHECK(expr) do { \
    if (!(expr)) { \
        fprintf(stderr, "CHECK failed %s:%d: %s\n", __FILE__, __LINE__, #expr); \
        exit(2); \
    } \
} while (0)

typedef struct {
    bool connected;
    bool current;
    bool close_during_final_flush;
    bool invalidate_session_during_final_flush;
    bool close_before_final_flush;
    bool final_flush_result;
    unsigned flush_calls;
    uint32_t expected_generation;
    char output[128];
    size_t output_len;
} tx_ctx_t;

static bool is_connected(void *opaque) {
    return ((tx_ctx_t *)opaque)->connected;
}

static bool session_is_current(void *opaque, uint32_t generation) {
    tx_ctx_t *ctx = opaque;
    return ctx->current && generation == ctx->expected_generation;
}

static size_t queue_bytes(void *opaque, const uint8_t *data, size_t len) {
    tx_ctx_t *ctx = opaque;
    CHECK(ctx->output_len + len < sizeof(ctx->output));
    memcpy(ctx->output + ctx->output_len, data, len);
    ctx->output_len += len;
    ctx->output[ctx->output_len] = '\0';
    return len;
}

static size_t queue_char(void *opaque, uint8_t value) {
    tx_ctx_t *ctx = opaque;
    CHECK(ctx->output_len + 1u < sizeof(ctx->output));
    ctx->output[ctx->output_len++] = (char)value;
    ctx->output[ctx->output_len] = '\0';
    if (ctx->close_before_final_flush) {
        ctx->connected = false;
    }
    return 1u;
}

static bool flush_bytes(void *opaque, uint32_t timeout_ms) {
    tx_ctx_t *ctx = opaque;
    ctx->flush_calls++;
    if (timeout_ms == 250u && ctx->close_during_final_flush) {
        /*
         * Model the Windows race observed by the physical gate: the host has
         * consumed the response and closes its CDC handle as the successful
         * final flush completes, dropping DTR/RTS immediately afterward.
         */
        ctx->connected = false;
    }
    if (timeout_ms == 250u && ctx->invalidate_session_during_final_flush) {
        ctx->current = false;
    }
    return timeout_ms == 250u ? ctx->final_flush_result : true;
}

static void wait_once(void *opaque) {
    (void)opaque;
}

static usb_cdc_tx_ops_t make_ops(tx_ctx_t *ctx) {
    usb_cdc_tx_ops_t ops = {
        .transport_ctx = ctx,
        .session_ctx = ctx,
        .is_connected = is_connected,
        .session_is_current = session_is_current,
        .queue = queue_bytes,
        .queue_char = queue_char,
        .flush = flush_bytes,
        .wait = wait_once,
        .retry_flush_timeout_ms = 20u,
        .final_flush_timeout_ms = 250u,
    };
    return ops;
}

int main(void) {
    const char *response = "{\"accepted\":true}";
    const uint32_t generation = 7u;
    tx_ctx_t after_flush_close = {
        .connected = true,
        .current = true,
        .close_during_final_flush = true,
        .final_flush_result = true,
        .expected_generation = generation,
    };
    usb_cdc_tx_ops_t ops = make_ops(&after_flush_close);

    CHECK(usb_cdc_tx_write_response(
        &ops, response, strlen(response), generation));
    CHECK(!after_flush_close.connected);
    CHECK(after_flush_close.flush_calls == 1u);
    CHECK(strcmp(after_flush_close.output, "{\"accepted\":true}\n") == 0);

    /*
     * Session identity is still authoritative after a successful flush. A
     * generation reset racing the flush must prevent stale-session teardown.
     */
    tx_ctx_t session_invalidated = {
        .connected = true,
        .current = true,
        .invalidate_session_during_final_flush = true,
        .final_flush_result = true,
        .expected_generation = generation,
    };
    ops = make_ops(&session_invalidated);
    CHECK(!usb_cdc_tx_write_response(
        &ops, response, strlen(response), generation));
    CHECK(session_invalidated.flush_calls == 1u);
    CHECK(!session_invalidated.current);

    /*
     * Disconnect before the final flush remains fail-closed: no transport
     * completion means the caller must not signal release teardown.
     */
    tx_ctx_t before_flush_close = {
        .connected = true,
        .current = true,
        .close_before_final_flush = true,
        .final_flush_result = true,
        .expected_generation = generation,
    };
    ops = make_ops(&before_flush_close);
    CHECK(!usb_cdc_tx_write_response(
        &ops, response, strlen(response), generation));
    CHECK(before_flush_close.flush_calls == 0u);

    /* A final flush failure itself also remains non-completion. */
    tx_ctx_t flush_failure = {
        .connected = true,
        .current = true,
        .final_flush_result = false,
        .expected_generation = generation,
    };
    ops = make_ops(&flush_failure);
    CHECK(!usb_cdc_tx_write_response(
        &ops, response, strlen(response), generation));
    CHECK(flush_failure.flush_calls == 1u);

    puts("production CDC final-flush boundary: PASS");
    return 0;
}
""",
        encoding="utf-8",
    )

    executable = tmp_path / "usb_cdc_tx_flush_boundary"
    result = subprocess.run(
        [
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-I", str(INCLUDE), str(TX), str(harness),
            "-o", str(executable),
        ],
        cwd=REPO, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    run = subprocess.run(
        [str(executable)], cwd=REPO, text=True, capture_output=True, check=False
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "production CDC final-flush boundary: PASS" in run.stdout


def test_transport_orders_gate_response_then_teardown_signal() -> None:
    src = TRANSPORT.read_text(encoding="utf-8")
    worker_at = src.index("static void usb_cdc_worker_task")
    reset_at = src.index("void usb_cdc_protocol_reset_session", worker_at)
    worker = src[worker_at:reset_at]

    process_at = worker.index("usb_cdc_protocol_process_line")
    tx_at = worker.index("cdc_write_response", process_at)
    command_end_at = worker.index("usb_cdc_session_gate_command_end", tx_at)
    response_complete_at = worker.index(
        "s_config.release_response_complete", command_end_at
    )
    assert process_at < tx_at < command_end_at < response_complete_at
    assert "cdc_lifecycle_admission_open" in worker
    assert "usb_cdc_tx_complete_accepted_release(" in worker
    assert "effect.release_accepted, response_complete" in worker

    line_at = src.index("static void usb_cdc_line_state_callback")
    wait_at = src.index("static void cdc_session_gate_wait", line_at)
    assert "s_config.release_accept" not in src[line_at:wait_at]


def test_release_acceptance_is_shared_atomic_msc_gate() -> None:
    src = OWNERSHIP.read_text(encoding="utf-8")
    accept_at = src.index("usb_msc_ownership_accept_release_storage")
    response_at = src.index(
        "usb_msc_ownership_release_response_complete", accept_at
    )
    accept = src[accept_at:response_at]
    assert "atomic_compare_exchange_strong_explicit" in accept
    assert "&s_release_pending" in accept

    read_at = src.index("int32_t __wrap_tud_msc_read10_cb")
    write_at = src.index("int32_t __wrap_tud_msc_write10_cb", read_at)
    read_wrapper = src[read_at:write_at]
    assert "s_release_pending" in read_wrapper
    assert "__real_tud_msc_read10_cb" in read_wrapper

    quiesce_at = src.index("usb_msc_ownership_complete_release_quiesce")
    assert "s_release_waiting_response" in src[quiesce_at:]


def test_physical_detach_during_tx_cannot_leak_response_after_reconnect(
    tmp_path: Path,
) -> None:
    """REV-83-11: old-session TX is fenced from a later physical session."""

    harness = tmp_path / "usb_cdc_tx_physical_reconnect.c"
    harness.write_text(
        r"""
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "usb_cdc_session_gate.h"
#include "usb_cdc_tx.h"

#define CHECK(expr) do { \
    if (!(expr)) { \
        fprintf(stderr, "CHECK failed %s:%d: %s\n", __FILE__, __LINE__, #expr); \
        exit(2); \
    } \
} while (0)

typedef struct {
    usb_cdc_session_gate_t *gate;
    bool connected;
    bool reconnected;
    bool detach_on_first_queue;
    unsigned queue_calls;
    char old_session_bytes[128];
    size_t old_session_len;
    char new_session_bytes[128];
    size_t new_session_len;
} tx_ctx_t;

static bool is_connected(void *opaque) {
    return ((tx_ctx_t *)opaque)->connected;
}

static bool session_is_current(void *opaque, uint32_t generation) {
    tx_ctx_t *ctx = opaque;
    usb_cdc_session_snapshot_t snapshot = {
        .generation = generation,
        .admitted = true,
    };
    return usb_cdc_session_gate_snapshot_is_current(ctx->gate, snapshot);
}

static void append_bytes(tx_ctx_t *ctx, const uint8_t *data, size_t len) {
    char *dst = ctx->reconnected ? ctx->new_session_bytes
                                 : ctx->old_session_bytes;
    size_t *used = ctx->reconnected ? &ctx->new_session_len
                                    : &ctx->old_session_len;
    CHECK(*used + len < 128u);
    memcpy(dst + *used, data, len);
    *used += len;
    dst[*used] = '\0';
}

static size_t queue_bytes(void *opaque, const uint8_t *data, size_t len) {
    tx_ctx_t *ctx = opaque;
    size_t queued = len;

    ctx->queue_calls++;
    if (ctx->detach_on_first_queue && ctx->queue_calls == 1u) {
        queued = len > 4u ? 4u : len;
        append_bytes(ctx, data, queued);

        /* Production DETACHED callback behavior: transport visibility drops
           and the application generation is invalidated without waiting. */
        ctx->connected = false;
        usb_cdc_session_gate_close(ctx->gate);
        return queued;
    }

    append_bytes(ctx, data, queued);
    return queued;
}

static size_t queue_char(void *opaque, uint8_t value) {
    tx_ctx_t *ctx = opaque;
    append_bytes(ctx, &value, 1u);
    return 1u;
}

static bool flush_bytes(void *opaque, uint32_t timeout_ms) {
    (void)opaque;
    (void)timeout_ms;
    return true;
}

static void wait_once(void *opaque) {
    (void)opaque;
}

static usb_cdc_tx_ops_t make_ops(tx_ctx_t *ctx) {
    usb_cdc_tx_ops_t ops = {
        .transport_ctx = ctx,
        .session_ctx = ctx,
        .is_connected = is_connected,
        .session_is_current = session_is_current,
        .queue = queue_bytes,
        .queue_char = queue_char,
        .flush = flush_bytes,
        .wait = wait_once,
        .retry_flush_timeout_ms = 20u,
        .final_flush_timeout_ms = 250u,
    };
    return ops;
}

int main(void) {
    usb_cdc_session_gate_t gate;
    tx_ctx_t ctx = {0};
    usb_cdc_tx_ops_t ops;
    uint32_t old_generation;
    uint32_t new_generation;
    uint32_t discard_epoch;
    const char *old_response = "{\"id\":\"old\",\"ok\":true}";
    const char *new_response = "{\"id\":\"new\",\"ok\":true}";

    usb_cdc_session_gate_init(&gate);
    CHECK(usb_cdc_session_gate_open(&gate));
    old_generation = usb_cdc_session_gate_generation(&gate);
    CHECK(usb_cdc_session_gate_command_begin(&gate, old_generation));

    ctx.gate = &gate;
    ctx.connected = true;
    ctx.detach_on_first_queue = true;
    ops = make_ops(&ctx);

    CHECK(!usb_cdc_tx_write_response(
        &ops, old_response, strlen(old_response), old_generation));
    CHECK(ctx.old_session_len > 0u);
    CHECK(ctx.old_session_len < strlen(old_response));
    CHECK(ctx.new_session_len == 0u);

    usb_cdc_session_gate_command_end(&gate);

    /* Recorder coordinator handles HOST_REATTACHED outside TinyUSB callback:
       blocking reset/drain first, then explicit fresh-session open. */
    usb_cdc_session_gate_reset(&gate, NULL, NULL);
    discard_epoch = usb_cdc_session_gate_rx_discard_epoch(&gate);
    usb_cdc_session_gate_mark_rx_drained(&gate, discard_epoch);
    CHECK(usb_cdc_session_gate_open(&gate));
    new_generation = usb_cdc_session_gate_generation(&gate);
    CHECK(new_generation != old_generation);

    ctx.connected = true;
    ctx.reconnected = true;
    ctx.detach_on_first_queue = false;
    ctx.queue_calls = 0u;

    /* Even if old TX code were retried after reconnect, its old generation
       cannot enqueue one byte into the new physical session. */
    CHECK(!usb_cdc_tx_write_response(
        &ops, old_response, strlen(old_response), old_generation));
    CHECK(ctx.new_session_len == 0u);

    CHECK(usb_cdc_session_gate_command_begin(&gate, new_generation));
    CHECK(usb_cdc_tx_write_response(
        &ops, new_response, strlen(new_response), new_generation));
    usb_cdc_session_gate_command_end(&gate);
    CHECK(strcmp(ctx.new_session_bytes, "{\"id\":\"new\",\"ok\":true}\n") == 0);

    puts("physical reconnect TX generation fence: PASS");
    return 0;
}
""",
        encoding="utf-8",
    )

    executable = tmp_path / "usb_cdc_tx_physical_reconnect"
    result = subprocess.run(
        [
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-I", str(INCLUDE),
            str(TX), str(GATE), str(harness),
            "-o", str(executable),
        ],
        cwd=REPO, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    run = subprocess.run(
        [str(executable)], cwd=REPO, text=True, capture_output=True, check=False
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "physical reconnect TX generation fence: PASS" in run.stdout


def test_release_requires_final_usb_in_completion(tmp_path: Path) -> None:
    """FIFO-flush success is not the endpoint-completion gate for release."""

    harness = tmp_path / "cdc_endpoint_completion.c"
    harness.write_text(
        r"""
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "usb_cdc_tx.h"

#define CHECK(condition) do { if (!(condition)) { \
    fprintf(stderr, "CHECK failed %s:%d: %s\n", __FILE__, __LINE__, #condition); \
    exit(2); \
} } while (0)

typedef struct {
    bool connected;
    bool current;
    bool fifo_flush_success;
    bool endpoint_completed;
    bool drop_dtr_at_flush;
    int endpoint_wait_calls;
    int flush_calls;
    char output[256];
    size_t used;
} tx_ctx_t;

static bool connected(void *p) { return ((tx_ctx_t *)p)->connected; }
static bool current(void *p, uint32_t gen) {
    return gen == 17u && ((tx_ctx_t *)p)->current;
}
static size_t queue(void *p, const uint8_t *data, size_t n) {
    tx_ctx_t *c = p;
    CHECK(c->used + n < sizeof(c->output));
    memcpy(c->output + c->used, data, n);
    c->used += n;
    return n;
}
static size_t queue_char(void *p, uint8_t ch) {
    return queue(p, &ch, 1u);
}
static bool flush(void *p, uint32_t timeout_ms) {
    tx_ctx_t *c = p;
    CHECK(timeout_ms == 250u);
    c->flush_calls++;
    if (c->drop_dtr_at_flush) c->connected = false;
    return c->fifo_flush_success;
}
static bool endpoint(void *p, uint32_t timeout_ms) {
    tx_ctx_t *c = p;
    CHECK(timeout_ms == 500u);
    c->endpoint_wait_calls++;
    return c->endpoint_completed;
}
static usb_cdc_tx_ops_t ops(tx_ctx_t *c) {
    usb_cdc_tx_ops_t o = {
        .transport_ctx = c,
        .session_ctx = c,
        .is_connected = connected,
        .session_is_current = current,
        .queue = queue,
        .queue_char = queue_char,
        .flush = flush,
        .await_endpoint = endpoint,
        .endpoint_timeout_ms = 500u,
        .final_flush_timeout_ms = 250u,
    };
    return o;
}
static bool write_response(tx_ctx_t *c) {
    usb_cdc_tx_ops_t o = ops(c);
    return usb_cdc_tx_write_response(&o,
        "{\"id\":\"r\",\"ok\":true}",
        strlen("{\"id\":\"r\",\"ok\":true}"), 17u);
}
int main(void) {
    tx_ctx_t in_flight = {
        .connected=true, .current=true, .fifo_flush_success=true
    };
    CHECK(!write_response(&in_flight));
    CHECK(in_flight.flush_calls == 1);
    CHECK(in_flight.endpoint_wait_calls == 1);
    CHECK(in_flight.output[in_flight.used - 1] == '\n');

    tx_ctx_t complete = {
        .connected=true, .current=true,
        .fifo_flush_success=true, .endpoint_completed=true
    };
    CHECK(write_response(&complete));
    CHECK(complete.endpoint_wait_calls == 1);

    tx_ctx_t invalidated = {
        .connected=true, .current=false,
        .fifo_flush_success=true, .endpoint_completed=true
    };
    CHECK(!write_response(&invalidated));
    CHECK(invalidated.endpoint_wait_calls == 0);

    tx_ctx_t fifo_failed = {
        .connected=true, .current=true,
        .fifo_flush_success=false, .endpoint_completed=true
    };
    CHECK(!write_response(&fifo_failed));
    CHECK(fifo_failed.endpoint_wait_calls == 0);

    tx_ctx_t host_closed_after_flush = {
        .connected=true, .current=true,
        .fifo_flush_success=true, .endpoint_completed=true,
        .drop_dtr_at_flush=true,
    };
    CHECK(write_response(&host_closed_after_flush));
    CHECK(!host_closed_after_flush.connected);
    puts("release endpoint completion gating: PASS");
    return 0;
}
""",
        encoding="utf-8",
    )
    exe = tmp_path / "cdc_endpoint_completion"
    build = subprocess.run(
        ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-I", str(INCLUDE), str(TX), str(harness), "-o", str(exe)],
        cwd=REPO, text=True, capture_output=True, check=False,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(exe)], cwd=REPO, text=True, capture_output=True, check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "release endpoint completion gating: PASS" in run.stdout


def test_release_endpoint_completion_production_wiring() -> None:
    """Only accepted release waits for CDC IN completion before teardown."""
    src = TRANSPORT.read_text(encoding="utf-8")
    assert "void tud_cdc_tx_complete_cb(uint8_t itf)" in src
    assert "s_cdc_tx_completed" in src
    assert ".await_endpoint = release_accepted" in src
    assert "cdc_release_await_endpoint" in src
    assert "effect.release_accepted" in src
    tx = TX.read_text(encoding="utf-8")
    assert "ops->await_endpoint(ops->transport_ctx" in tx


def test_release_endpoint_completion_captures_baseline_before_queuing() -> None:
    """Fast host completion during FIFO flush must not strand release admission.

    Regression for real-device C01: Windows received the entire accepted=true
    response, but the Device continued publishing CDC/MSC. Sampling the USB IN
    completion counter after the final flush can miss an already-completed TX.
    """
    src = TRANSPORT.read_text(encoding="utf-8")
    tx_start = src.index("static bool cdc_write_response(")
    tx_end = src.index("static void usb_cdc_rx_callback", tx_start)
    tx = src[tx_start:tx_end]
    snapshot = tx.index(".tx_completed_before = release_accepted")
    write = tx.index("usb_cdc_tx_write_response(")
    assert snapshot < write
    assert "&s_cdc_tx_completed" in tx[snapshot:write]
    assert "memory_order_acquire" in tx[snapshot:write]

    wait_start = src.index("static bool cdc_release_await_endpoint(")
    wait_end = src.index("static bool cdc_write_response(", wait_start)
    wait = src[wait_start:wait_end]
    sample_start = src.index("static bool cdc_release_wait_sample(")
    sample_end = src.index("static uint32_t cdc_release_now_ticks(", sample_start)
    sample = src[sample_start:sample_end]
    # The exact production wait now runs in portable C with an injected
    # TinyUSB snapshot seam, exercised by a dynamic release coordinator test.
    assert "atomic_load_explicit(&s_cdc_tx_completed" in sample
    assert "tud_cdc_n_write_available(" in sample
    assert "usbd_edpt_busy(" in sample
    assert "response->tx_completed_before" in wait
    assert "usb_cdc_tx_wait_final_in(&wait_ops)" in wait
    assert "CDC_RELEASE_TX_COMPLETE_TIMEOUT_MS" in src
    assert "CDC_RELEASE_HOST_READ_GRACE_MS" in src
    assert "cdc_release_wait_current" in wait

    # No completion notification still fails closed; admission is never
    # converted into teardown permission based solely on a successful FIFO
    # flush or the PC's later transport disappearance.
    tx_file = TX.read_text(encoding="utf-8")
    assert "if (ops->await_endpoint != NULL &&" in tx_file
    assert "!ops->await_endpoint(ops->transport_ctx" in tx_file


def test_release_final_in_proof_rejects_old_and_partial_completions(
    tmp_path: Path,
) -> None:
    """REV-83-13: execute the production completion predicate against races."""
    harness = tmp_path / "cdc_final_endpoint_proof.c"
    harness.write_text(
        r"""
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include "usb_cdc_tx.h"

#define CHECK(v) do { if (!(v)) { \
    fprintf(stderr, "failed at %d\n", __LINE__); exit(2); \
} } while (0)

int main(void) {
    const unsigned before = 20u;
    unsigned after = before;
    bool fifo_drained = false;
    bool endpoint_busy = true;

    /* Queue accepted=true while an older response remains in flight. */
    CHECK(!usb_cdc_tx_final_in_complete(
        before, after, fifo_drained, endpoint_busy));
    after++; /* The old response completes, but RELEASE_STORAGE remains IN-flight. */
    fifo_drained = true;
    CHECK(!usb_cdc_tx_final_in_complete(
        before, after, fifo_drained, endpoint_busy));

    /* First chunk of a split release response completes; tail still queued. */
    fifo_drained = false;
    endpoint_busy = false;
    after++;
    CHECK(!usb_cdc_tx_final_in_complete(
        before, after, fifo_drained, endpoint_busy));

    /* Tail was scheduled and FIFO drained but the final IN never completes. */
    fifo_drained = true;
    endpoint_busy = true;
    CHECK(!usb_cdc_tx_final_in_complete(
        before, after, fifo_drained, endpoint_busy));

    /* Final packet completion plus drained FIFO and idle endpoint: success. */
    endpoint_busy = false;
    after++;
    CHECK(usb_cdc_tx_final_in_complete(
        before, after, fifo_drained, endpoint_busy));

    /* No TX completion, even with empty FIFO/idle endpoint: fail closed. */
    CHECK(!usb_cdc_tx_final_in_complete(
        before, before, true, false));
    /* A session-invalidated release is rejected by the transport's
       session_is_current gate before this predicate can authorize teardown. */
    puts("CDC final IN completion proof: PASS");
    return 0;
}
""",
        encoding="utf-8",
    )
    exe = tmp_path / "cdc_final_endpoint_proof"
    build = subprocess.run(
        ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-I", str(INCLUDE), str(TX), str(harness), "-o", str(exe)],
        cwd=REPO, text=True, capture_output=True, check=False,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(exe)], cwd=REPO, text=True, capture_output=True, check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "CDC final IN completion proof: PASS" in run.stdout


def test_release_proof_wired_to_active_bulk_in_and_session_fence() -> None:
    """Production wiring must check both hardware endpoint and class FIFO."""
    src = TRANSPORT.read_text(encoding="utf-8")
    assert "tud_descriptor_configuration_cb(0u)" in src
    assert "TUSB_CLASS_CDC_DATA" in src
    assert "usbd_edpt_busy(0u, wait->ep_in)" in src
    assert "tud_cdc_n_write_available(TINYUSB_CDC_ACM_0)" in src
    assert "cdc_tx_session_is_current(" in src
    assert "usb_cdc_tx_wait_final_in(&wait_ops)" in src
    assert "usb_cdc_tx_complete_accepted_release(" in src
    assert "CDC_RELEASE_HOST_READ_GRACE_MS" in src


def test_production_final_in_wait_and_release_authorization(tmp_path: Path) -> None:
    """REV-83-14: execute real wait + writer + coordinator gate, not mocks."""
    executable = tmp_path / "usb_cdc_release_wait"
    harness = REPO / "firmware/tests/native/usb_cdc_release_wait_harness.c"
    build = subprocess.run(
        ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-I", str(INCLUDE), str(TX), str(harness), "-o", str(executable)],
        cwd=REPO, text=True, capture_output=True, check=False,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(executable)], cwd=REPO, text=True, capture_output=True, check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "production final-IN wait and release authorization: PASS" in run.stdout
