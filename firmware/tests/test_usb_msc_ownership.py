"""Task #49 host contract tests: USB MSC ownership handoff.

Stdlib-only source/contract tests. Physical enumeration, repeated cable cycling,
and filesystem integrity remain device Human-Gate evidence after the Device task
chain reaches #50; these tests lock the fail-closed software ordering.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
MAIN = REPO / "firmware/main/main.c"
USB_H = COMP / "include/usb_msc_ownership.h"
USB_C = COMP / "usb_msc_ownership.c"
SD_C = COMP / "sd_mount.c"
CMAKE = COMP / "CMakeLists.txt"
MANIFEST = REPO / "firmware/main/idf_component.yml"
SDKCONFIG = REPO / "firmware/sdkconfig.defaults"


def test_usb_ownership_module_is_built_and_tinyusb_is_pinned():
    cmake = CMAKE.read_text(encoding="utf-8")
    manifest = MANIFEST.read_text(encoding="utf-8")
    sdkconfig = SDKCONFIG.read_text(encoding="utf-8")

    assert '"usb_msc_ownership.c"' in cmake
    assert "esp_tinyusb" in manifest
    assert "==" in manifest  # deterministic managed-component revision
    assert "CONFIG_TINYUSB_MSC_ENABLED=y" in sdkconfig


def test_usb_publish_gate_requires_finalize_manifest_and_device_fs_release():
    hdr = USB_H.read_text(encoding="utf-8")
    src = USB_C.read_text(encoding="utf-8")

    # Ownership transfer must be explicit and fail closed. The host-visible
    # transition is not allowed from a plain cable-attach callback.
    for marker in (
        "usb_msc_ownership_init",
        "usb_msc_ownership_note_prepare_complete",
        "usb_msc_ownership_is_host_owned",
        "USB_MSC_EVENT_ATTACH",
        "USB_MSC_EVENT_DETACH",
    ):
        assert marker in hdr or marker in src, marker

    for gate in (
        "wav_finalized",
        "manifest_committed",
        "device_fs_released",
    ):
        assert gate in src, gate

    publish_at = src.index("usb_msc_publish")
    assert src.rfind("wav_finalized", 0, publish_at) >= 0
    assert src.rfind("manifest_committed", 0, publish_at) >= 0
    assert src.rfind("device_fs_released", 0, publish_at) >= 0


def test_usb_sync_never_keeps_device_filesystem_mounted():
    src = USB_C.read_text(encoding="utf-8")
    sd = SD_C.read_text(encoding="utf-8")

    assert "RECORDER_STATE_USB_SYNC" in src
    assert "sd_mount_is_mounted" in src
    assert "device_fs_released" in src
    # The SD layer exposes an ownership-aware release path rather than
    # silently deleting/renaming audio while switching owners.
    assert "sd_mount_release_for_usb" in sd
    assert "sd_mount_remount_after_usb" in sd


def test_disconnect_remounts_before_new_recording_session():
    main = MAIN.read_text(encoding="utf-8")

    remount_at = main.index("RECORDER_STATE_REMOUNT")
    mount_at = main.index("sd_mount_remount_after_usb", remount_at)
    recover_at = main.index("RECORDER_STATE_RECOVER", mount_at)
    restart_at = main.index("recorder_start_session", recover_at)

    assert remount_at < mount_at < recover_at < restart_at
    # New session generation means the writer creates a new recording UUID
    # instead of reopening the finalized pre-USB segment.
    assert "recorder_start_session" in main
    assert "recorder_generate_uuid" in main


def test_usb_prepare_uses_usb_specific_stop_and_waits_for_writer_commit():
    main = MAIN.read_text(encoding="utf-8")

    prepare_at = main.index("RECORDER_STATE_USB_PREPARE")
    stop_at = main.index("recorder_request_usb_stop", prepare_at)
    finalized_at = main.index("REC_BIT_WRITER_FINALIZED", stop_at)
    publish_at = main.index("usb_msc_ownership_note_prepare_complete", finalized_at)

    assert prepare_at < stop_at < finalized_at < publish_at


def test_usb_removal_during_sync_reaches_remount_not_terminal_stop():
    main = MAIN.read_text(encoding="utf-8")
    usb = USB_C.read_text(encoding="utf-8")

    assert "USB_MSC_EVENT_DETACH" in usb
    assert "RECORDER_STATE_USB_SYNC" in main
    assert "RECORDER_STATE_REMOUNT" in main
    assert "RECORDER_STATE_ERROR" in main
    # Detach is recoverable: errors in the remount path fail loud, but a
    # normal detach itself is not encoded as a terminal recorder error.
    assert "RECORDER_REASON_USB" in main
    assert "sd_mount_remount_after_usb" in main


def test_usb_ownership_logs_metadata_only():
    src = USB_C.read_text(encoding="utf-8").lower()
    # USB lifecycle evidence must not log protected recording contents.
    for forbidden in ("audio bytes", "transcript", "authorization", "password"):
        assert forbidden not in src
    for marker in ("attach", "detach", "owner", "mount"):
        assert marker in src, marker
