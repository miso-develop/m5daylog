# M5Daylog recorder component — Task #44 (IM-007).
#
# Portable sources (pcm_pipeline.c, wav_part.c) deliberately have no
# ESP-IDF dependency so host contract tests can compile them with a plain
# C compiler. ESP-IDF wrappers (i2s_pdm_capture.c, sd_pcm_sink.c) use IDF
# drivers and guard IDF-only includes with ESP_PLATFORM.

idf_component_register(
    SRCS
        "pcm_pipeline.c"
        "wav_part.c"
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
)
