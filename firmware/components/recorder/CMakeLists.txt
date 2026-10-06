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
        "shutdown_armed.c"
    INCLUDE_DIRS
        "include"
    REQUIRES
        driver
        esp_adc
        esp_driver_rmt
        esp_timer
        fatfs
        sdmmc
        nvs_flash
        esp_tinyusb
)

# Task #87: once the post-status explicit-eject completion sets
# s_release_pending, no later MSC command may reach the storage backend while
# the recorder coordinator is still scheduling TinyUSB teardown. The
# START STOP UNIT wrapper below is observation-only Human-Gate instrumentation:
# it always delegates to esp_tinyusb and never grants release. Authority remains
# exclusively in the production post-status completion callback.
target_link_libraries(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=tud_msc_start_stop_cb"
    "-Wl,--wrap=tud_msc_test_unit_ready_cb"
    "-Wl,--wrap=tud_msc_capacity_cb"
    "-Wl,--wrap=tud_msc_read10_cb"
    "-Wl,--wrap=tud_msc_write10_cb"
)
