#include "recorder_power.h"

#ifdef ESP_PLATFORM

#include <stdbool.h>

#include "driver/gpio.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_sleep.h"
#include "recorder_config.h"

#define RECORDER_BATTERY_ADC_UNIT ADC_UNIT_1
#define RECORDER_BATTERY_ADC_CHANNEL ADC_CHANNEL_5
#define RECORDER_BATTERY_ADC_ATTEN ADC_ATTEN_DB_12
#define RECORDER_BATTERY_DIVIDER_NUM 2
#ifndef RECORDER_WAKE_BUTTON_PIN
#define RECORDER_WAKE_BUTTON_PIN 42
#endif

static adc_oneshot_unit_handle_t s_adc_unit = NULL;
static adc_cali_handle_t s_adc_cali = NULL;
static bool s_power_ready = false;

// Sample the physical WAKE button during the first HOLD assertion, before
// recorder_nvs_init() can delay the later shutdown-armed boot decision.
// Only a real active-low GPIO sample grants this latch; no USB or timer
// event may substitute for physical WAKE. A cold boot resets both statics.
static bool s_boot_wake_sampled = false;
static bool s_boot_wake_pressed = false;

static esp_err_t recorder_power_config_wake_input(void) {
    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << RECORDER_WAKE_BUTTON_PIN,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    return gpio_config(&cfg);
}

static void recorder_power_cleanup(void) {
    if (s_adc_cali != NULL) {
        (void)adc_cali_delete_scheme_curve_fitting(s_adc_cali);
        s_adc_cali = NULL;
    }
    if (s_adc_unit != NULL) {
        (void)adc_oneshot_del_unit(s_adc_unit);
        s_adc_unit = NULL;
    }
    s_power_ready = false;
}

static esp_err_t recorder_power_config_hold_output(void) {
    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << RECORDER_POWER_HOLD_PIN,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    return gpio_config(&cfg);
}

esp_err_t recorder_power_enable_hold(void) {
    esp_err_t err;

    // An armed USB-powered reset can restart while G46 is still retained low
    // from the previous deep-sleep entry. ESP-IDF requires the pad to be put
    // into a known output state before gpio_hold_dis(), otherwise releasing a
    // retained pad can briefly expose its reset/default level. Preload HIGH
    // while the latch is still effective, then release the hold and drive HIGH
    // again. On a cold battery WAKE this is simply an idempotent early assert.
    err = recorder_power_config_hold_output();
    if (err != ESP_OK) {
        return err;
    }
    err = gpio_set_level((gpio_num_t)RECORDER_POWER_HOLD_PIN, 1);
    if (err != ESP_OK) {
        return err;
    }
    gpio_deep_sleep_hold_dis();
    err = gpio_hold_dis((gpio_num_t)RECORDER_POWER_HOLD_PIN);
    if (err != ESP_OK) {
        return err;
    }
    err = gpio_set_level((gpio_num_t)RECORDER_POWER_HOLD_PIN, 1);
    if (err != ESP_OK) {
        return err;
    }
    // First power-on observation is sticky: a three-second manual press
    // must not be lost while NVS/RTC initialization is still running.
    // If early GPIO configuration fails, retain the original late read
    // path rather than inventing an asserted button.
    if (!s_boot_wake_sampled &&
        recorder_power_config_wake_input() == ESP_OK) {
        s_boot_wake_pressed =
            gpio_get_level((gpio_num_t)RECORDER_WAKE_BUTTON_PIN) == 0;
        s_boot_wake_sampled = true;
    }
    return ESP_OK;
}

esp_err_t recorder_power_release_hold(void) {
    esp_err_t err = recorder_power_config_hold_output();
    if (err != ESP_OK) {
        return err;
    }
    err = gpio_set_level((gpio_num_t)RECORDER_POWER_HOLD_PIN, 0);
    if (err != ESP_OK) {
        return err;
    }
    err = gpio_hold_en((gpio_num_t)RECORDER_POWER_HOLD_PIN);
    if (err != ESP_OK) {
        return err;
    }
    gpio_deep_sleep_hold_en();
    return ESP_OK;
}

