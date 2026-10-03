"""Production-C regressions for Task #50 review findings REV-83-03/04."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
INCLUDE = COMP / "include"
CORE = COMP / "usb_cdc_protocol_core.c"
HARNESS = REPO / "firmware/tests/native/usb_cdc_protocol_harness.c"
CJSON_STUB = REPO / "firmware/tests/native/cjson_stub.c"
STUB_INCLUDE = REPO / "firmware/tests/native/include"
GATE = COMP / "usb_cdc_session_gate.c"
GATE_HEADER = INCLUDE / "usb_cdc_session_gate.h"
GATE_HARNESS = REPO / "firmware/tests/native/usb_cdc_session_gate_harness.c"
TRANSPORT = COMP / "usb_cdc_protocol.c"
RUNTIME = REPO / "firmware/main/task50_runtime.c"


def test_canonical_protocol_vectors_execute_production_c(tmp_path: Path) -> None:
    """AC-11 executes the C parser/dispatcher/framer used by firmware."""

    for required in (CORE, HARNESS, CJSON_STUB):
        assert required.exists(), f"missing production/native-test source: {required}"

    executable = tmp_path / "usb_cdc_protocol_harness"
    compile_result = subprocess.run(
        [
            "cc",
            "-std=c11",
            "-D_POSIX_C_SOURCE=200809L",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-include",
            "stdio.h",
            "-I",
            str(STUB_INCLUDE),
            "-I",
            str(INCLUDE),
            str(CORE),
            str(CJSON_STUB),
            str(HARNESS),
            "-o",
            str(executable),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert compile_result.returncode == 0, compile_result.stderr

    run_result = subprocess.run(
        [str(executable)],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert run_result.returncode == 0, run_result.stdout + run_result.stderr
    assert "production protocol vectors: PASS" in run_result.stdout


def test_detach_reset_dynamically_blocks_inflight_and_stale_commands(
    tmp_path: Path,
) -> None:
    """Execute the production session gate across SET_TIME/detach overlap."""

    for required in (GATE, GATE_HEADER, GATE_HARNESS):
        assert required.exists(), f"missing production overlap source: {required}"

    executable = tmp_path / "usb_cdc_session_gate_harness"
    compile_result = subprocess.run(
        [
            "cc",
            "-std=c11",
            "-D_POSIX_C_SOURCE=200809L",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pthread",
            "-I",
            str(INCLUDE),
            str(GATE),
            str(GATE_HARNESS),
            "-o",
            str(executable),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert compile_result.returncode == 0, compile_result.stderr

    run_result = subprocess.run(
        [str(executable)],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert run_result.returncode == 0, run_result.stdout + run_result.stderr
    assert "production session gate overlap: PASS" in run_result.stdout


def test_transport_and_runtime_use_the_production_teardown_barrier() -> None:
    """Wire the dynamically tested gate around the real command path/cutoff."""

    src = TRANSPORT.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")

    assert '#include "usb_cdc_session_gate.h"' in src
    worker_at = src.index("static void usb_cdc_worker_task")
    reset_at = src.index("void usb_cdc_protocol_reset_session", worker_at)
    worker = src[worker_at:reset_at]
    process_at = worker.index("usb_cdc_protocol_process_line")
    begin_at = worker.rfind("usb_cdc_session_gate_command_begin", 0, process_at)
    end_at = worker.index("usb_cdc_session_gate_command_end", process_at)
    assert begin_at >= 0
    assert begin_at < process_at < end_at

    init_at = src.index("esp_err_t usb_cdc_protocol_init", reset_at)
    reset = src[reset_at:init_at]
    assert "s_connected = false" in reset
    assert "usb_cdc_session_gate_reset" in reset

    # Device remount/flush/restart begins only after that blocking reset returns.
    detach_at = runtime.index("static void recorder_handle_usb_detach")
    app_main_at = runtime.index("void app_main", detach_at)
    detach = runtime[detach_at:app_main_at]
    reset_call = detach.index("usb_cdc_protocol_reset_session();")
    remount = detach.index("sd_mount_remount_after_usb")
    flush = detach.index("rtc_correction_flush_pending_event", remount)
    restart = detach.index("recorder_start_session", flush)
    assert reset_call < remount < flush < restart
