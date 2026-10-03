"""Production-C regressions for Task #50 review findings REV-83-03..08."""

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
PROTOCOL_HEADER = INCLUDE / "usb_cdc_protocol.h"
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


def test_detach_reset_dynamically_blocks_reopen_stale_batches_and_pre_ready_rx(
    tmp_path: Path,
) -> None:
    """Execute the production session gate across teardown/lifecycle races."""

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
    assert "production session gate lifecycle overlap: PASS" in run_result.stdout


def test_transport_and_runtime_use_lifecycle_owned_admission_and_rx_snapshots() -> None:
    """Wire the tested gate to the real RX path and recorder lifecycle cutoff."""

    src = TRANSPORT.read_text(encoding="utf-8")
    header = PROTOCOL_HEADER.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")

    assert '#include "usb_cdc_session_gate.h"' in src
    assert "void usb_cdc_protocol_open_session(void);" in header

    rx_at = src.index("static void usb_cdc_rx_callback")
    line_state_at = src.index("static void usb_cdc_line_state_callback", rx_at)
    worker_at = src.index("static void usb_cdc_worker_task", line_state_at)
    reset_at = src.index("void usb_cdc_protocol_reset_session", worker_at)
    init_at = src.index("esp_err_t usb_cdc_protocol_init", reset_at)

    rx_callback = src[rx_at:line_state_at]
    line_callback = src[line_state_at:worker_at]
    worker = src[worker_at:reset_at]
    reset_and_open = src[reset_at:init_at]

    # TinyUSB transport callbacks may report connectivity/RX, but lifecycle
    # ownership alone decides when commands become executable.
    assert "usb_cdc_session_gate_open" not in rx_callback
    assert "usb_cdc_session_gate_open" not in line_callback
    assert "usb_cdc_session_gate_note_rx" in rx_callback

    # Bind a dequeued batch to the gate state captured before TinyUSB read.
    snapshot_at = worker.index("usb_cdc_session_gate_snapshot")
    read_at = worker.index("tinyusb_cdcacm_read")
    current_check_at = worker.index("usb_cdc_session_gate_snapshot_is_current", read_at)
    discard_check_at = worker.index("usb_cdc_session_gate_should_discard_rx", read_at)
    assert snapshot_at < read_at < current_check_at
    assert snapshot_at < read_at < discard_check_at
    assert "usb_cdc_session_gate_mark_rx_drained" in worker

    process_at = worker.index("usb_cdc_protocol_process_line")
    begin_at = worker.rfind("usb_cdc_session_gate_command_begin", 0, process_at)
    end_at = worker.index("usb_cdc_session_gate_command_end", process_at)
    assert begin_at >= 0
    assert begin_at < process_at < end_at

    assert "s_connected = false" in reset_and_open
    assert "usb_cdc_session_gate_reset" in reset_and_open
    assert "void usb_cdc_protocol_open_session(void)" in reset_and_open
    open_fn_at = reset_and_open.index("void usb_cdc_protocol_open_session(void)")
    open_region = reset_and_open[open_fn_at:]
    assert "while (!usb_cdc_session_gate_open(&s_session_gate))" in open_region
    assert "xTaskNotifyGive(s_worker)" in open_region
    assert "vTaskDelay(1)" in open_region

    # Attach closes before prepare. Host-owned/USB_SYNC is the only lifecycle
    # point that opens command admission. Detach closes again before recovery.
    attach_at = runtime.index("static void recorder_handle_usb_attach")
    host_owned_at = runtime.index("static void recorder_handle_usb_host_owned", attach_at)
    detach_at = runtime.index("static void recorder_handle_usb_detach", host_owned_at)
    app_main_at = runtime.index("void app_main", detach_at)
    attach = runtime[attach_at:host_owned_at]
    host_owned = runtime[host_owned_at:detach_at]
    detach = runtime[detach_at:app_main_at]

    assert "usb_cdc_protocol_reset_session();" in attach
    transition = host_owned.index("RECORDER_STATE_USB_SYNC")
    lifecycle_open = host_owned.index("usb_cdc_protocol_open_session();")
    assert transition < lifecycle_open

    reset_call = detach.index("usb_cdc_protocol_reset_session();")
    remount = detach.index("sd_mount_remount_after_usb")
    flush = detach.index("rtc_correction_flush_pending_event", remount)
    restart = detach.index("recorder_start_session", flush)
    assert reset_call < remount < flush < restart