esp_err_t recorder_power_manual_wake_asserted(bool *asserted) {
    esp_err_t err;
    bool pressed_now;

    if (asserted == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *asserted = false;
    err = recorder_power_config_wake_input();
    if (err != ESP_OK) {
        return err;
    }
    pressed_now = gpio_get_level((gpio_num_t)RECORDER_WAKE_BUTTON_PIN) == 0;
    *asserted = s_boot_wake_pressed || pressed_now;
    return ESP_OK;
}

esp_err_t recorder_power_init(void) {
    adc_oneshot_unit_init_cfg_t unit_cfg = {
        .unit_id = RECORDER_BATTERY_ADC_UNIT,
        .ulp_mode = ADC_ULP_MODE_DISABLE,
    };
    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = RECORDER_BATTERY_ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id = RECORDER_BATTERY_ADC_UNIT,
        .chan = RECORDER_BATTERY_ADC_CHANNEL,
        .atten = RECORDER_BATTERY_ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    esp_err_t err;

    err = recorder_power_enable_hold();
    if (err != ESP_OK) {
        return err;
    }
    if (s_power_ready) {
        return ESP_OK;
    }
    recorder_power_cleanup();
    err = adc_oneshot_new_unit(&unit_cfg, &s_adc_unit);
    if (err != ESP_OK) {
        recorder_power_cleanup();
        return err;
    }
    s_power_ready = true;
    err = adc_oneshot_config_channel(s_adc_unit,
                                     RECORDER_BATTERY_ADC_CHANNEL,
                                     &chan_cfg);
    if (err != ESP_OK) {
        recorder_power_cleanup();
        return err;
    }
    err = adc_cali_create_scheme_curve_fitting(&cali_cfg, &s_adc_cali);
    if (err != ESP_OK) {
        recorder_power_cleanup();
        return err;
    }
    return ESP_OK;
}

esp_err_t recorder_power_read_battery_mv(int *battery_mv) {
    int raw = 0;
    int pin_mv = 0;
    esp_err_t err;
    if (!s_power_ready || s_adc_unit == NULL || s_adc_cali == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (battery_mv == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *battery_mv = 0;
    err = adc_oneshot_read(s_adc_unit, RECORDER_BATTERY_ADC_CHANNEL, &raw);
    if (err != ESP_OK) {
        return err;
    }
    err = adc_cali_raw_to_voltage(s_adc_cali, raw, &pin_mv);
    if (err != ESP_OK) {
        return err;
    }
    if (pin_mv <= 0 || pin_mv > 2500) {
        return ESP_ERR_INVALID_RESPONSE;
    }
    *battery_mv = pin_mv * RECORDER_BATTERY_DIVIDER_NUM;
    return ESP_OK;
}

void recorder_power_deinit(void) {
    recorder_power_cleanup();
}

esp_err_t recorder_power_shutdown(void) {
    esp_err_t err;

    recorder_power_cleanup();

    // SHUTDOWN_ARMED must not become wakeable from stale ESP sleep sources.
    // Do this before dropping battery HOLD so a configuration failure leaves
    // the board powered and observable instead of entering an unrecoverable
    // no-authority state.
    err = esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);
    if (err != ESP_OK) {
        return err;
    }

    // The durable SHUTDOWN_ARMED marker and USB/storage quiescence are proven
    // by the caller before this point. If HOLD cannot be released, do not hide
    // the fault inside deep sleep: remaining awake and fail-closed is safer and
    // diagnosable, and still cannot remount or restart recording.
    err = recorder_power_release_hold();
    if (err != ESP_OK) {
        return err;
    }

    esp_deep_sleep_start();

    // Real ESP-IDF never returns from esp_deep_sleep_start(). Keeping a return
    // value makes the production sequencing executable in host behavioral tests.
    return ESP_OK;
}

#endif  // ESP_PLATFORM
