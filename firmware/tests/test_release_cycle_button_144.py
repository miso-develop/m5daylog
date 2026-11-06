"""R5-FU compiled production physical-hold detector; host-only, no GPIO proof."""

from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]
REC = ROOT / "firmware/components/recorder"


def test_continuous_gpio_hold_and_cutoff_native(tmp_path):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.fail("native C compiler required: GPIO detector fault matrix not run")
    out = tmp_path / "fu_button"
    build = subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-I", str(REC / "include"), str(REC / "release_cycle_button.c"),
         str(ROOT / "firmware/tests/native/release_cycle_button_harness.c"),
         "-o", str(out)], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(out)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr + run.stdout
    assert "FU_BUTTON_DETECTOR=PASS" in run.stdout


def test_exact_device_gpio_isr_and_status_guard():
    source = (REC / "release_cycle_button.c").read_text(encoding="utf-8")
    runtime = (ROOT / "firmware/main/task49_runtime.c").read_text(encoding="utf-8")
    fence = (REC / "release_cycle_fence.c").read_text(encoding="utf-8")
    assert "GPIO_INTR_POSEDGE" in source
    assert "gpio_isr_handler_add" in source
    assert "release_cycle_button_note_session_cutoff()" in runtime
    assert "tud_mounted()" in runtime
    assert runtime.index("release_cycle_button_poll(custody, &epoch)") < runtime.index("release_cycle_fence_stage_first_use()")
    assert runtime.index("usb_msc_ownership_complete_release_quiesce()") < runtime.index("release_cycle_fence_note_release_completed()")
    assert "FENCE_FU_READY_AFTER_RELEASE" in fence
    assert "FENCE_FU_STAGED" in fence
    assert "if (err == ESP_OK) atomic_store(&s_first_use_staged, true);" in fence


def test_production_esp_gpio_poll_repress_race(tmp_path):
    """Compile real ESP_PLATFORM poll with controlled ISR/GPIO/timer mocks.

    Unlike portable detector-only tests, this injects the GPIO ISR *inside*
    production release_cycle_button_poll's snapshot/level/clock window.
    """
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.fail("native C compiler required: production GPIO ISR race regression not run")
    inc = tmp_path / "include"
    (inc / "driver").mkdir(parents=True)
    (inc / "esp_err.h").write_text(
        "#pragma once\n"
        "typedef int esp_err_t;\n"
        "#define ESP_OK 0\n"
        "#define ESP_ERR_INVALID_STATE 3\n",
        encoding="utf-8",
    )
    (inc / "esp_attr.h").write_text(
        "#pragma once\n#define IRAM_ATTR\n", encoding="utf-8"
    )
    (inc / "esp_intr_alloc.h").write_text(
        "#pragma once\n#define ESP_INTR_FLAG_IRAM 1\n", encoding="utf-8"
    )
    (inc / "esp_timer.h").write_text(
        "#pragma once\n#include <stdint.h>\n"
        "int64_t esp_timer_get_time(void);\n", encoding="utf-8"
    )
    (inc / "driver/gpio.h").write_text(
        "#pragma once\n#include <stdint.h>\n#include \"esp_err.h\"\n"
        "typedef int gpio_num_t;\n"
        "typedef struct { uint64_t pin_bit_mask; int mode; int pull_up_en;"
        " int pull_down_en; int intr_type; } gpio_config_t;\n"
        "#define GPIO_MODE_INPUT 1\n"
        "#define GPIO_PULLUP_ENABLE 1\n"
        "#define GPIO_PULLDOWN_DISABLE 0\n"
        "#define GPIO_INTR_POSEDGE 1\n"
        "esp_err_t gpio_install_isr_service(int flags);\n"
        "esp_err_t gpio_config(const gpio_config_t *config);\n"
        "esp_err_t gpio_isr_handler_add(gpio_num_t pin, void (*cb)(void *), void *arg);\n"
        "esp_err_t gpio_isr_handler_remove(gpio_num_t pin);\n"
        "esp_err_t gpio_intr_enable(gpio_num_t pin);\n"
        "int gpio_get_level(gpio_num_t pin);\n",
        encoding="utf-8",
    )
    exe = tmp_path / "release_cycle_button_poll"
    build = subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-DESP_PLATFORM",
         "-I", str(inc), "-I", str(REC / "include"),
         str(REC / "release_cycle_button.c"),
         str(ROOT / "firmware/tests/native/release_cycle_button_poll_harness.c"),
         "-o", str(exe)],
        capture_output=True, text=True,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "FU_PRODUCTION_GPIO_POLL=PASS" in run.stdout


def test_first_use_stage_uses_double_gpio_witness_guard():
    runtime = (ROOT / "firmware/main/task49_runtime.c").read_text(encoding="utf-8")
    poll = runtime[runtime.index("static void recorder_first_use_monitor_poll("):
                   runtime.index("static void recorder_handle_usb_host_owned(")]
    assert poll.count("release_cycle_button_witness_still_valid(epoch)") == 2
    assert poll.index("release_cycle_button_witness_still_valid(epoch)") < poll.index(
        "release_cycle_fence_stage_first_use()"
    )
    assert poll.rindex("release_cycle_button_witness_still_valid(epoch)") > poll.index(
        "release_cycle_fence_stage_first_use()"
    )
    assert "release_cycle_fence_cancel_first_use_staging();" in poll
