# M5Daylog recorder component — Tasks #44 (IM-007) / #45 (IM-008) /
# #46 (IM-009) / #47 (IM-010).
#
# Portable sources (pcm_pipeline.c, wav_part.c, wav_rotation.c,
# wav_recovery.c, sha256.c, device_identity.c (portable subset),
# device_manifest.c) deliberately have no ESP-IDF dependency so host
# contract tests can reason about them with a plain stdlib toolchain.
# ESP-IDF wrappers (i2s_pdm_capture.c, sd_pcm_sink.c, sd_mount.c,
# device_identity.c NVS ensure) use IDF drivers and guard IDF-only
# includes with ESP_PLATFORM.

idf_component_register(
    SRCS
        "pcm_pipeline.c"
        "wav_part.c"
        "wav_rotation.c"
        "wav_recovery.c"
        "sha256.c"
        "device_identity.c"
        "device_manifest.c"
        "i2s_pdm_capture.c"
        "sd_pcm_sink.c"
        "sd_mount.c"
    INCLUDE_DIRS
        "include"
    REQUIRES
        driver
        esp_timer
        fatfs
        sdmmc
        nvs_flash
)
