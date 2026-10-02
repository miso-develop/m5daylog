#include "recorder_power.h"

#ifdef ESP_PLATFORM

#include <stdbool.h>
#include <string.h>

#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"

// M5Capsule / Capsule v1.1 board mapping used by M5Unified:
// GPIO6 -> ADC1 channel 5, VBAT divider ratio 2.0.
#define RECORDER_BATTERY_ADC_UNIT ADC_UNIT_1
#define RECORDER_BATTERY_ADC_CHANNEL ADC_CHANNEL_5
#define RECORDER_BATTERY_ADC_ATTEN ADC_ATTEN_DB_12
#define RECORDER_BATTERY_DIVIDER_NUM 2

static adc_oneshot_unit_handle_t s_adc_unit = NULL;
static adc_cali_handle_t s_adc_cali = NULL;
static bool s_power_ready = false;

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

    if (s_power_ready) {
        return ESP_OK;
    }
    recorder_power_cleanup();
    err = adc_oneshot_new_unit(&unit_cfg, &s_adc_unit);
    if (err != ESP_OK) {
        recorder_power_cleanup();
        return err;
    }
    err = adc_oneshot_config_channel(s_adc_unit,
                                     RECORDER_BATTERY_ADC_CHANNEL,
                                     &chan_cfg);
    if (err != ESP_OK) {
        recorder_power_cleanup();
        return err;
    }
    // ESP32-S3 supports curve-fitting calibration. Treat missing calibration
    // as a fail-loud monitor-init failure rather than applying an unverified
    // raw-count threshold to a safety stop.
    err = adc_cali_create_scheme_curve_fitting(&cali_cfg, &s_adc_cali);
    if (err != ESP_OK) {
        recorder_power_cleanup();
        return err;
    }
    s_power_ready = true;
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

#endif  // ESP_PLATFORM
