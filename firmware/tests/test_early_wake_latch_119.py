"""#119: compile the real recorder_power.c and exercise WAKE sampling across
an arbitrarily long NVS initialization delay. GPIO/ADC/sleep are fakes;
the tested WAKE/HOLD statements are the production source, not a copy.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
POWER = ROOT / "firmware/components/recorder/recorder_power.c"
RUNTIME = ROOT / "firmware/main/task49_runtime.c"
HEADERS = ROOT / "firmware/components/recorder/include"

STUBS = {
    "esp_err.h": r"""
#pragma once
#include <stddef.h>
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_INVALID_ARG -2
#define ESP_ERR_INVALID_STATE -3
#define ESP_ERR_INVALID_RESPONSE -4
""",
    "driver/gpio.h": r"""
#pragma once
#include <stdint.h>
#include "esp_err.h"
typedef int gpio_num_t;
typedef struct {
    uint64_t pin_bit_mask;
    int mode;
    int pull_up_en;
    int pull_down_en;
    int intr_type;
} gpio_config_t;
#define GPIO_MODE_OUTPUT 1
#define GPIO_MODE_INPUT 2
#define GPIO_PULLUP_DISABLE 0
#define GPIO_PULLUP_ENABLE 1
#define GPIO_PULLDOWN_DISABLE 0
#define GPIO_INTR_DISABLE 0
esp_err_t gpio_config(const gpio_config_t *);
esp_err_t gpio_set_level(gpio_num_t, unsigned);
esp_err_t gpio_hold_dis(gpio_num_t);
esp_err_t gpio_hold_en(gpio_num_t);
void gpio_deep_sleep_hold_dis(void);
void gpio_deep_sleep_hold_en(void);
int gpio_get_level(gpio_num_t);
""",
    "esp_adc/adc_oneshot.h": r"""
#pragma once
#include "esp_err.h"
typedef void *adc_oneshot_unit_handle_t;
typedef struct { int unit_id; int ulp_mode; } adc_oneshot_unit_init_cfg_t;
typedef struct { int atten; int bitwidth; } adc_oneshot_chan_cfg_t;
#define ADC_UNIT_1 1
#define ADC_CHANNEL_5 5
#define ADC_ATTEN_DB_12 12
#define ADC_BITWIDTH_DEFAULT 0
#define ADC_ULP_MODE_DISABLE 0
esp_err_t adc_oneshot_new_unit(const adc_oneshot_unit_init_cfg_t *, adc_oneshot_unit_handle_t *);
esp_err_t adc_oneshot_config_channel(adc_oneshot_unit_handle_t, int, const adc_oneshot_chan_cfg_t *);
esp_err_t adc_oneshot_read(adc_oneshot_unit_handle_t, int, int *);
esp_err_t adc_oneshot_del_unit(adc_oneshot_unit_handle_t);
""",
    "esp_adc/adc_cali.h": r"""
#pragma once
#include "esp_err.h"
typedef void *adc_cali_handle_t;
esp_err_t adc_cali_delete_scheme_curve_fitting(adc_cali_handle_t);
esp_err_t adc_cali_raw_to_voltage(adc_cali_handle_t, int, int *);
""",
    "esp_adc/adc_cali_scheme.h": r"""
#pragma once
#include "esp_adc/adc_cali.h"
typedef struct { int unit_id; int chan; int atten; int bitwidth; }
    adc_cali_curve_fitting_config_t;
esp_err_t adc_cali_create_scheme_curve_fitting(const adc_cali_curve_fitting_config_t *, adc_cali_handle_t *);
""",
    "esp_sleep.h": r"""
#pragma once
#include "esp_err.h"
#define ESP_SLEEP_WAKEUP_ALL 0
esp_err_t esp_sleep_disable_wakeup_source(int);
void esp_deep_sleep_start(void);
""",
    "recorder_config.h": r"""
