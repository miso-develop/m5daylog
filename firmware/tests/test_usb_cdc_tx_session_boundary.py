"""Executable production-path regression for Task #50 finding REV-83-10."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
INCLUDE = COMP / "include"
GATE = COMP / "usb_cdc_session_gate.c"
TX = COMP / "usb_cdc_tx.c"
TX_HEADER = INCLUDE / "usb_cdc_tx.h"
HARNESS = REPO / "firmware/tests/native/usb_cdc_tx_harness.c"
TRANSPORT = COMP / "usb_cdc_protocol.c"


def test_response_tx_cannot_cross_physical_session_boundary(tmp_path: Path) -> None:
    """Execute the production TX routine through pre-write and in-flush reset races."""

    for required in (GATE, TX, TX_HEADER, HARNESS):
        assert required.exists(), f"missing production/native-test source: {required}"

    executable = tmp_path / "usb_cdc_tx_harness"
    compile_result = subprocess.run(
        [
            "cc",
            "-std=c11",
            "-D_POSIX_C_SOURCE=200809L",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pthread",
            "-I",
            str(INCLUDE),
            str(GATE),
            str(TX),
            str(HARNESS),
            "-o",
            str(executable),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert compile_result.returncode == 0, compile_result.stderr

    run_result = subprocess.run(
        [str(executable)],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert run_result.returncode == 0, run_result.stdout + run_result.stderr
    assert "production CDC TX session boundary: PASS" in run_result.stdout

    # The ESP transport must delegate to the same production routine exercised
    # above; this is linkage evidence, not the behavioral acceptance itself.
    src = TRANSPORT.read_text(encoding="utf-8")
    assert '#include "usb_cdc_tx.h"' in src
    assert "usb_cdc_tx_write_response" in src
