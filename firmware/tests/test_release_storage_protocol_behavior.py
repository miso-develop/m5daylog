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
    assert "effect.release_accepted && response_complete" in worker

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
