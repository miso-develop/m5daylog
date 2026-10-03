"""Task #87 software-only disconnect barrier source contracts.

These tests lock the proof obligation that host-side deferred MSC writes cannot
survive the barrier into Device filesystem ownership. Physical cable/suspend
behavior remains Human-Gate evidence.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
USB_C = REPO / "firmware/components/recorder/usb_msc_ownership.c"
SD_C = REPO / "firmware/components/recorder/sd_mount.c"


def test_barrier_teardown_precedes_storage_rebuild():
    usb = USB_C.read_text(encoding="utf-8")

    fn_at = usb.index("usb_msc_ownership_complete_disconnect_barrier")
    uninstall_at = usb.index("tinyusb_driver_uninstall", fn_at)
    proof_at = usb.index("host-io-quiesced", uninstall_at)
    transfer_at = usb.index("sd_mount_transfer_to_app", proof_at)
    assert uninstall_at < proof_at < transfer_at


def test_app_storage_rebuild_requires_zero_deferred_writes():
    sd = SD_C.read_text(encoding="utf-8")

    helper_at = sd.index("static esp_err_t sd_mount_create_storage")
    new_storage_at = sd.index("tinyusb_msc_new_storage_sdmmc", helper_at)
    assert new_storage_at > helper_at

    fn_at = sd.index("esp_err_t sd_mount_transfer_to_app")
    delete_at = sd.index("tinyusb_msc_delete_storage", fn_at)
    clear_at = sd.index("s_storage = NULL", delete_at)
    recreate_at = sd.index("sd_mount_create_storage", clear_at)
    assert delete_at < clear_at < recreate_at

    # esp_tinyusb refuses storage deletion while deferred writes remain. The
    # implementation must propagate that failure rather than remount anyway.
    delete_region = sd[fn_at:recreate_at]
    assert "if (err != ESP_OK)" in delete_region
    assert "return err" in delete_region
