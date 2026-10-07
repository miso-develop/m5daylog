"""Task #50 durable RTC correction event recovery regressions."""

from pathlib import Path
import subprocess
import pytest

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
INCLUDE = COMP / "include"
STUB_INCLUDE = REPO / "firmware/tests/native/include"


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "clear-crash",
        "torn",
        "substring",
        "append-failure",
        "sync-failure",
        "close-failure",
    ],
)
def test_rtc_event_recovery_failure_and_idempotency_paths(
    tmp_path: Path, scenario: str
) -> None:
    exe = tmp_path / "rtc_event_recovery"
    result = subprocess.run(
        [
            "cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L",
            "-Wall", "-Wextra", "-Werror",
            "-I", str(STUB_INCLUDE), "-I", str(INCLUDE),
            str(COMP / "rtc_correction_event.c"),
            str(REPO / "firmware/tests/native/rtc_event_cjson_stub.c"),
            str(REPO / "firmware/tests/native/rtc_correction_event_harness.c"),
            "-Wl,--wrap=fwrite", "-Wl,--wrap=fsync", "-Wl,--wrap=fclose",
            "-o", str(exe),
        ],
        cwd=REPO, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    run = subprocess.run(
        [str(exe), scenario, str(tmp_path / f"{scenario}.jsonl")],
        cwd=REPO, text=True, capture_output=True, check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "rtc correction recovery production core: PASS" in run.stdout


def test_task87_failure_keeps_manual_wake_fail_closed() -> None:
    src = (REPO / "firmware/tests/test_task87_wake_recovery_behavior.py").read_text(
        encoding="utf-8"
    )
    assert '"rtc-failure"' in src
    assert "task87_wake_recovery_usb_rearm_allowed" in src
