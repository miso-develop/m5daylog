"""Production-C regressions for Task #50 review findings REV-83-03/04."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
INCLUDE = COMP / "include"
CORE = COMP / "usb_cdc_protocol_core.c"
RTC_PARSE = COMP / "rtc_correction_parse.c"
HARNESS = REPO / "firmware/tests/native/usb_cdc_protocol_harness.c"
CJSON_STUB = REPO / "firmware/tests/native/cjson_stub.c"
STUB_INCLUDE = REPO / "firmware/tests/native/include"
TRANSPORT = COMP / "usb_cdc_protocol.c"
RUNTIME = REPO / "firmware/main/task50_runtime.c"


def test_canonical_protocol_vectors_execute_production_c(tmp_path: Path) -> None:
    """AC-11 must execute the C parser/dispatcher/framer used by firmware."""

    for required in (CORE, RTC_PARSE, HARNESS, CJSON_STUB):
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
            "-I",
            str(STUB_INCLUDE),
            "-I",
            str(INCLUDE),
            str(CORE),
            str(RTC_PARSE),
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


def test_detach_reset_is_a_barrier_for_inflight_command_execution() -> None:
    """A complete SET_TIME may not mutate after detach flush/restart cutoff."""

    src = TRANSPORT.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")

    # The transport owns a command-execution mutex. Worker processing happens
    # under that mutex and re-checks the session state after acquiring it.
    assert "s_command_lock" in src
    worker_at = src.index("static void usb_cdc_worker_task")
    reset_at = src.index("void usb_cdc_protocol_reset_session", worker_at)
    worker = src[worker_at:reset_at]
    process_at = worker.index("usb_cdc_protocol_process_line")
    take_at = worker.rfind("xSemaphoreTake(s_command_lock", 0, process_at)
    assert take_at >= 0
    assert "s_connected" in worker[take_at:process_at]
    give_at = worker.index("xSemaphoreGive(s_command_lock", process_at)
    assert take_at < process_at < give_at

    # reset_session closes admission first, then waits for the command lock.
    # Therefore it cannot return while a previously admitted SET_TIME remains
    # inside the production dispatcher/RTC transaction.
    init_at = src.index("esp_err_t usb_cdc_protocol_init", reset_at)
    reset = src[reset_at:init_at]
    close_at = reset.index("s_connected = false")
    barrier_at = reset.index("xSemaphoreTake(s_command_lock")
    release_at = reset.index("xSemaphoreGive(s_command_lock", barrier_at)
    assert close_at < barrier_at < release_at

    # Device remount/flush/restart begins only after that barrier returns.
    detach_at = runtime.index("static void recorder_handle_usb_detach")
    app_main_at = runtime.index("void app_main", detach_at)
    detach = runtime[detach_at:app_main_at]
    reset_call = detach.index("usb_cdc_protocol_reset_session();")
    remount = detach.index("sd_mount_remount_after_usb")
    flush = detach.index("rtc_correction_flush_pending_event", remount)
    restart = detach.index("recorder_start_session", flush)
    assert reset_call < remount < flush < restart
