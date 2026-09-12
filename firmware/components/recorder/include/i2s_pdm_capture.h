#pragma once

// ESP-IDF PDM microphone capture wrapper — Task #44.
//
// Fixed contract: PDM RX configured for 16kHz / 16bit / mono PCM. Board
// PDM pins (clock/data) arrive caller-provided in `pdm_capture_config_t`;
// the Task #44 baseline defaults (M5Capsule v1.1 CLK 40 / DAT 41) live in
// `recorder_config.h` and are passed in by `main.c` — build flags may
// override them. Unset pins (`< 0`) fail init fail-loud so the firmware
// can enter ERROR instead of a silent "recording" state (Spec #36).

#include <stdbool.h>
#include <stddef.h>

#ifdef ESP_PLATFORM
#include "esp_err.h"
#else
// Host/test builds: minimal esp_err_t stand-in so headers stay includable.
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL 1
#define ESP_ERR_INVALID_ARG 2
#define ESP_ERR_INVALID_STATE 3
#define ESP_ERR_NO_MEM 4
#define ESP_ERR_NOT_SUPPORTED 5
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int i2s_port;      // I2S peripheral index (normally 0 on ESP32-S3)
    int pdm_clk_pin;   // board-verified PDM clock GPIO, or -1 if unset
    int pdm_data_pin;  // board-verified PDM data GPIO, or -1 if unset
} pdm_capture_config_t;

typedef struct pdm_capture_handle *pdm_capture_t;

// Configure + enable the PDM RX channel per `recorder_config.h` format.
// Fail-loud: ESP_ERR_INVALID_ARG for unset pins, ESP_ERR_NO_MEM on
// allocation failure. ESP_ERR_NOT_SUPPORTED on non-ESP host builds.
esp_err_t pdm_capture_init(const pdm_capture_config_t *config,
                           pdm_capture_t *out_handle);

// Blocking read of up to `len` PCM bytes (len must be sample-aligned).
// Returns ESP_OK with `*out_read` set; `*out_overrun` is set true when the
// driver reports data loss / timeout so the caller can bump DMA counters.
// Any transport error is fail-loud (non-ESP_OK) — never silent.
esp_err_t pdm_capture_read(pdm_capture_t handle, void *dst, size_t len,
                           size_t *out_read, bool *out_overrun);

// Disable + release the channel. Safe with NULL (no-op).
void pdm_capture_deinit(pdm_capture_t handle);

#ifdef __cplusplus
}
#endif
