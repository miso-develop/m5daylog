"""Task #50 production-C CDC v1 application command regressions."""

from pathlib import Path
import subprocess

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
INCLUDE = COMP / "include"
STUB_INCLUDE = REPO / "firmware/tests/native/include"


def test_application_commands_execute_canonical_production_dispatcher(tmp_path: Path) -> None:
    exe = tmp_path / "cdc_v1_application"
    result = subprocess.run(
        [
            "cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L",
            "-Wall", "-Wextra", "-Werror", "-include", "stdio.h",
            "-I", str(STUB_INCLUDE), "-I", str(INCLUDE),
            str(COMP / "usb_cdc_protocol_core.c"),
            str(REPO / "firmware/tests/native/cjson_stub.c"),
            str(REPO / "firmware/tests/native/usb_cdc_protocol_harness.c"),
            "-o", str(exe),
        ],
        cwd=REPO, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    run = subprocess.run(
        [str(exe)], cwd=REPO, text=True, capture_output=True, check=False
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "cdc v1 application protocol production core: PASS" in run.stdout


def test_strategy2_runtime_has_no_release_time_rtc_or_remount_path() -> None:
    runtime = (REPO / "firmware/main/task49_runtime.c").read_text(encoding="utf-8")
    release_at = runtime.index("static void recorder_handle_usb_release")
    end = runtime.index("static void recorder_shutdown_armed_now", release_at)
    release = runtime[release_at:end]
    assert "rtc_correction_flush_pending_event" not in release
    assert "sd_mount_remount_after_usb" not in runtime
    assert "USB_MSC_EVENT_DETACH" not in runtime
    assert "usb_cdc_protocol_reset_session();" in release
    assert "usb_msc_ownership_complete_release_quiesce" in release


def test_manual_wake_uses_task87_recovery_hook_and_fresh_session_gate() -> None:
    runtime = (REPO / "firmware/main/task49_runtime.c").read_text(encoding="utf-8")
    wake = (REPO / "firmware/main/task87_wake_recovery.c").read_text(encoding="utf-8")
    rtc = (REPO / "firmware/components/recorder/rtc_correction.c").read_text(encoding="utf-8")

    assert "rtc_correction_init" in runtime
    assert "rtc_correction_flush_pending_event" in wake
    assert "task87_wake_recovery_complete_device_recovery" in runtime
    assert "task87_wake_recovery_usb_rearm_allowed" in runtime

    apply_at = rtc.index("rtc_correction_result_t rtc_correction_apply(")
    flush_at = rtc.index("rtc_correction_flush_pending_event", apply_at)
    apply = rtc[apply_at:flush_at]
    assert "nvs_set_blob" in apply and "nvs_commit" in apply
    assert "fopen(" not in apply and "RECORDER_EVENTS_PATH" not in apply


def test_release_acceptance_closes_admission_before_any_later_command() -> None:
    transport = (COMP / "usb_cdc_protocol.c").read_text(encoding="utf-8")
    worker_at = transport.index("static void usb_cdc_worker_task")
    reset_at = transport.index("void usb_cdc_protocol_reset_session", worker_at)
    worker = transport[worker_at:reset_at]
    process_at = worker.index("usb_cdc_protocol_process_line")
    gate_after = worker.index("if (!cdc_lifecycle_admission_open())", process_at)
    assert gate_after > process_at
    assert "usb_cdc_protocol_framer_reset" in worker[gate_after:]
