"""Task #44 scope-boundary tests: format, fail-loud, and out-of-scope guards.

Stdlib only. Ensures the #44 implementation keeps the fixed 16kHz/16bit/
mono contract, stays fail-loud (no silent recording state), writes only
`.part` files, and does NOT absorb Task #45/#46/#47 responsibilities
(rotation+finalize, power-loss recovery/quarantine, manifest/retention).
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
MAIN = REPO / "firmware/main/main.c"


def _sources():
    return {
        str(p.relative_to(REPO)): p.read_text(encoding="utf-8")
        for p in COMP.rglob("*")
        if p.is_file() and p.suffix in (".c", ".h")
    }


def test_format_contract_present():
    srcs = _sources()
    blob = "\n".join(srcs.values())
    assert "16000" in blob
    assert "I2S_DATA_BIT_WIDTH_16BIT" in blob
    assert "I2S_SLOT_MODE_MONO" in blob
    assert "RECORDER_SAMPLE_RATE_HZ" in blob


def test_part_suffix_discipline():
    srcs = _sources()
    blob = "\n".join(srcs.values())
    # Only `.wav.part` is accepted for capture output, never bare `.part`.
    assert ".wav.part" in blob
    assert '#define RECORDER_PART_SUFFIX ".wav.part"' in blob
    # #44 never finalizes to bare `.wav`: no rename/remove in the component.
    assert "rename(" not in blob
    assert "remove(" not in blob


def test_fail_loud_symbols():
    main = MAIN.read_text(encoding="utf-8")
    for marker in (
        "result: error",
        "mic init",
        "sd mount",
        "part open",
        "sd write",
        "buffer overflow",
        "dma overrun",
    ):
        assert marker in main, marker
    # Unset-pin guard: no silent recording when board pins are missing.
    capture = (COMP / "i2s_pdm_capture.c").read_text(encoding="utf-8")
    assert "pdm pins unset" in capture
    assert "ESP_ERR_INVALID_ARG" in capture


def test_no_scope_creep_into_later_tasks():
    srcs = _sources()
    blob = "\n".join(srcs.values()).lower()
    # Rotation / finalize (#45) policy keywords must not be implemented here.
    for keyword in ("quarantine", "manifest", "device.json", "acks/"):
        assert keyword not in blob, keyword
    # This component documents that mount/rotation/recovery are out of scope.
    readme = (COMP / "README.md").read_text(encoding="utf-8").lower()
    for marker in ("out of scope", "#45", "#46", "never deletes"):
        assert marker in readme, marker


def test_no_credentials_or_private_data_patterns():
    srcs = _sources()
    blob = "\n".join(srcs.values())
    for forbidden in ("BEGIN PRIVATE KEY", "ghp_", "AKIA", "password="):
        assert forbidden not in blob, forbidden
    main = MAIN.read_text(encoding="utf-8")
    assert "password" not in main.lower()


def test_board_pin_baseline():
    cfg = (COMP / "include/recorder_config.h").read_text(encoding="utf-8")
    for line in (
        "#define RECORDER_PDM_CLK_PIN 40",
        "#define RECORDER_PDM_DATA_PIN 41",
        "#define RECORDER_SD_CS_PIN 11",
        "#define RECORDER_SD_MOSI_PIN 12",
        "#define RECORDER_SD_CLK_PIN 14",
        "#define RECORDER_SD_MISO_PIN 39",
        '#define RECORDER_SD_MOUNT_POINT "/sdcard"',
    ):
        assert line in cfg, line


def test_sd_mount_creates_only_recording_dirs():
    src = (COMP / "sd_mount.c").read_text(encoding="utf-8")
    hdr = (COMP / "include/sd_mount.h").read_text(encoding="utf-8")
    for symbol in (
        "sd_mount_recordings",
        "sd_mount_is_mounted",
        "sd_mount_unmount",
    ):
        assert symbol in src or symbol in hdr, symbol
    # SPI bus pins and mount point come from the verified board baseline.
    for token in (
        "RECORDER_SD_CS_PIN",
        "RECORDER_SD_MOSI_PIN",
        "RECORDER_SD_CLK_PIN",
        "RECORDER_SD_MISO_PIN",
        "RECORDER_SD_MOUNT_POINT",
    ):
        assert token in src, token
    # Only live-recording directories are created; later-Task state is not.
    assert "RECORDER_RECORDINGS_DIR" in src
    assert "mkdir" in src
    assert "format_if_mount_failed" in src  # never format away audio
    for keyword in ("quarantine", "manifest", "device.json", "acks/"):
        assert keyword not in src.lower(), keyword
    # Mount teardown never deletes or renames audio.
    assert "rename(" not in src
    assert "remove(" not in src


def test_default_part_path_shape():
    main = MAIN.read_text(encoding="utf-8")
    assert ".wav.part" in main
    assert "/sdcard/M5DAYLOG/recordings/" in main


def test_producer_consumer_structure():
    main = MAIN.read_text(encoding="utf-8")
    # Two execution contexts: capture owns PDM/DMA, writer owns the sink.
    assert "recorder_capture_task" in main
    assert "recorder_writer_task" in main
    assert '"rec_capture"' in main and '"rec_writer"' in main
    # Bounded handoff: mutex-guarded pipeline plus event-group wakeups.
    assert "xSemaphoreCreateMutex" in main
    assert "xEventGroupSetBits" in main
    assert "xEventGroupWaitBits" in main
    # Slow SD writes release the pipeline lock so capture keeps filling
    # the other slot; both-full still drops loudly instead of overwriting.
    assert "keeps filling the other slot" in main
    assert "buffer_overflow" in main or "buffer overflow" in main


def test_writer_ready_handshake():
    main = MAIN.read_text(encoding="utf-8")
    # Capture never starts the mic until mount + dirs + sink are ready.
    assert "REC_BIT_WRITER_READY" in main
    assert "REC_BIT_STOP" in main
    assert "handshake" in main.lower()
    # STOP is sticky: after the READY/STOP wait the capture task must
    # re-check STOP before pdm_capture_init, so a late STOP never starts
    # the mic even when READY is also set.
    assert "(bits & REC_BIT_STOP) != 0" in main
    assert main.index("(bits & REC_BIT_STOP)") < main.index("pdm_capture_init")


def test_safe_stop_lifecycle():
    main = MAIN.read_text(encoding="utf-8")
    # One app-lifetime event group is the only cross-task channel: no
    # published task handle exists, so no stale-handle notify is possible.
    assert "xEventGroupCreate" in main
    assert "xEventGroupGetBits" in main
    assert "xTaskNotifyGive" not in main
    assert "s_writer_task" not in main
    assert "volatile bool s_stop" not in main


def test_dma_overrun_accounting_wired():
    main = MAIN.read_text(encoding="utf-8")
    # Proven loss arrives only from the driver overflow drain; stalls are
    # counted separately and never as drops.
    assert "pdm_capture_drain_overflow" in main
    assert "pdm_capture_stop_and_drain_final" in main
    assert "pcm_pipeline_note_driver_overflow" in main
    assert "pcm_pipeline_note_read_stall" in main
    assert "pdm_overflow_snapshot_t" in main
    assert "dma_overrun_events" in main
    assert "overrun: %" in main
    # Overflow is drained on EVERY read plus a quiescent teardown drain,
    # never gated on got > 0, so timeout/zero/STOP/fatal reads cannot lose
    # it. Teardown disables before the final drain (no drain-while-running
    # race).
    assert "Quiescent teardown" in main or "quiescent" in main.lower()
    assert "if (got > 0)" in main  # produce still gated, drain is not
    assert "Always preserve" in main
    # No inferred evidence: no single-bool overrun, no gap fabrication.
    assert "out_overrun" not in main
    assert "note_dma_gap" not in main


def test_quiescent_teardown_ordering():
    main = MAIN.read_text(encoding="utf-8")
    src = (COMP / "i2s_pdm_capture.c").read_text(encoding="utf-8")
    hdr = (COMP / "include/i2s_pdm_capture.h").read_text(encoding="utf-8")
    # New stop-and-final-drain API exists and is used before deinit.
    assert "pdm_capture_stop_and_drain_final" in hdr
    assert "pdm_capture_stop_and_drain_final" in src
    assert main.index("pdm_capture_stop_and_drain_final") < main.index(
        "pdm_capture_deinit"
    )
    # Implementation disables first, then drains: no callback can fire
    # after the final drain.
    fn = src[src.index("pdm_capture_stop_and_drain_final") :]
    fn = fn[: fn.index("\n}\n") + 3]
    assert "i2s_channel_disable" in fn
    assert "pdm_capture_drain_overflow" in fn
    assert fn.index("i2s_channel_disable") < fn.index(
        "pdm_capture_drain_overflow"
    )


def test_exact_stall_semantics():
    main = MAIN.read_text(encoding="utf-8")
    # Stall = timeout OR ESP_OK short read, only when the same read has no
    # driver overflow. Overflow and stall are never double-classified.
    assert "short_read" in main
    assert "ESP_ERR_TIMEOUT" in main
    assert "got < sizeof(s_dma_scratch)" in main
    assert "!snap_pending" in main
    assert main.count("!snap_pending") >= 2  # STOP path + normal path
    assert "note_read_stall" in main.lower() or "note_read_stall" in main


def test_driver_overflow_wiring():
    src = (COMP / "i2s_pdm_capture.c").read_text(encoding="utf-8")
    hdr = (COMP / "include/i2s_pdm_capture.h").read_text(encoding="utf-8")
    # ESP-IDF v5.5.5 three-argument registration shape.
    assert "i2s_channel_register_event_callback" in src
    assert "i2s_channel_register_event_callback(handle->rx_chan, &cbs, NULL)" in src
    assert "on_recv_q_ovf" in src
    assert "i2s_event_data_t" in src
    assert "portENTER_CRITICAL_ISR" in src
    assert "IRAM_ATTR" in src
    assert "esp_attr.h" in src
    assert "pdm_capture_drain_overflow" in src
    assert "pdm_overflow_snapshot_t" in hdr
    assert "stdint.h" in hdr
    # No single-bool overrun plumbing remains anywhere.
    assert "out_overrun" not in src and "out_overrun" not in hdr


def test_overflow_state_synchronized():
    src = (COMP / "i2s_pdm_capture.c").read_text(encoding="utf-8")
    # Armed flag shares the spinlock discipline: checked inside ISR
    # critical, reset/armed/disarmed inside task critical sections.
    assert "s_ovf_armed" in src
    assert "portENTER_CRITICAL_ISR" in src
    assert "portENTER_CRITICAL(&s_ovf_mux)" in src
    assert src.count("portENTER_CRITICAL") >= 4  # reset+arm+disarm+drain
    assert "Deterministic reset" in src or "deterministic" in src.lower()


def test_separate_driver_software_counters():
    main = MAIN.read_text(encoding="utf-8")
    hdr = (COMP / "include/pcm_pipeline.h").read_text(encoding="utf-8")
    for symbol in (
        "buffer_overflow",
        "buffer_drop_bytes",
        "buffer_drop_samples",
        "dma_overrun_events",
        "dma_drop_bytes",
        "dma_drop_samples",
    ):
        assert symbol in hdr, symbol
    # Diagnostics report both families separately while preserving
    # overflow and sd_err.
    assert "dma_drop:" in main
    assert "buf_drop:" in main
    assert "dma_bytes:" in main
    assert "buf_bytes:" in main
    assert "sd_err" in main


def test_capsule_right_slot():
    src = (COMP / "i2s_pdm_capture.c").read_text(encoding="utf-8")
    # M5Capsule mic is the RIGHT PDM slot, never the mono-default LEFT.
    assert "I2S_PDM_SLOT_RIGHT" in src
    assert "slot_mask" in src


def test_sd_flush_close_failure_accounting():
    main = MAIN.read_text(encoding="utf-8")
    # Write, flush, and close failures each reach the SD error counter.
    assert main.count("pcm_pipeline_note_sd_error") >= 3
    assert "part flush" in main
    assert "part close" in main


def test_main_component_declares_idf_dependencies():
    cmake = (REPO / "firmware/main/CMakeLists.txt").read_text(
        encoding="utf-8"
    )
    main = MAIN.read_text(encoding="utf-8")
    # Scaffold boot diagnostics use esp_flash_get_size(); that header and
    # symbol must keep working.
    assert '#include "esp_flash.h"' in main
    assert "esp_flash_get_size" in main
    # ...so the main component must depend on spi_flash (and recorder for
    # the capture path) through the supported component graph.
    assert "spi_flash" in cmake
    assert "recorder" in cmake
    # No raw include-path workarounds into IDF internals.
    assert "IDF_PATH" not in cmake
    assert "include_directories" not in cmake.lower()


def test_esp_idf_v55_api_shape():
    capture = (COMP / "i2s_pdm_capture.c").read_text(encoding="utf-8")
    capture_hdr = (COMP / "include/i2s_pdm_capture.h").read_text(
        encoding="utf-8"
    )
    # Public header uses bool: stdbool.h must be explicit, not transitive.
    assert "stdbool.h" in capture_hdr
    # ESP-IDF v5.5 PDM RX: single bundled config, two-argument init.
    assert "i2s_pdm_rx_config_t" in capture
    assert "I2S_PDM_RX_CLK_DEFAULT_CONFIG" in capture
    assert "I2S_PDM_RX_SLOT_DEFAULT_CONFIG" in capture
    assert (
        "i2s_channel_init_pdm_rx_mode(handle->rx_chan, &pdm_rx_cfg)"
        in capture
    )
    # GPIO struct is zeroed before use: no uninitialized fields.
    assert "memset(&gpio_cfg, 0, sizeof(gpio_cfg))" in capture
    # ESP-IDF v5.5 mount order: base_path, host, slot, mount, card.
    mount = (COMP / "sd_mount.c").read_text(encoding="utf-8")
    assert "esp_vfs_fat_mount_config_t" in mount
    assert "&slot_config, &mount_config, &s_card" in mount
    # Real v5.5.5 SDSPI types: declaration-initialized host, direct slot id,
    # sdmmc_card_t handle shared between mount and unmount.
    assert "sdmmc_host_t host = SDSPI_HOST_DEFAULT()" in mount
    assert "slot_config.host_id = host.slot" in mount
    assert "sdmmc_card_t" in mount
    blob = "\n".join(_sources().values())
    assert "sd_mmc_card_t" not in blob  # invalid type, never use
    assert "sdspi_host_t" not in blob  # nonexistent type, never use