#pragma once
#define RECORDER_POWER_HOLD_PIN 46
""",
}

HARNESS = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "recorder_power.h"
#include "driver/gpio.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_sleep.h"

static int wake_level = 1;
static int early_wake_config_fail = 0;
static int current_wake_config_fail = 0;
static int wake_sample_count = 0;
static int hold_high_count = 0;

esp_err_t gpio_config(const gpio_config_t *cfg) {
    if (cfg->pin_bit_mask == (1ULL << 42)) {
        if (early_wake_config_fail) {
            early_wake_config_fail = 0;
            return ESP_FAIL;
        }
        if (current_wake_config_fail) return ESP_FAIL;
    }
    return ESP_OK;
}
esp_err_t gpio_set_level(gpio_num_t gpio, unsigned high) {
    if (gpio == 46 && high == 1) hold_high_count++;
    return ESP_OK;
}
esp_err_t gpio_hold_dis(gpio_num_t gpio) { (void)gpio; return ESP_OK; }
esp_err_t gpio_hold_en(gpio_num_t gpio) { (void)gpio; return ESP_OK; }
void gpio_deep_sleep_hold_dis(void) {}
void gpio_deep_sleep_hold_en(void) {}
int gpio_get_level(gpio_num_t gpio) {
    assert(gpio == 42);
    wake_sample_count++;
    return wake_level;
}
esp_err_t adc_oneshot_new_unit(const adc_oneshot_unit_init_cfg_t *c, adc_oneshot_unit_handle_t *h) {
    (void)c; *h = (void *)1; return ESP_OK;
}
esp_err_t adc_oneshot_config_channel(adc_oneshot_unit_handle_t h, int c, const adc_oneshot_chan_cfg_t *cfg) {
    (void)h; (void)c; (void)cfg; return ESP_OK;
}
esp_err_t adc_oneshot_read(adc_oneshot_unit_handle_t h, int c, int *raw) {
    (void)h; (void)c; *raw = 1000; return ESP_OK;
}
esp_err_t adc_oneshot_del_unit(adc_oneshot_unit_handle_t h) { (void)h; return ESP_OK; }
esp_err_t adc_cali_create_scheme_curve_fitting(const adc_cali_curve_fitting_config_t *c, adc_cali_handle_t *h) {
    (void)c; *h = (void *)1; return ESP_OK;
}
esp_err_t adc_cali_delete_scheme_curve_fitting(adc_cali_handle_t h) { (void)h; return ESP_OK; }
esp_err_t adc_cali_raw_to_voltage(adc_cali_handle_t h, int raw, int *voltage) {
    (void)h; *voltage = raw; return ESP_OK;
}
esp_err_t esp_sleep_disable_wakeup_source(int source) { (void)source; return ESP_OK; }
void esp_deep_sleep_start(void) {}

int main(int argc, char **argv) {
    bool asserted = true;
    assert(argc == 2);
    if (strcmp(argv[1], "early-release") == 0) {
        wake_level = 0;
        assert(recorder_power_enable_hold() == ESP_OK);
        assert(hold_high_count > 0);
        // Simulate NVS taking longer than the operator's button hold.
        wake_level = 1;
        assert(recorder_power_manual_wake_asserted(&asserted) == ESP_OK);
        assert(asserted);
        assert(wake_sample_count >= 2);
    } else if (strcmp(argv[1], "never-pressed") == 0) {
        assert(recorder_power_enable_hold() == ESP_OK);
        assert(recorder_power_manual_wake_asserted(&asserted) == ESP_OK);
        assert(!asserted);
    } else if (strcmp(argv[1], "late-pressed") == 0) {
        assert(recorder_power_enable_hold() == ESP_OK);
        wake_level = 0;
        assert(recorder_power_manual_wake_asserted(&asserted) == ESP_OK);
        assert(asserted);
    } else if (strcmp(argv[1], "sticky-across-reinit") == 0) {
        wake_level = 0;
        assert(recorder_power_enable_hold() == ESP_OK);
        wake_level = 1;
        assert(recorder_power_init() == ESP_OK); // repeated HOLD must not erase initial sample
        assert(recorder_power_manual_wake_asserted(&asserted) == ESP_OK);
        assert(asserted);
    } else if (strcmp(argv[1], "early-config-error") == 0) {
        wake_level = 0;
        early_wake_config_fail = 1;
        assert(recorder_power_enable_hold() == ESP_OK);
        wake_level = 1;
        assert(recorder_power_manual_wake_asserted(&asserted) == ESP_OK);
        assert(!asserted); // a failed early GPIO config cannot assert WAKE
    } else if (strcmp(argv[1], "late-config-error") == 0) {
        assert(recorder_power_enable_hold() == ESP_OK);
        current_wake_config_fail = 1;
        assert(recorder_power_manual_wake_asserted(&asserted) != ESP_OK);
        assert(!asserted); // fail closed if the authoritative late GPIO read fails
    } else if (strcmp(argv[1], "null-output") == 0) {
        assert(recorder_power_enable_hold() == ESP_OK);
        assert(recorder_power_manual_wake_asserted(NULL) == ESP_ERR_INVALID_ARG);
    } else {
        return 2;
    }
    puts("WAKE_LATCH_CASE_PASS");
    return 0;
}
"""


def test_wake_latch_uses_real_power_source_with_mocked_gpio(tmp_path):
    compiler = shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        pytest.skip("host C compiler not installed")

    for relative, content in STUBS.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    harness = tmp_path / "wake_harness.c"
    binary = tmp_path / "wake_harness"
    harness.write_text(HARNESS, encoding="utf-8")
    subprocess.run(
        [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-DESP_PLATFORM", "-I", str(tmp_path), "-I", str(HEADERS),
         str(POWER), str(harness), "-o", str(binary)],
        check=True, capture_output=True, text=True, timeout=120
    )

    cases = (
        "early-release", "never-pressed", "late-pressed",
        "sticky-across-reinit", "early-config-error",
        "late-config-error", "null-output",
    )
    for case in cases:
        completed = subprocess.run(
            [str(binary), case], capture_output=True, text=True, timeout=15
        )
        assert completed.returncode == 0, (case, completed.stdout, completed.stderr)
        assert "WAKE_LATCH_CASE_PASS" in completed.stdout


def test_first_hold_precedes_nvs_boot_decision():
    runtime = RUNTIME.read_text(encoding="utf-8")
    app = runtime[runtime.index("void app_main(void) {"):]
    assert app.index("recorder_power_enable_hold()") < app.index("recorder_nvs_init()")
    assert app.index("recorder_nvs_init()") < app.index("recorder_power_manual_wake_asserted(")
    power = POWER.read_text(encoding="utf-8")
    hold = power[power.index("esp_err_t recorder_power_enable_hold(void) {"):]
    assert hold.index("s_boot_wake_pressed =") < hold.index("esp_err_t recorder_power_manual_wake_asserted(")
