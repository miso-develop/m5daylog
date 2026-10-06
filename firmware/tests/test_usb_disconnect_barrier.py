"""Task #87 Strategy 2 source-order contracts.

Behavioral host tests are the primary proof. These assertions lock two-phase
MSC publication, the explicit-eject seam, durable ownership ordering, and the
absence of same-session APP remount.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
USB_C = REPO / "firmware/components/recorder/usb_msc_ownership.c"
SD_C = REPO / "firmware/components/recorder/sd_mount.c"
RUNTIME = REPO / "firmware/main/task49_runtime.c"
WAKE_RECOVERY = REPO / "firmware/main/task87_wake_recovery.c"
MAIN_CMAKE = REPO / "firmware/main/CMakeLists.txt"


def test_usb_status_boundaries_use_completion_callbacks_not_linker_wraps():
    usb = USB_C.read_text(encoding="utf-8")
    cmake = MAIN_CMAKE.read_text(encoding="utf-8")
    assert "__wrap_tud_mount_cb" not in usb
    assert "__real_tud_mount_cb" not in usb
    assert "--wrap=tud_mount_cb" not in cmake
    assert "__wrap_tud_msc_start_stop_cb" not in usb
    assert "__real_tud_msc_start_stop_cb" not in usb
    assert "--wrap=tud_msc_start_stop_cb" not in cmake
    assert "tud_msc_scsi_complete_cb" in usb


def test_publish_persists_unresolved_before_storage_transfer_and_reconnect():
    usb = USB_C.read_text(encoding="utf-8")
    publish_at = usb.index("static bool usb_msc_publish")
    unresolved_at = usb.index("shutdown_armed_mark_host_unresolved", publish_at)
    authorize_at = usb.index("s_transfer_authorized = true", unresolved_at)
    transfer_at = usb.index("sd_mount_transfer_to_usb", authorize_at)
    delay_at = usb.index("vTaskDelay", transfer_at)
    reconnect_at = usb.index("tud_connect", delay_at)
    assert publish_at < unresolved_at < authorize_at < transfer_at < delay_at < reconnect_at


def test_storage_mount_start_is_detached_pre_authorized_validation_seam():
    usb = USB_C.read_text(encoding="utf-8")
    cb_at = usb.index("static void usb_storage_event_cb")
    start_at = usb.index("TINYUSB_MSC_EVENT_MOUNT_START", cb_at)
    complete_at = usb.index("TINYUSB_MSC_EVENT_MOUNT_COMPLETE", start_at)
    region = usb[start_at:complete_at]
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


def test_initial_attach_preserves_set_configuration_until_no_media_status():
    usb = USB_C.read_text(encoding="utf-8")
    cb_at = usb.index("static void usb_device_event_cb")
    attached_at = usb.index("TINYUSB_EVENT_ATTACHED", cb_at)
    suspend_at = usb.index("#ifdef CONFIG_TINYUSB_SUSPEND_CALLBACK", attached_at)
    region = usb[attached_at:suspend_at]
    provisional_at = region.index("s_provisional_attached = true")
    first_branch_end = region.index("if (!s_host_owned)", provisional_at)
    provisional = region[:first_branch_end]
    assert "tud_disconnect" not in provisional
    assert "USB_BIT_ATTACH" not in provisional
    assert "s_publish_triggered = true" not in provisional
    assert "sd_mount_transfer_to_usb" not in region

    scsi_at = usb.index("void tud_msc_scsi_complete_cb")
    init_at = usb.index("esp_err_t usb_msc_ownership_init", scsi_at)
    scsi_region = usb[scsi_at:init_at]
    assert "USB_SCSI_CMD_TEST_UNIT_READY" in scsi_region
    assert "usb_note_initial_msc_command_complete" in scsi_region


def test_release_quiesce_tears_down_usb_before_deferred_write_proof_and_durable_arm():
    usb = USB_C.read_text(encoding="utf-8")
    fn_at = usb.index("usb_msc_ownership_complete_release_quiesce")
    teardown_at = usb.index("tinyusb_driver_uninstall", fn_at)
    proof_at = usb.index("sd_mount_release_usb_storage", teardown_at)
    arm_at = usb.index("shutdown_armed_commit", proof_at)
    retire_at = usb.index("s_host_owned = false", arm_at)
    assert teardown_at < proof_at < arm_at < retire_at


def test_release_never_rebuilds_app_storage_same_session():
    sd = SD_C.read_text(encoding="utf-8")
    fn_at = sd.index("esp_err_t sd_mount_release_usb_storage")
    end_at = sd.index("\n}", fn_at)
    region = sd[fn_at:end_at]
    assert "tinyusb_msc_delete_storage" in region
    assert "sd_mount_create_storage" not in region
    assert "TINYUSB_MSC_STORAGE_MOUNT_APP" not in region


def test_runtime_enters_guarded_shutdown_only_after_ownership_quiesce_success():
    runtime = RUNTIME.read_text(encoding="utf-8")
    fn_at = runtime.index("static void recorder_handle_usb_release")
    quiesce_at = runtime.index("usb_msc_ownership_complete_release_quiesce", fn_at)
    shutdown_at = runtime.index("recorder_power_shutdown", quiesce_at)
    assert quiesce_at < shutdown_at

    release_region = runtime[fn_at:runtime.index("\n}", fn_at)]
    assert "shutdown_armed_commit" not in release_region
    assert "recorder_start_session" not in release_region
    assert "sd_mount_remount_after_usb" not in release_region
    assert "usb_msc_ownership_rearm" not in runtime


def test_manual_wake_runtime_delegates_to_production_recovery_seam():
    runtime = RUNTIME.read_text(encoding="utf-8")
    recovery = WAKE_RECOVERY.read_text(encoding="utf-8")

    fn_at = runtime.index("bool recorder_task87_manifest_sync_wav_dir")
    base_call_at = runtime.index("device_manifest_sync_wav_dir(", fn_at)
    recover_at = runtime.index("task87_wake_recovery_complete_device_recovery", base_call_at)
    assert base_call_at < recover_at
    assert "#define device_manifest_sync_wav_dir recorder_task87_manifest_sync_wav_dir" in runtime

    seam_at = recovery.index("task87_wake_recovery_complete_device_recovery")
    rtc_at = recovery.index("rtc_correction_flush_pending_event", seam_at)
    pending_at = recovery.index("shutdown_armed_mark_wake_recovery_pending", rtc_at)
    proof_at = recovery.index("task87_wake_recovery_note_recording_started", pending_at)
    complete_at = recovery.index("shutdown_armed_complete_wake_recovery", proof_at)
    assert seam_at < rtc_at < pending_at < proof_at < complete_at

    wait_at = runtime.index("static bool recorder_wait_initial_recording")
    recording_at = runtime.index("RECORDER_STATE_RECORDING", wait_at)
    fresh_id_at = runtime.index("s_seg_rec_id[0]", recording_at)
    note_at = runtime.index("task87_wake_recovery_note_recording_started", fresh_id_at)
    usb_gate_at = runtime.index("task87_wake_recovery_usb_rearm_allowed", note_at)
    usb_init_at = runtime.index("usb_msc_ownership_init", usb_gate_at)
    assert recording_at < fresh_id_at < note_at < usb_gate_at < usb_init_at
