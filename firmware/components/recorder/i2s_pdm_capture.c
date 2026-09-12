// ESP-IDF PDM RX capture — Task #44.
//
// Format: 16kHz / 16bit / mono PCM (recorder_config.h). Pins arrive from
// the caller; the Task #44 baseline defaults live in recorder_config.h
// (see header).

#include "i2s_pdm_capture.h"

#include <stdlib.h>
#include <string.h>

#include "recorder_config.h"

#ifdef ESP_PLATFORM

#include "driver/i2s_pdm.h"
#include "esp_check.h"
#include "esp_log.h"

static const char *TAG = "recorder_pdm";

struct pdm_capture_handle {
    i2s_chan_handle_t rx_chan;
};

esp_err_t pdm_capture_init(const pdm_capture_config_t *config,
                           pdm_capture_t *out_handle) {
    struct pdm_capture_handle *handle = NULL;
    i2s_chan_config_t chan_cfg;
    i2s_pdm_rx_clk_config_t clk_cfg;
    i2s_pdm_rx_slot_config_t slot_cfg;
    i2s_pdm_rx_gpio_config_t gpio_cfg;
    i2s_pdm_rx_config_t pdm_rx_cfg;
    esp_err_t err;

    if (config == NULL || out_handle == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (config->pdm_clk_pin < 0 || config->pdm_data_pin < 0) {
        ESP_LOGE(TAG,
                 "stage: record, result: error, reason: pdm pins unset "
                 "(board bring-up must provide verified clk/data pins)");
        return ESP_ERR_INVALID_ARG;
    }

    handle = (struct pdm_capture_handle *)calloc(1, sizeof(*handle));
    if (handle == NULL) {
        return ESP_ERR_NO_MEM;
    }

    chan_cfg = (i2s_chan_config_t)I2S_CHANNEL_DEFAULT_CONFIG(
        (i2s_port_t)config->i2s_port, I2S_ROLE_MASTER);
    // DMA staging: several short descriptors keep PDM overrun visible via
    // read timeouts instead of coalescing long gaps.
    chan_cfg.dma_frame_num = 240;
    chan_cfg.dma_desc_num = 8;

    err = i2s_new_channel(&chan_cfg, NULL, &handle->rx_chan);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: i2s new chan");
        free(handle);
        return err;
    }

    clk_cfg = (i2s_pdm_rx_clk_config_t)I2S_PDM_RX_CLK_DEFAULT_CONFIG(
        RECORDER_SAMPLE_RATE_HZ);
    // PCM-output slot config per the ESP-IDF v5.5 PDM RX example: 16bit
    // samples, mono slot.
    slot_cfg = (i2s_pdm_rx_slot_config_t)I2S_PDM_RX_SLOT_DEFAULT_CONFIG(
        I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO);
    // Zero the GPIO struct first so every field (including invert flags) is
    // deterministic; PDM RX uses clk + one data line.
    memset(&gpio_cfg, 0, sizeof(gpio_cfg));
    gpio_cfg.clk = (gpio_num_t)config->pdm_clk_pin;
    gpio_cfg.din = (gpio_num_t)config->pdm_data_pin;

    // ESP-IDF v5.5 takes a single PDM RX config bundling clk/slot/gpio.
    memset(&pdm_rx_cfg, 0, sizeof(pdm_rx_cfg));
    pdm_rx_cfg.clk_cfg = clk_cfg;
    pdm_rx_cfg.slot_cfg = slot_cfg;
    pdm_rx_cfg.gpio_cfg = gpio_cfg;

    err = i2s_channel_init_pdm_rx_mode(handle->rx_chan, &pdm_rx_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: pdm rx init");
        i2s_del_channel(handle->rx_chan);
        free(handle);
        return err;
    }

    err = i2s_channel_enable(handle->rx_chan);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "stage: record, result: error, reason: pdm enable");
        i2s_del_channel(handle->rx_chan);
        free(handle);
        return err;
    }

    *out_handle = handle;
    ESP_LOGI(TAG, "stage: record, result: pdm ready, rate: %u, bits: %u",
             (unsigned)RECORDER_SAMPLE_RATE_HZ,
             (unsigned)RECORDER_BITS_PER_SAMPLE);
    return ESP_OK;
}

esp_err_t pdm_capture_read(pdm_capture_t handle, void *dst, size_t len,
                           size_t *out_read, bool *out_overrun) {
    size_t bytes_read = 0;
    esp_err_t err;

    if (handle == NULL || dst == NULL || out_read == NULL ||
        out_overrun == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (len == 0 || (len % RECORDER_BYTES_PER_SAMPLE) != 0) {
        return ESP_ERR_INVALID_ARG;
    }

    err = i2s_channel_read(handle->rx_chan, dst, len, &bytes_read, 1000);
    *out_read = bytes_read;
    // Timeout with short/zero read while recording = DMA could not keep up.
    *out_overrun =
        (err == ESP_ERR_TIMEOUT) || (bytes_read != len);
    if (err == ESP_ERR_TIMEOUT && bytes_read > 0) {
        // Partial data is still usable; surface the timeout as overrun.
        return ESP_OK;
    }
    return err;
}

void pdm_capture_deinit(pdm_capture_t handle) {
    if (handle == NULL) {
        return;
    }
    i2s_channel_disable(handle->rx_chan);
    i2s_del_channel(handle->rx_chan);
    free(handle);
}

#else  // !ESP_PLATFORM — host/test build: linkable stubs, never recording.

esp_err_t pdm_capture_init(const pdm_capture_config_t *config,
                           pdm_capture_t *out_handle) {
    (void)config;
    (void)out_handle;
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t pdm_capture_read(pdm_capture_t handle, void *dst, size_t len,
                           size_t *out_read, bool *out_overrun) {
    (void)handle;
    (void)dst;
    (void)len;
    (void)out_read;
    (void)out_overrun;
    return ESP_ERR_NOT_SUPPORTED;
}

void pdm_capture_deinit(pdm_capture_t handle) {
    (void)handle;
}

#endif  // ESP_PLATFORM
