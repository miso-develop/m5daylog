"""Task #87 Strategy 2 source-order contracts.

Behavioral host tests are the primary proof. These assertions additionally
lock the release ordering and prevent a regression back to same-session APP
remount/rearm.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
USB_C = REPO / "firmware/components/recorder/usb_msc_ownership.c"
SD_C = REPO / "firmware/components/recorder/sd_mount.c"
RUNTIME = REPO / "firmware/main/task49_runtime.c"


def test_release_quiesce_tears_down_usb_before_deferred_write_proof():
    usb = USB_C.read_text(encoding="utf-8")
    fn_at = usb.index("usb_msc_ownership_complete_release_quiesce")
    teardown_at = usb.index("tinyusb_driver_uninstall", fn_at)
    proof_at = usb.index("sd_mount_release_usb_storage", teardown_at)
    assert teardown_at < proof_at


def test_release_never_rebuilds_app_storage_same_session():
    sd = SD_C.read_text(encoding="utf-8")
    fn_at = sd.index("esp_err_t sd_mount_release_usb_storage")
    end_at = sd.index("\n}", fn_at)
    region = sd[fn_at:end_at]
    assert "tinyusb_msc_delete_storage" in region
    assert "sd_mount_create_storage" not in region
    assert "TINYUSB_MSC_STORAGE_MOUNT_APP" not in region


def test_runtime_arms_before_hold_release_and_never_restarts_recording():
    runtime = RUNTIME.read_text(encoding="utf-8")
    fn_at = runtime.index("static void recorder_handle_usb_release")
    quiesce_at = runtime.index("usb_msc_ownership_complete_release_quiesce", fn_at)
    arm_at = runtime.index("shutdown_armed_commit", quiesce_at)
    hold_at = runtime.index("recorder_power_release_hold", arm_at)
    assert quiesce_at < arm_at < hold_at

    release_region = runtime[fn_at:runtime.index("\n}", fn_at)]
    assert "recorder_start_session" not in release_region
    assert "sd_mount_remount_after_usb" not in release_region
    assert "usb_msc_ownership_rearm" not in runtime


def test_manual_wake_resume_hook_orders_pending_rtc_before_clear_and_session():
    runtime = RUNTIME.read_text(encoding="utf-8")
    fn_at = runtime.index("static bool recorder_manual_wake_resume")
    rtc_at = runtime.index("recorder_flush_pending_rtc_after_mount", fn_at)
    clear_at = runtime.index("shutdown_armed_clear", rtc_at)
    start_at = runtime.index("recorder_start_session", clear_at)
    assert rtc_at < clear_at < start_at
