# M5Daylog recorder component — Tasks #44 (IM-007) through #49/#87.

idf_component_register(
    SRCS
        "pcm_pipeline.c"
        "wav_part.c"
        "wav_rotation.c"
        "wav_recovery.c"
        "sha256.c"
        "device_identity.c"
        "device_manifest.c"
        "recorder_state.c"
        "recorder_power.c"
        "recorder_status_led.c"
        "i2s_pdm_capture.c"
        "sd_pcm_sink.c"
        "sd_mount.c"
        "usb_msc_ownership.c"
        "usb_cdc_protocol_core.c"
        "usb_cdc_session_gate.c"
        "usb_cdc_tx.c"
        "usb_cdc_protocol.c"
        "shutdown_armed.c"
    INCLUDE_DIRS
        "include"
    REQUIRES
        driver
        esp_adc
        esp_driver_rmt
        esp_timer
        fatfs
        json
        sdmmc
        nvs_flash
        esp_tinyusb
)

# Task #87 / D-031: RELEASE_STORAGE acceptance sets the shared release gate
# before its CDC success response, so no later MSC command can reach storage
# while the response/teardown boundary is pending. Qualified post-status SCSI
# eject may set the same gate as an optional compatibility path. The START STOP
# wrapper remains observation-only diagnostic instrumentation.
target_link_libraries(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=tud_msc_start_stop_cb"
    "-Wl,--wrap=tud_msc_test_unit_ready_cb"
    "-Wl,--wrap=tud_msc_capacity_cb"
    "-Wl,--wrap=tud_msc_read10_cb"
    "-Wl,--wrap=tud_msc_write10_cb"
)
