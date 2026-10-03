"""Tasks #49/#87 host contract tests: USB MSC ownership handoff.

Stdlib-only source/contract tests. Physical enumeration, repeated cable cycling,
and filesystem integrity remain Device Human-Gate evidence; these tests lock the
fail-closed software ordering and Task #87 software-only disconnect barrier.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
BASE_MAIN = REPO / "firmware/main/main.c"
RUNTIME = REPO / "firmware/main/task49_runtime.c"
MAIN_CMAKE = REPO / "firmware/main/CMakeLists.txt"
USB_H = COMP / "include/usb_msc_ownership.h"
USB_C = COMP / "usb_msc_ownership.c"
SD_H = COMP / "include/sd_mount.h"
SD_C = COMP / "sd_mount.c"
CMAKE = COMP / "CMakeLists.txt"
MANIFEST = COMP / "idf_component.yml"
SDKCONFIG = REPO / "firmware/sdkconfig.defaults"


def test_usb_ownership_module_is_built_and_tinyusb_is_pinned():
    cmake = CMAKE.read_text(encoding="utf-8")
    manifest = MANIFEST.read_text(encoding="utf-8")
    sdkconfig = SDKCONFIG.read_text(encoding="utf-8")
    main_cmake = MAIN_CMAKE.read_text(encoding="utf-8")

    assert '"usb_msc_ownership.c"' in cmake
    assert "esp_tinyusb" in manifest
    assert "==2.2.1" in manifest
    assert "CONFIG_TINYUSB_MSC_ENABLED=y" in sdkconfig
    assert 'SRCS "task49_runtime.c"' in main_cmake
    assert 'SRCS "main.c"' not in main_cmake


def test_usb_publish_gate_requires_finalize_manifest_and_device_fs_release():
    hdr = USB_H.read_text(encoding="utf-8")
    src = USB_C.read_text(encoding="utf-8")

    for marker in (
        "usb_msc_ownership_init",
        "usb_msc_ownership_note_prepare_complete",
        "usb_msc_ownership_is_host_owned",
        "USB_MSC_EVENT_ATTACH",
        "USB_MSC_EVENT_DETACH",
    ):
        assert marker in hdr or marker in src, marker

    publish_at = src.index("usb_msc_publish")
    gate_region = src[publish_at : src.index("usb_storage_event_cb", publish_at)]
    for gate in ("wav_finalized", "manifest_committed", "device_fs_released"):
        assert gate in gate_region, gate
    assert "USB_BIT_PREPARE_OK" in gate_region


def test_attach_callback_blocks_before_physical_usb_ownership():
    src = USB_C.read_text(encoding="utf-8")
    attach_at = src.index("USB_MSC_EVENT_ATTACH")
    wait_at = src.index("USB_BIT_PREPARE_OK", src.index("usb_storage_event_cb"))
    assert attach_at >= 0
    assert wait_at >= 0
    assert "portMAX_DELAY" in src
    assert "TINYUSB_MSC_EVENT_MOUNT_START" in src


def test_usb_sync_never_keeps_device_filesystem_mounted():
    runtime = RUNTIME.read_text(encoding="utf-8")
    sd = SD_C.read_text(encoding="utf-8")

    assert "RECORDER_STATE_USB_SYNC" in runtime
    assert "sd_mount_is_mounted" in runtime
    assert "sd_mount_device_fs_released" in runtime
    assert "sd_mount_release_for_usb" in sd
    assert "sd_mount_remount_after_usb" in sd
    assert "sd_mount_note_usb_owned" in sd


def test_disconnect_remounts_before_new_recording_session():
    runtime = RUNTIME.read_text(encoding="utf-8")
    base = BASE_MAIN.read_text(encoding="utf-8")

    fn_at = runtime.index("static void recorder_handle_usb_detach")
    remount_state = runtime.index("RECORDER_STATE_REMOUNT", fn_at)
    mount_at = runtime.index("sd_mount_remount_after_usb", remount_state)
    recover_at = runtime.index("RECORDER_STATE_RECOVER", mount_at)
    restart_at = runtime.index("recorder_start_session", recover_at)
    assert remount_state < mount_at < recover_at < restart_at
    assert "recorder_new_recording_id" in base


def test_usb_prepare_uses_usb_specific_stop_and_waits_for_writer_commit():
    runtime = RUNTIME.read_text(encoding="utf-8")

    fn_at = runtime.index("static void recorder_handle_usb_attach")
    prepare_at = runtime.index("RECORDER_STATE_USB_PREPARE", fn_at)
    release_at = runtime.index("sd_mount_release_for_usb", prepare_at)
    stop_at = runtime.index("recorder_request_usb_stop", release_at)
    finalized_at = runtime.index("REC_BIT_WRITER_FINALIZED", stop_at)
    publish_at = runtime.index("usb_msc_ownership_note_prepare_complete", finalized_at)
    assert prepare_at < release_at < stop_at < finalized_at < publish_at


def test_fast_detach_waits_for_all_old_tasks_before_publish():
    runtime = RUNTIME.read_text(encoding="utf-8")
    for marker in (
        "REC_BIT_WRITER_FINALIZED",
        "REC_BIT_CAPTURE_DONE",
        "REC_BIT_BATTERY_DONE",
        "REC_TASK_DONE_MASK",
        "recorder_task49_delete",
    ):
        assert marker in runtime, marker
    assert "pdTRUE, portMAX_DELAY" in runtime


def test_usb_removal_during_sync_reaches_remount_not_terminal_stop():
    runtime = RUNTIME.read_text(encoding="utf-8")
    usb = USB_C.read_text(encoding="utf-8")

    assert "USB_MSC_EVENT_DETACH" in usb
    assert "RECORDER_STATE_USB_SYNC" in runtime
    assert "RECORDER_STATE_REMOUNT" in runtime
    assert "RECORDER_STATE_ERROR" in runtime
    assert "RECORDER_REASON_USB" in runtime
    assert "sd_mount_remount_after_usb" in runtime


def test_usb_ownership_logs_metadata_only():
    src = USB_C.read_text(encoding="utf-8").lower()
    for forbidden in ("audio bytes", "transcript", "authorization", "password"):
        assert forbidden not in src
    for marker in ("attach", "detach", "owner", "mount"):
        assert marker in src, marker


def test_task87_suspend_is_only_a_trigger_for_explicit_disconnect_barrier():
    hdr = USB_H.read_text(encoding="utf-8")
    src = USB_C.read_text(encoding="utf-8")
    sdkconfig = SDKCONFIG.read_text(encoding="utf-8")

    assert "CONFIG_TINYUSB_SUSPEND_CALLBACK=y" in sdkconfig
    assert "USB_MSC_EVENT_BARRIER_REQUIRED" in hdr
    assert "TINYUSB_EVENT_SUSPENDED" in src
    suspend_at = src.index("TINYUSB_EVENT_SUSPENDED")
    barrier_at = src.index("USB_BIT_BARRIER_REQUIRED", suspend_at)
    assert "tud_disconnect()" in src[suspend_at:barrier_at]
    # An ambiguous suspend/bus-loss observation cannot directly authorize APP mount.
    assert "sd_mount_transfer_to_app" not in src[suspend_at:barrier_at]


def test_task87_barrier_stops_usb_stack_before_app_mount():
    hdr = USB_H.read_text(encoding="utf-8")
    src = USB_C.read_text(encoding="utf-8")

    assert "usb_msc_ownership_complete_disconnect_barrier" in hdr
    fn_at = src.index("usb_msc_ownership_complete_disconnect_barrier")
    uninstall_at = src.index("tinyusb_driver_uninstall", fn_at)
    quiesced_at = src.index("host-io-quiesced", uninstall_at)
    app_mount_at = src.index("sd_mount_transfer_to_app", quiesced_at)
    proof_at = src.index("s_barrier_app_mounted", app_mount_at)
    detach_at = src.index("USB_BIT_DETACH", proof_at)
    assert uninstall_at < quiesced_at < app_mount_at < proof_at < detach_at


def test_task87_msc_auto_mount_is_disabled_and_attach_gate_remains_blocking():
    sd = SD_C.read_text(encoding="utf-8")
    src = USB_C.read_text(encoding="utf-8")

    assert "tinyusb_msc_install_driver" in sd
    assert ".auto_mount_off = 1" in sd
    for marker in ("sd_mount_transfer_to_usb", "sd_mount_transfer_to_app"):
        assert marker in SD_H.read_text(encoding="utf-8")
        assert marker in sd

    attached_at = src.index("TINYUSB_EVENT_ATTACHED")
    wait_at = src.index("USB_BIT_PREPARE_OK", attached_at)
    to_usb_at = src.index("sd_mount_transfer_to_usb", wait_at)
    host_owned_at = src.index("s_host_owned", to_usb_at)
    assert attached_at < wait_at < to_usb_at < host_owned_at


def test_task87_barrier_failure_is_fail_closed_without_remount():
    src = USB_C.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")

    fn_at = src.index("usb_msc_ownership_complete_disconnect_barrier")
    uninstall_at = src.index("tinyusb_driver_uninstall", fn_at)
    fail_return_at = src.index("return", uninstall_at)
    app_mount_at = src.index("sd_mount_transfer_to_app", uninstall_at)
    assert uninstall_at < fail_return_at < app_mount_at

    handler_at = runtime.index("static void recorder_handle_usb_barrier")
    complete_at = runtime.index("usb_msc_ownership_complete_disconnect_barrier", handler_at)
    detach_at = runtime.index("recorder_handle_usb_detach", complete_at)
    error_at = runtime.index("recorder_enter_error", complete_at)
    assert complete_at < error_at < detach_at


def test_task87_fresh_usb_session_is_rearmed_only_after_recording_restart():
    hdr = USB_H.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")

    assert "usb_msc_ownership_rearm" in hdr
    fn_at = runtime.index("static void recorder_handle_usb_detach")
    restart_at = runtime.index("recorder_start_session", fn_at)
    ready_at = runtime.index("recorder_wait_initial_recording", restart_at)
    rearm_at = runtime.index("usb_msc_ownership_rearm", ready_at)
    assert restart_at < ready_at < rearm_at
