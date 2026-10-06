"""Task #87 behavioral tests for Capsule HOLD/deep-sleep power sequencing.

The production recorder_power.c is compiled against deterministic host stubs.
These tests cover the hardware-critical order required by M5Capsule v1.1 and
ESP-IDF: preload HOLD high before releasing a retained low pad, and never enter
an unwakeable deep sleep when shutdown preparation fails.
"""

from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[2]
RECORDER = REPO / "firmware/components/recorder"
INCLUDE = RECORDER / "include"

STUB_HEADERS = {
    "esp_err.h": r"""
        #pragma once
        typedef int esp_err_t;
        #define ESP_OK 0
        #define ESP_FAIL 1
        #define ESP_ERR_INVALID_ARG 2
        #define ESP_ERR_INVALID_STATE 3
        #define ESP_ERR_INVALID_RESPONSE 4
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
        #define GPIO_MODE_INPUT 1
        #define GPIO_MODE_OUTPUT 2
        #define GPIO_PULLUP_DISABLE 0
        #define GPIO_PULLUP_ENABLE 1
        #define GPIO_PULLDOWN_DISABLE 0
        #define GPIO_INTR_DISABLE 0
        esp_err_t gpio_config(const gpio_config_t *cfg);
        esp_err_t gpio_set_level(gpio_num_t pin, uint32_t level);
        int gpio_get_level(gpio_num_t pin);
        esp_err_t gpio_hold_en(gpio_num_t pin);
        esp_err_t gpio_hold_dis(gpio_num_t pin);
        void gpio_deep_sleep_hold_en(void);
        void gpio_deep_sleep_hold_dis(void);
    """,
    "esp_adc/adc_oneshot.h": r"""
        #pragma once
        #include "esp_err.h"
        typedef void *adc_oneshot_unit_handle_t;
        typedef int adc_unit_t;
        typedef int adc_channel_t;
        typedef int adc_atten_t;
        typedef struct { int unit_id; int ulp_mode; } adc_oneshot_unit_init_cfg_t;
        typedef struct { int atten; int bitwidth; } adc_oneshot_chan_cfg_t;
        #define ADC_UNIT_1 1
        #define ADC_CHANNEL_5 5
        #define ADC_ATTEN_DB_12 12
        #define ADC_BITWIDTH_DEFAULT 0
        #define ADC_ULP_MODE_DISABLE 0
        esp_err_t adc_oneshot_new_unit(const adc_oneshot_unit_init_cfg_t *cfg,
                                       adc_oneshot_unit_handle_t *handle);
        esp_err_t adc_oneshot_del_unit(adc_oneshot_unit_handle_t handle);
        esp_err_t adc_oneshot_config_channel(adc_oneshot_unit_handle_t handle,
                                             adc_channel_t channel,
                                             const adc_oneshot_chan_cfg_t *cfg);
        esp_err_t adc_oneshot_read(adc_oneshot_unit_handle_t handle,
                                   adc_channel_t channel, int *raw);
    """,
    "esp_adc/adc_cali.h": r"""
        #pragma once
        #include "esp_err.h"
        typedef void *adc_cali_handle_t;
        esp_err_t adc_cali_raw_to_voltage(adc_cali_handle_t handle,
                                          int raw, int *mv);
    """,
    "esp_adc/adc_cali_scheme.h": r"""
        #pragma once
        #include "esp_adc/adc_oneshot.h"
        #include "esp_adc/adc_cali.h"
        typedef struct {
            int unit_id;
            int chan;
            int atten;
            int bitwidth;
        } adc_cali_curve_fitting_config_t;
        esp_err_t adc_cali_create_scheme_curve_fitting(
            const adc_cali_curve_fitting_config_t *cfg,
            adc_cali_handle_t *handle);
        esp_err_t adc_cali_delete_scheme_curve_fitting(adc_cali_handle_t handle);
    """,
    "esp_sleep.h": r"""
        #pragma once
        #include "esp_err.h"
        typedef int esp_sleep_source_t;
        #define ESP_SLEEP_WAKEUP_ALL 99
        esp_err_t esp_sleep_disable_wakeup_source(esp_sleep_source_t source);
        void esp_deep_sleep_start(void);
    """,
}

