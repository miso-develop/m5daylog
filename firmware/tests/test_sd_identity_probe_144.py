"""Source-linked isolation checks for one-shot DEV SD identity probe.

Actual ESP-IDF production compile is covered by the existing firmware CI job.
These tests are not a real SD identity witness or physical flash permit.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMP = ROOT / "firmware/components/recorder"


def test_probe_runs_before_nvs_or_recorder_and_cannot_share_installer_mode():
    kconfig = (COMP / "Kconfig").read_text(encoding="utf-8")
    runtime = (ROOT / "firmware/main/task49_runtime.c").read_text(encoding="utf-8")
    assert "config M5DAYLOG_SDB11_SD_ID_PROBE_MODE" in kconfig
    assert "depends on !M5DAYLOG_SDB11_DEV_INSTALL" in kconfig
    start = runtime.index("const esp_err_t probe = sd_mount_dev_observe_identity(")
    end = runtime.index("recorder_power_shutdown();", start)
    nvs = runtime.index("recorder_nvs_init()")
    assert start < end < nvs
    assert "return;" in runtime[end:nvs]
    assert "recorder_base_app_main();" not in runtime[start:nvs]


def test_probe_uses_only_sd_spi_identity_and_fails_closed_on_cleanup():
    source = (COMP / "sd_mount.c").read_text(encoding="utf-8")
    start = source.index("static bool s_dev_sd_probe_attempted;")
    end = source.index("#else\nesp_err_t sd_mount_dev_observe_identity(", start)
    probe = source[start:end]
    assert "s_dev_sd_probe_attempted = true;" in probe
    assert "sdmmc_card_init(&host, &card)" in probe
    assert "card.cid.serial" in probe
    assert "card.csd.capacity" in probe
    assert "sdmmc_card_deinit(&card)" in probe
    assert "sdspi_host_remove_device(handle)" in probe
    assert "sdspi_host_deinit()" in probe
    assert "spi_bus_free(spi_host)" in probe
    assert "if (err == ESP_OK)" in probe
    assert "return err;" in probe
    for forbidden in (
        "esp_vfs_", "sdmmc_write_sectors", "sdmmc_erase_sectors",
        "tinyusb_msc_", "nvs_", "release_storage", "mkdir(",
    ):
        assert forbidden not in probe
