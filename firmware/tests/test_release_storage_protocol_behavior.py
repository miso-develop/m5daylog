"""D-031 / Task #87 production-C RELEASE_STORAGE regressions."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
INCLUDE = COMP / "include"
CORE = COMP / "usb_cdc_protocol_core.c"
TRANSPORT = COMP / "usb_cdc_protocol.c"
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
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
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
    assert "release_accept" not in src[line_at:wait_at]


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
