"""#147 SDB-1.1 source-linked DEV isolation and ordering regression.

Native NVS faults are tested in test_dev_sdb11_first_boot_147.py; the real
GPIO detector is tested separately. These static checks are *not* proof of a
hardware-selected SD, 10s GPIO timing, ESP-IDF DEV-enabled build or release.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "components" / "recorder"
RUNTIME = ROOT / "main" / "task49_runtime.c"


def test_dev_sd_rebaseline_is_explicitly_guarded_and_isolated():
    source = (COMP / "sd_mount.c").read_text()
    api = (COMP / "include" / "sd_mount.h").read_text()
    runtime = RUNTIME.read_text()
    assert "sd_mount_dev_rebaseline_selected" in source
    assert "sd_mount_dev_rebaseline_selected" in api
    assert "sd_mount_dev_rebaseline_selected(" not in runtime
    assert "CONFIG_M5DAYLOG_SDB11_DEV_INSTALL" in source
    assert source.count("esp_vfs_fat_sdcard_format(") == 1
    assert "esp_vfs_fat_sdcard_format(" not in source[
        source.index("esp_err_t sd_mount_recordings(void) {"):
        source.index("// SDB-1.1 intentionally destructive installer primitive.")
    ]
    installer = source[
        source.index("static bool dev_sd_empty_root("):
        source.index("esp_err_t sd_mount_ensure_date_dir(")
    ]
    assert "owner_grant_consumed" in installer
    assert "expected_cid_serial" in installer
    assert "expected_sector_count" in installer
    assert "card->cid.serial == serial" in installer
    assert "(uint64_t)card->csd.capacity == sectors" in installer
    assert installer.count("esp_vfs_fat_sdspi_mount(") == 2
    assert "esp_vfs_fat_sdcard_format(" in installer
    assert installer.index("dev_sd_same_selected_card(",
                           installer.index("sd_mount_dev_rebaseline_selected(")) < installer.index(
                           "esp_vfs_fat_sdcard_format(")
    assert installer.index("esp_vfs_fat_sdcard_format(") < installer.index(
                           "esp_vfs_fat_sdcard_unmount(") < installer.rindex(
                           "esp_vfs_fat_sdspi_mount(")
    assert "tinyusb_msc_new_storage_sdmmc" not in installer
    assert ".format_if_mount_failed = false" in installer
    assert "ESP_ERR_NOT_SUPPORTED" in installer


def test_dev_gpio_gate_and_cdc_release_are_guarded():
    source = RUNTIME.read_text()
    kconfig = (COMP / "Kconfig").read_text()
    assert "default n" in kconfig
    assert 'CONFIG_M5DAYLOG_SDB11_EXPECTED_GENERATION' in source
    assert 'CONFIG_M5DAYLOG_SDB11_EXPECTED_DEVICE_ID' in source
    assert 'CONFIG_M5DAYLOG_SDB11_EXPECTED_BASELINE_SHA256' in source
    begin = source.index("void app_main(void) {")
    end = source.index("xTaskCreate(recorder_base_task,", begin)
    boot = source[begin:end]
    for marker in (
        "dev_sdb11_probe_boot(", "recorder_dev_wait_for_first_hold(",
        "dev_sdb11_consume_boot_intent(", "release_cycle_fence_begin_boot(",
        "release_cycle_fence_admit_dev_first_boot("
    ):
        assert marker in boot
    assert boot.index("dev_sdb11_probe_boot(") < boot.index(
        "recorder_dev_wait_for_first_hold(") < boot.index(
        "dev_sdb11_consume_boot_intent(") < boot.index(
        "release_cycle_fence_admit_dev_first_boot(")
    assert source.index("release_cycle_button_init()",  # physical ISR
                        source.index("recorder_dev_wait_for_first_hold(")) < begin
    assert "release_cycle_button_poll(true, &epoch)" in source
    assert "release_cycle_button_witness_still_valid(epoch)" in source
    assert "release_cycle_button_witness_still_valid(dev_hold_epoch)" in boot
    release = source[source.index("static usb_cdc_release_result_t recorder_cdc_release_accept("):
                     source.index("static bool recorder_cdc_release_response_complete(")]
    assert "recorder_cdc_status(&observed, NULL)" in release
    assert "observed.release_cycle_id == NULL" in release
    assert "observed.release_cycle_origin == NULL" in release
    assert release.index("recorder_cdc_status(&observed, NULL)") < release.index(
        "usb_msc_ownership_accept_release_storage(")


def test_sdb11_origin_is_from_consumed_boot_not_nvs_presence():
    fence = (COMP / "release_cycle_fence.c").read_text()
    assert "release_cycle_fence_admit_dev_first_boot(void)" in fence
    section = fence[fence.index("esp_err_t release_cycle_fence_admit_dev_first_boot(void)"):]
    section = section[:section.index("esp_err_t release_cycle_fence_note_recorder_ready(void)")]
    assert "dev_sdb11_boot_intent_consumed_this_boot()" in section
    assert 's_boot_origin = "SUPERVISED_FIRST_USE"' in section
    assert section.index("dev_sdb11_boot_intent_consumed_this_boot()") < section.index(
        's_boot_origin = "SUPERVISED_FIRST_USE"')
    assert "s_this_boot_qualified = true" in fence
    assert "FENCE_BOOT_ACTIVE" in fence
    assert "READY_AFTER_RELEASE" in fence
