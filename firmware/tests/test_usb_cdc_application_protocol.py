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


def test_physical_reconnect_wires_nonblocking_cutoff_then_blocking_reopen() -> None:
    transport = (COMP / "usb_cdc_protocol.c").read_text(encoding="utf-8")
    runtime = (REPO / "firmware/main/task49_runtime.c").read_text(encoding="utf-8")

    close_at = transport.index("void usb_cdc_protocol_close_session(void)")
    reset_at = transport.index("void usb_cdc_protocol_reset_session(void)", close_at)
    close = transport[close_at:reset_at]
    assert "usb_cdc_session_gate_close" in close
    assert "usb_cdc_session_gate_reset" not in close
    assert "s_connected = false" in close

    callback_at = runtime.index("static void recorder_cdc_physical_session_cutoff")
    status_at = runtime.index("static bool recorder_cdc_status", callback_at)
    assert "usb_cdc_protocol_close_session();" in runtime[callback_at:status_at]
    assert "usb_msc_ownership_set_physical_session_cutoff" in runtime

    reconnect_at = runtime.index("static void recorder_handle_usb_host_reattached")
    release_at = runtime.index("static void recorder_handle_usb_release", reconnect_at)
    reconnect = runtime[reconnect_at:release_at]
    reset_call = reconnect.index("usb_cdc_protocol_reset_session();")
    open_call = reconnect.index("usb_cdc_protocol_open_session();")
    assert reset_call < open_call
    assert "usb_msc_ownership_is_host_owned()" in reconnect
    assert "sd_mount_is_mounted()" in reconnect

def test_rtc_transport_failure_does_not_block_pending_state_boot_gate() -> None:
    rtc = (COMP / "rtc_correction.c").read_text(encoding="utf-8")
    runtime = (REPO / "firmware/main/task49_runtime.c").read_text(encoding="utf-8")

    init_at = rtc.index("esp_err_t rtc_correction_init(void)")
    pending_at = rtc.index("bool rtc_correction_is_pending(void)", init_at)
    init = rtc[init_at:pending_at]
    assert "return nvs_err;" in init
    assert "return hw_err;" not in init

    apply_at = rtc.index("rtc_correction_result_t rtc_correction_apply(")
    clear_at = rtc.index("static esp_err_t rtc_correction_clear_pending_nvs", apply_at)
    apply = rtc[apply_at:clear_at]
    assert "if (!s_hw_ready && rtc_hw_init() != ESP_OK)" in apply

    app_at = runtime.index("void app_main(void)")
    app = runtime[app_at:]
    assert (
        "if (action == SHUTDOWN_ARMED_BOOT_MANUAL_RESUME &&\n"
        "        rtc_correction_init() != ESP_OK)"
    ) in app
    assert (
        "if (action == SHUTDOWN_ARMED_BOOT_NORMAL &&\n"
        "        rtc_correction_init() != ESP_OK)"
    ) in app

    recording_wait = app.index("recorder_wait_initial_recording()")
    normal_rtc_init = app.index(
        "if (action == SHUTDOWN_ARMED_BOOT_NORMAL &&\n"
        "        rtc_correction_init() != ESP_OK)"
    )
    usb_init = app.index("usb_msc_ownership_init()")
    assert recording_wait < normal_rtc_init < usb_init

