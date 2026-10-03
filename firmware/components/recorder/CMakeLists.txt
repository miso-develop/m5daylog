# M5Daylog recorder component — Tasks #44 (IM-007) through #50 (IM-013).
# Portable recorder sources remain stdlib-testable; ESP-IDF wrappers add
# PDM/SD/power/RGB, exclusive USB MSC ownership, CDC JSON, and RTC correction.

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
        "usb_cdc_protocol.c"
        "rtc_correction.c"
    INCLUDE_DIRS
        "include"
    REQUIRES
        driver
        esp_adc
        esp_driver_i2c
        esp_driver_rmt
        esp_timer
        fatfs
        json
        sdmmc
        nvs_flash
        esp_tinyusb
)
