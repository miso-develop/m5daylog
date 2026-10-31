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
