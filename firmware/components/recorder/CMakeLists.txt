# M5Daylog recorder component — Tasks #44 (IM-007) / #45 (IM-008) /
# #46 (IM-009) / #47 (IM-010) / #48 (IM-011).
#
# Portable sources (pcm_pipeline.c, wav_part.c, wav_rotation.c,
# wav_recovery.c, sha256.c, device_identity.c portable subset,
# device_manifest.c, recorder_state.c) deliberately have no ESP-IDF
# dependency so host contract tests can reason about them with stdlib-only
# tooling. ESP-IDF wrappers add PDM/SD plus Task #48 battery/RGB support.

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
    INCLUDE_DIRS
        "include"
    REQUIRES
        driver
        esp_adc
        esp_timer
        fatfs
        sdmmc
        nvs_flash
)
