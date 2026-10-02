#include "recorder_status_led.h"

#ifdef ESP_PLATFORM

#include <stdbool.h>
#include <stdint.h>

#include "driver/gpio.h"
#include "driver/rmt_encoder.h"
#include "driver/rmt_tx.h"
#include "esp_rom_sys.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "recorder_config.h"

#define RECORDER_LED_RMT_RESOLUTION_HZ 10000000u
#define RECORDER_LED_RESET_US 80u

static rmt_channel_handle_t s_led_channel = NULL;
static rmt_encoder_handle_t s_led_encoder = NULL;
static SemaphoreHandle_t s_led_lock = NULL;
static bool s_led_ready = false;
static recorder_state_t s_led_state = RECORDER_STATE_BOOT;

// Call only while s_led_lock is held after the mutex has been created.
static void recorder_status_led_cleanup(void) {
    if (s_led_channel != NULL) {
        (void)rmt_disable(s_led_channel);
    }
    if (s_led_encoder != NULL) {
        (void)rmt_del_encoder(s_led_encoder);
        s_led_encoder = NULL;
    }
    if (s_led_channel != NULL) {
        (void)rmt_del_channel(s_led_channel);
        s_led_channel = NULL;
    }
    (void)gpio_set_level((gpio_num_t)RECORDER_STATUS_LED_POWER_PIN, 0);
    s_led_ready = false;
    s_led_state = RECORDER_STATE_BOOT;
}

// Call only while s_led_lock is held. The RMT channel handle APIs used here
// are not task-safe, so the component mutex owns the complete transaction.
static esp_err_t recorder_status_led_write(uint8_t red, uint8_t green,
                                           uint8_t blue,
                                           bool keep_powered) {
    uint8_t grb[3] = { green, red, blue };
    rmt_transmit_config_t tx_cfg = {
        .loop_count = 0,
    };
    esp_err_t err;
    if (!s_led_ready || s_led_channel == NULL || s_led_encoder == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    err = gpio_set_level((gpio_num_t)RECORDER_STATUS_LED_POWER_PIN, 1);
    if (err != ESP_OK) {
        return err;
    }
    esp_rom_delay_us(1000);
    err = rmt_transmit(s_led_channel, s_led_encoder, grb, sizeof(grb),
                       &tx_cfg);
    if (err != ESP_OK) {
        return err;
    }
    // IDF rmt_tx_wait_all_done() takes milliseconds, not RTOS ticks.
    err = rmt_tx_wait_all_done(s_led_channel, 20);
    if (err != ESP_OK) {
        return err;
    }
    esp_rom_delay_us(RECORDER_LED_RESET_US);
    if (!keep_powered) {
        err = gpio_set_level((gpio_num_t)RECORDER_STATUS_LED_POWER_PIN, 0);
    }
    return err;
}

esp_err_t recorder_status_led_init(void) {
    gpio_config_t power_cfg = {
        .pin_bit_mask = 1ULL << RECORDER_STATUS_LED_POWER_PIN,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    rmt_tx_channel_config_t tx_cfg = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .gpio_num = RECORDER_STATUS_LED_DATA_PIN,
        .mem_block_symbols = 64,
        .resolution_hz = RECORDER_LED_RMT_RESOLUTION_HZ,
        .trans_queue_depth = 4,
    };
    rmt_bytes_encoder_config_t encoder_cfg = {
        .bit0 = {
            .level0 = 1,
            .duration0 = 3,
            .level1 = 0,
            .duration1 = 9,
        },
        .bit1 = {
            .level0 = 1,
            .duration0 = 9,
            .level1 = 0,
            .duration1 = 3,
        },
        .flags.msb_first = 1,
    };
    esp_err_t err = ESP_OK;

    if (s_led_lock == NULL) {
        s_led_lock = xSemaphoreCreateMutex();
        if (s_led_lock == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    if (xSemaphoreTake(s_led_lock, portMAX_DELAY) != pdTRUE) {
        return ESP_FAIL;
    }
    if (s_led_ready) {
        goto out;
    }

    recorder_status_led_cleanup();
    err = gpio_config(&power_cfg);
    if (err != ESP_OK) {
        goto out;
    }
    err = gpio_set_level((gpio_num_t)RECORDER_STATUS_LED_POWER_PIN, 0);
    if (err != ESP_OK) {
        goto out;
    }
    err = rmt_new_tx_channel(&tx_cfg, &s_led_channel);
    if (err != ESP_OK) {
        recorder_status_led_cleanup();
        goto out;
    }
    err = rmt_new_bytes_encoder(&encoder_cfg, &s_led_encoder);
    if (err != ESP_OK) {
        recorder_status_led_cleanup();
        goto out;
    }
    err = rmt_enable(s_led_channel);
    if (err != ESP_OK) {
        recorder_status_led_cleanup();
        goto out;
    }
    s_led_ready = true;
    s_led_state = RECORDER_STATE_BOOT;
    err = recorder_status_led_write(0, 0, 0, false);
    if (err != ESP_OK) {
        recorder_status_led_cleanup();
    }

out:
    xSemaphoreGive(s_led_lock);
    return err;
}

esp_err_t recorder_status_led_set_state(recorder_state_t state) {
    esp_err_t err = ESP_OK;
    if (s_led_lock == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (xSemaphoreTake(s_led_lock, portMAX_DELAY) != pdTRUE) {
        return ESP_FAIL;
    }

    // Terminal indications are monotonic: ERROR supersedes low-battery,
    // and a delayed lower-priority task can never overwrite red with amber
    // or switch a terminal indication back off. The guard and write share
    // one lock so physical indication ordering follows this precedence.
    if (s_led_state == RECORDER_STATE_ERROR &&
        state != RECORDER_STATE_ERROR) {
        goto out;
    }
    if (s_led_state == RECORDER_STATE_LOW_BATTERY_STOP &&
        state != RECORDER_STATE_ERROR &&
        state != RECORDER_STATE_LOW_BATTERY_STOP) {
        goto out;
    }
    switch (state) {
        case RECORDER_STATE_ERROR:
            err = recorder_status_led_write(32, 0, 0, true);
            break;
        case RECORDER_STATE_LOW_BATTERY_STOP:
            err = recorder_status_led_write(24, 8, 0, true);
            break;
        default:
            err = recorder_status_led_write(0, 0, 0, false);
            break;
    }
    if (err == ESP_OK) {
        s_led_state = state;
    }

out:
    xSemaphoreGive(s_led_lock);
    return err;
}

void recorder_status_led_deinit(void) {
    if (s_led_lock == NULL ||
        xSemaphoreTake(s_led_lock, portMAX_DELAY) != pdTRUE) {
        return;
    }
    if (s_led_ready) {
        (void)recorder_status_led_write(0, 0, 0, false);
    }
    recorder_status_led_cleanup();
    xSemaphoreGive(s_led_lock);
}

#endif  // ESP_PLATFORM