HARNESS = r"""
    #include <stdbool.h>
    #include <stdint.h>
    #include <stdio.h>
    #include <stdlib.h>
    #include <string.h>

    #include "esp_err.h"
    #include "recorder_power.h"

    #define CHECK(expr) do {         if (!(expr)) {             fprintf(stderr, "CHECK failed %s:%d: %s\n", __FILE__, __LINE__, #expr);             exit(2);         }     } while (0)

    static bool g_hold_output_configured;
    static bool g_high_preloaded;
    static int g_gpio_level = 1;
    static int g_sleep_calls;
    static int g_deep_hold_en_calls;
    static esp_err_t g_gpio_config_result = ESP_OK;
    static esp_err_t g_set_level_result = ESP_OK;
    static esp_err_t g_hold_en_result = ESP_OK;
    static esp_err_t g_hold_dis_result = ESP_OK;
    static esp_err_t g_disable_wakeup_result = ESP_OK;

    static void reset_stubs(void) {
        g_hold_output_configured = false;
        g_high_preloaded = false;
        g_gpio_level = 1;
        g_sleep_calls = 0;
        g_deep_hold_en_calls = 0;
        g_gpio_config_result = ESP_OK;
        g_set_level_result = ESP_OK;
        g_hold_en_result = ESP_OK;
        g_hold_dis_result = ESP_OK;
        g_disable_wakeup_result = ESP_OK;
    }

    esp_err_t gpio_config(const void *raw_cfg) {
        typedef struct {
            uint64_t pin_bit_mask;
            int mode;
            int pull_up_en;
            int pull_down_en;
            int intr_type;
        } cfg_t;
        const cfg_t *cfg = (const cfg_t *)raw_cfg;
        if (g_gpio_config_result != ESP_OK) return g_gpio_config_result;
        if (cfg->mode == 2 && (cfg->pin_bit_mask & (1ULL << 46)) != 0) {
            g_hold_output_configured = true;
        }
        return ESP_OK;
    }
    esp_err_t gpio_set_level(int pin, uint32_t level) {
        CHECK(pin == 46);
        if (g_set_level_result != ESP_OK) return g_set_level_result;
        g_gpio_level = (int)level;
        if (level == 1 && g_hold_output_configured) g_high_preloaded = true;
        return ESP_OK;
    }
    int gpio_get_level(int pin) {
        CHECK(pin == 42);
        return g_gpio_level;
    }
    esp_err_t gpio_hold_en(int pin) {
        CHECK(pin == 46);
        return g_hold_en_result;
    }
    esp_err_t gpio_hold_dis(int pin) {
        CHECK(pin == 46);
        /* ESP-IDF requires a known output state before releasing a retained pad. */
        CHECK(g_hold_output_configured);
        CHECK(g_high_preloaded);
        return g_hold_dis_result;
    }
    void gpio_deep_sleep_hold_en(void) { g_deep_hold_en_calls++; }
    void gpio_deep_sleep_hold_dis(void) {}

    esp_err_t esp_sleep_disable_wakeup_source(int source) {
        CHECK(source == 99);
        return g_disable_wakeup_result;
    }
    void esp_deep_sleep_start(void) { g_sleep_calls++; }

    esp_err_t adc_oneshot_new_unit(const void *cfg, void **handle) {
        (void)cfg; *handle = (void *)1; return ESP_OK;
    }
    esp_err_t adc_oneshot_del_unit(void *handle) { (void)handle; return ESP_OK; }
    esp_err_t adc_oneshot_config_channel(void *handle, int channel, const void *cfg) {
        (void)handle; (void)channel; (void)cfg; return ESP_OK;
    }
    esp_err_t adc_oneshot_read(void *handle, int channel, int *raw) {
        (void)handle; (void)channel; *raw = 1000; return ESP_OK;
    }
    esp_err_t adc_cali_create_scheme_curve_fitting(const void *cfg, void **handle) {
        (void)cfg; *handle = (void *)2; return ESP_OK;
    }
    esp_err_t adc_cali_delete_scheme_curve_fitting(void *handle) {
        (void)handle; return ESP_OK;
    }
    esp_err_t adc_cali_raw_to_voltage(void *handle, int raw, int *mv) {
        (void)handle; (void)raw; *mv = 2000; return ESP_OK;
    }

    static void run_enable_hold(void) {
        reset_stubs();
        CHECK(recorder_power_enable_hold() == ESP_OK);
        CHECK(g_hold_output_configured);
        CHECK(g_high_preloaded);
        CHECK(g_gpio_level == 1);
    }

    static void run_shutdown_success(void) {
        reset_stubs();
        CHECK(recorder_power_shutdown() == ESP_OK);
        CHECK(g_gpio_level == 0);
        CHECK(g_deep_hold_en_calls == 1);
        CHECK(g_sleep_calls == 1);
    }

    static void run_shutdown_wakeup_disable_failure(void) {
        reset_stubs();
        g_disable_wakeup_result = ESP_FAIL;
        CHECK(recorder_power_shutdown() == ESP_FAIL);
        CHECK(g_gpio_level == 1);
        CHECK(g_deep_hold_en_calls == 0);
        CHECK(g_sleep_calls == 0);
    }

    static void run_shutdown_hold_failure(void) {
        reset_stubs();
        g_hold_en_result = ESP_FAIL;
        CHECK(recorder_power_shutdown() == ESP_FAIL);
        CHECK(g_sleep_calls == 0);
        CHECK(g_deep_hold_en_calls == 0);
    }

    static void run_manual_wake(void) {
        bool asserted = false;
        reset_stubs();
        g_gpio_level = 0;
        CHECK(recorder_power_manual_wake_asserted(&asserted) == ESP_OK);
        CHECK(asserted);
        g_gpio_level = 1;
        CHECK(recorder_power_manual_wake_asserted(&asserted) == ESP_OK);
        CHECK(!asserted);
    }

    int main(int argc, char **argv) {
        CHECK(argc == 2);
        if (strcmp(argv[1], "enable-hold") == 0) run_enable_hold();
        else if (strcmp(argv[1], "shutdown-success") == 0) run_shutdown_success();
        else if (strcmp(argv[1], "shutdown-wakeup-disable-failure") == 0)
            run_shutdown_wakeup_disable_failure();
        else if (strcmp(argv[1], "shutdown-hold-failure") == 0)
            run_shutdown_hold_failure();
        else if (strcmp(argv[1], "manual-wake") == 0) run_manual_wake();
        else CHECK(false);
        return 0;
    }
"""


def _write_headers(root: Path) -> None:
    for rel, content in STUB_HEADERS.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")


def _build(tmp_path: Path) -> Path:
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("host C compiler is unavailable")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _write_headers(stubs)
    harness = tmp_path / "harness.c"
    harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
    binary = tmp_path / "recorder_power"
    subprocess.run(
        [
            cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-DESP_PLATFORM",
            "-I", str(stubs), "-I", str(INCLUDE),
            str(RECORDER / "recorder_power.c"), str(harness),
            "-o", str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


@pytest.mark.parametrize(
    "scenario",
    [
        "enable-hold",
        "shutdown-success",
        "shutdown-wakeup-disable-failure",
        "shutdown-hold-failure",
        "manual-wake",
    ],
)
def test_capsule_power_shutdown_behavior(tmp_path: Path, scenario: str) -> None:
    binary = _build(tmp_path)
    subprocess.run([str(binary), scenario], check=True, capture_output=True, text=True)
