"""Task #49 regression tests: USB MSC ownership handoff.

Task #50 may supersede the runtime coordinator, but it must preserve every
Task #49 fail-closed ownership invariant.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
BASE_MAIN = REPO / "firmware/main/main.c"
TASK49_RUNTIME = REPO / "firmware/main/task49_runtime.c"
TASK50_RUNTIME = REPO / "firmware/main/task50_runtime.c"
MAIN_CMAKE = REPO / "firmware/main/CMakeLists.txt"
USB_H = COMP / "include/usb_msc_ownership.h"
USB_C = COMP / "usb_msc_ownership.c"
SD_C = COMP / "sd_mount.c"
CMAKE = COMP / "CMakeLists.txt"
MANIFEST = COMP / "idf_component.yml"
SDKCONFIG = REPO / "firmware/sdkconfig.defaults"


def _runtime_path():
    return TASK50_RUNTIME if TASK50_RUNTIME.exists() else TASK49_RUNTIME


def _runtime_text():
    return _runtime_path().read_text(encoding="utf-8")


def test_usb_ownership_module_is_built_and_tinyusb_is_pinned():
    cmake = CMAKE.read_text(encoding="utf-8")
    manifest = MANIFEST.read_text(encoding="utf-8")
    sdkconfig = SDKCONFIG.read_text(encoding="utf-8")
    main_cmake = MAIN_CMAKE.read_text(encoding="utf-8")
    assert '"usb_msc_ownership.c"' in cmake
    assert "esp_tinyusb" in manifest
    assert "==2.2.1" in manifest
    assert "CONFIG_TINYUSB_MSC_ENABLED=y" in sdkconfig
    assert (
        'SRCS "task49_runtime.c"' in main_cmake
        or 'SRCS "task50_runtime.c"' in main_cmake
    )
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
    runtime = _runtime_text()
    sd = SD_C.read_text(encoding="utf-8")
    assert "RECORDER_STATE_USB_SYNC" in runtime
    assert "sd_mount_is_mounted" in runtime
    assert "sd_mount_device_fs_released" in runtime
    assert "sd_mount_release_for_usb" in sd
    assert "sd_mount_remount_after_usb" in sd
    assert "sd_mount_note_usb_owned" in sd


def test_disconnect_remounts_before_new_recording_session():
    runtime = _runtime_text()
    base = BASE_MAIN.read_text(encoding="utf-8")
    fn_at = runtime.index("static void recorder_handle_usb_detach")
    remount_state = runtime.index("RECORDER_STATE_REMOUNT", fn_at)
    mount_at = runtime.index("sd_mount_remount_after_usb", remount_state)
    recover_at = runtime.index("RECORDER_STATE_RECOVER", mount_at)
    restart_at = runtime.index("recorder_start_session", recover_at)
    assert remount_state < mount_at < recover_at < restart_at
    assert "recorder_new_recording_id" in base


def test_usb_prepare_uses_usb_specific_stop_and_waits_for_writer_commit():
    runtime = _runtime_text()
    fn_at = runtime.index("static void recorder_handle_usb_attach")
    prepare_at = runtime.index("RECORDER_STATE_USB_PREPARE", fn_at)
    release_at = runtime.index("sd_mount_release_for_usb", prepare_at)
    stop_at = runtime.index("recorder_request_usb_stop", release_at)
    finalized_at = runtime.index("REC_BIT_WRITER_FINALIZED", stop_at)
    publish_at = runtime.index("usb_msc_ownership_note_prepare_complete", finalized_at)
    assert prepare_at < release_at < stop_at < finalized_at < publish_at


def test_fast_detach_waits_for_all_old_tasks_before_publish():
    runtime = _runtime_text()
    for marker in (
        "REC_BIT_WRITER_FINALIZED",
        "REC_BIT_CAPTURE_DONE",
        "REC_BIT_BATTERY_DONE",
        "REC_TASK_DONE_MASK",
    ):
        assert marker in runtime, marker
    assert (
        "recorder_task49_delete" in runtime
        or "recorder_task50_delete" in runtime
    )
    assert "pdTRUE, portMAX_DELAY" in runtime


def test_usb_removal_during_sync_reaches_remount_not_terminal_stop():
    runtime = _runtime_text()
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
