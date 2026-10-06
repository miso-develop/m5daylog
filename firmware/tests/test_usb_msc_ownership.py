"""Tasks #49/#87 source contracts for Strategy 2 USB ownership.

Executable host-C tests carry the behavioral proof. These checks protect the
pre-configuration APP -> USB gate, the real esp_tinyusb callback ordering, and
reject regressions to automatic suspend/detach ownership return.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
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
    main_cmake = MAIN_CMAKE.read_text(encoding="utf-8")
    sd = SD_C.read_text(encoding="utf-8")

    assert '"usb_msc_ownership.c"' in cmake
    assert '"shutdown_armed.c"' in cmake
    assert "esp_tinyusb" in manifest
    assert "==2.2.1" in manifest
    assert 'SRCS "task49_runtime.c"' in main_cmake
    # Publication no longer depends on intercepting esp_tinyusb's mount
    # callback. Only the explicit-eject START STOP compatibility seam remains.
    assert "--wrap=tud_mount_cb" not in main_cmake
    assert "--wrap=tud_msc_start_stop_cb" in main_cmake
    assert ".auto_mount_off = 1" in sd


def test_usb_publish_gate_requires_finalize_manifest_and_device_fs_release():
    hdr = USB_H.read_text(encoding="utf-8")
    src = USB_C.read_text(encoding="utf-8")

    for marker in (
        "usb_msc_ownership_init",
        "usb_msc_ownership_note_prepare_complete",
        "usb_msc_ownership_is_host_owned",
        "USB_MSC_EVENT_ATTACH",
        "USB_MSC_EVENT_HOST_OWNED",
    ):
        assert marker in hdr or marker in src, marker

    publish_at = src.index("usb_msc_publish")
    gate_region = src[publish_at : src.index("usb_storage_event_cb", publish_at)]
    for gate in ("wav_finalized", "manifest_committed", "device_fs_released"):
        assert gate in gate_region, gate
    unresolved_at = gate_region.index("shutdown_armed_mark_host_unresolved")
    authorize_at = gate_region.index("s_transfer_authorized = true", unresolved_at)
    transfer_at = gate_region.index("sd_mount_transfer_to_usb", authorize_at)
    delay_at = gate_region.index("vTaskDelay", transfer_at)
    reconnect_at = gate_region.index("tud_connect", delay_at)
    assert unresolved_at < authorize_at < transfer_at < delay_at < reconnect_at


def test_storage_mount_start_only_accepts_pre_authorized_detached_transfer():
    src = USB_C.read_text(encoding="utf-8")
    cb_at = src.index("static void usb_storage_event_cb")
    start_at = src.index("TINYUSB_MSC_EVENT_MOUNT_START", cb_at)
    complete_at = src.index("TINYUSB_MSC_EVENT_MOUNT_COMPLETE", start_at)
    region = src[start_at:complete_at]

    for marker in (
        "s_publish_triggered",
        "s_transfer_authorized",
        "s_wav_finalized",
        "s_manifest_committed",
        "s_device_fs_released",
    ):
        assert marker in region, marker
    assert "xEventGroupWaitBits" not in region
    assert "USB_BIT_ATTACH" not in region


def test_real_attached_callback_disconnects_before_publication_work():
    src = USB_C.read_text(encoding="utf-8")
    device_cb_at = src.index("static void usb_device_event_cb")
    attached_at = src.index("TINYUSB_EVENT_ATTACHED", device_cb_at)
    suspend_at = src.index("#ifdef CONFIG_TINYUSB_SUSPEND_CALLBACK", attached_at)
    attached_region = src[attached_at:suspend_at]

    trigger_at = attached_region.index("s_publish_triggered = true")
    disconnect_at = attached_region.index("tud_disconnect", trigger_at)
    attach_at = attached_region.index("USB_BIT_ATTACH", disconnect_at)
    assert trigger_at < disconnect_at < attach_at
    assert "sd_mount_transfer_to_usb" not in attached_region


def test_usb_event_coordinator_runs_before_tinyusb_driver_start():
    runtime = RUNTIME.read_text(encoding="utf-8")

    worker_at = runtime.index("static void recorder_usb_event_task")
    app_at = runtime.index("void app_main")
    create_at = runtime.index("xTaskCreate(recorder_usb_event_task", app_at)
    start_at = runtime.index("usb_msc_ownership_start()", create_at)
    assert worker_at < app_at < create_at < start_at


def test_usb_sync_keeps_device_filesystem_unmounted():
    runtime = RUNTIME.read_text(encoding="utf-8")
    sd = SD_C.read_text(encoding="utf-8")

    assert "RECORDER_STATE_USB_SYNC" in runtime
    assert "sd_mount_is_mounted" in runtime
    assert "sd_mount_device_fs_released" in runtime
    assert "sd_mount_release_for_usb" in sd
    assert "sd_mount_note_usb_owned" in sd


def test_usb_prepare_uses_usb_specific_stop_and_waits_for_writer_commit():
    runtime = RUNTIME.read_text(encoding="utf-8")

    fn_at = runtime.index("static void recorder_handle_usb_attach")
    prepare_at = runtime.index("RECORDER_STATE_USB_PREPARE", fn_at)
    release_at = runtime.index("sd_mount_release_for_usb", prepare_at)
    stop_at = runtime.index("recorder_request_usb_stop", release_at)
    finalized_at = runtime.index("REC_BIT_WRITER_FINALIZED", stop_at)
    publish_at = runtime.index("usb_msc_ownership_note_prepare_complete", finalized_at)
    assert prepare_at < release_at < stop_at < finalized_at < publish_at


def test_ambiguous_suspend_and_detach_never_authorize_release():
    src = USB_C.read_text(encoding="utf-8")
    sdkconfig = SDKCONFIG.read_text(encoding="utf-8")

    assert "CONFIG_TINYUSB_SUSPEND_CALLBACK=y" in sdkconfig
    callback_at = src.index("static void usb_device_event_cb")
    compatibility_at = src.index("// Compatibility seam for the original implementation.", callback_at)
    ambiguous_region = src[callback_at:compatibility_at]
    assert "TINYUSB_EVENT_SUSPENDED" in ambiguous_region
    assert "TINYUSB_EVENT_DETACHED" in ambiguous_region
    suspend_region = ambiguous_region[ambiguous_region.index("TINYUSB_EVENT_SUSPENDED"):]
    assert "USB_BIT_RELEASE_REQUESTED" not in suspend_region
    assert "sd_mount_release_usb_storage" not in suspend_region
    assert "ambiguous" in suspend_region.lower()


def test_explicit_eject_is_observed_by_real_scsi_completion_seam():
    hdr = USB_H.read_text(encoding="utf-8")
    src = USB_C.read_text(encoding="utf-8")

    assert "USB_MSC_EVENT_RELEASE_REQUESTED" in hdr

    helper_at = src.index("static bool usb_request_explicit_eject")
    wrapper_at = src.index("__wrap_tud_msc_start_stop_cb", helper_at)
    helper_region = src[helper_at:wrapper_at]
    assert "USB_BIT_RELEASE_REQUESTED" in helper_region
    assert "tud_disconnect" in helper_region
    assert "s_release_pending" in helper_region

    callback_at = src.index("void tud_msc_scsi_complete_cb", wrapper_at)
    next_fn = src.index("usb_msc_ownership_init", callback_at)
    callback_region = src[callback_at:next_fn]
    assert "USB_SCSI_CMD_START_STOP_UNIT" in callback_region
    assert "scsi_cmd[4] & 0x02u" in callback_region
    assert "scsi_cmd[4] & 0x01u" in callback_region
    assert "usb_request_explicit_eject" in callback_region

    wrapper_region = src[wrapper_at:callback_at]
    assert "load_eject" in wrapper_region
    assert "!start" in wrapper_region
    assert "usb_request_explicit_eject" in wrapper_region


def test_release_quiescence_stops_usb_before_storage_release_and_durable_arm():
    hdr = USB_H.read_text(encoding="utf-8")
    src = USB_C.read_text(encoding="utf-8")
    sd_hdr = SD_H.read_text(encoding="utf-8")
    sd = SD_C.read_text(encoding="utf-8")

    assert "usb_msc_ownership_complete_release_quiesce" in hdr
    assert "sd_mount_release_usb_storage" in sd_hdr
    fn_at = src.index("usb_msc_ownership_complete_release_quiesce")
    uninstall_at = src.index("tinyusb_driver_uninstall", fn_at)
    release_at = src.index("sd_mount_release_usb_storage", uninstall_at)
    arm_at = src.index("shutdown_armed_commit", release_at)
    retire_at = src.index("s_host_owned = false", arm_at)
    assert uninstall_at < release_at < arm_at < retire_at

    sd_fn = sd.index("esp_err_t sd_mount_release_usb_storage")
    sd_region = sd[sd_fn:sd.index("\n}", sd_fn)]
    assert "tinyusb_msc_delete_storage" in sd_region
    assert "TINYUSB_MSC_STORAGE_MOUNT_APP" not in sd_region
    assert "sd_mount_create_storage" not in sd_region


def test_runtime_release_enters_persistent_shutdown_not_same_session_restart():
    runtime = RUNTIME.read_text(encoding="utf-8")
    fn_at = runtime.index("static void recorder_handle_usb_release")
    end_at = runtime.index("\n}", fn_at)
    region = runtime[fn_at:end_at]

    for marker in (
        "usb_msc_ownership_complete_release_quiesce",
        "recorder_power_shutdown",
    ):
        assert marker in region, marker
    # HOLD release + wake-source disable + deep-sleep ordering is executed by
    # test_recorder_power_behavior.py against the production power module.
    assert "shutdown_armed_commit" not in region
    for forbidden in (
        "sd_mount_remount_after_usb",
        "recorder_start_session",
        "usb_msc_ownership_rearm",
    ):
        assert forbidden not in runtime, forbidden


def test_armed_boot_gate_precedes_recorder_and_usb_publication():
    runtime = RUNTIME.read_text(encoding="utf-8")
    app_at = runtime.index("void app_main")
    gate_at = runtime.index("shutdown_armed_boot_action", app_at)
    task_at = runtime.index("xTaskCreate(recorder_base_task", gate_at)
    usb_at = runtime.index("usb_msc_ownership_init", task_at)
    assert gate_at < task_at < usb_at
    assert "SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN" in runtime[app_at:task_at]
    assert "SHUTDOWN_ARMED_BOOT_MANUAL_RESUME" in runtime[app_at:task_at]


def test_usb_ownership_logs_metadata_only():
    src = USB_C.read_text(encoding="utf-8").lower()
    for forbidden in ("audio bytes", "transcript", "authorization", "password"):
        assert forbidden not in src
    for marker in ("attach", "owner", "mount", "eject"):
        assert marker in src, marker
