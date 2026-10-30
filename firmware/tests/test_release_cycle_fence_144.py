"""#144: compile the actual R3 NVS fence C source with deterministic NVS fakes.

These are host-only crash-boundary simulations, not Device/Windows/Human Gate proof.
"""
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMP = ROOT / "firmware/components/recorder"


@pytest.fixture(scope="module")
def fence_binary(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("no native C compiler")
    tmp = tmp_path_factory.mktemp("cycle_fence")
    inc = tmp / "include"
    inc.mkdir()
    (inc / "esp_err.h").write_text(textwrap.dedent("""\
        #pragma once
        typedef int esp_err_t;
        #define ESP_OK 0
        #define ESP_FAIL 1
        #define ESP_ERR_INVALID_ARG 2
        #define ESP_ERR_INVALID_STATE 3
        #define ESP_ERR_NOT_SUPPORTED 5
        #define ESP_ERR_NVS_NOT_FOUND 10
    """))
    (inc / "nvs.h").write_text(textwrap.dedent("""\
        #pragma once
        #include <stdint.h>
        #include "esp_err.h"
        typedef int nvs_handle_t;
        #define NVS_READONLY 0
        #define NVS_READWRITE 1
        esp_err_t nvs_open(const char *, int, nvs_handle_t *);
        void nvs_close(nvs_handle_t);
        esp_err_t nvs_get_u8(nvs_handle_t, const char *, uint8_t *);
        esp_err_t nvs_set_u8(nvs_handle_t, const char *, uint8_t);
        esp_err_t nvs_commit(nvs_handle_t);
    """))
    exe = tmp / "release_cycle_fence"
    build = subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-DESP_PLATFORM",
         "-I", str(inc), "-I", str(COMP / "include"),
         str(COMP / "release_cycle_fence.c"),
         str(ROOT / "firmware/tests/native/release_cycle_fence_harness.c"),
         "-o", str(exe)],
        capture_output=True, text=True,
    )
    assert build.returncode == 0, build.stderr
    return exe


def observation(exe, mode, lifecycle, fence, failure="none"):
    r = subprocess.run(
        [str(exe), mode, str(lifecycle), str(fence), failure],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    return dict(part.split("=", 1) for part in r.stdout.strip().split())


@pytest.mark.parametrize("lifecycle,fence,manual", [
    (0, 83, "claim"), (2, 83, "claim"), (3, 83, "claim"),
    (1, 82, "claim"), (1, 81, "claim"), (1, "missing", "claim"),
    (1, 99, "claim"), (1, 83, "normal"),
])
def test_no_stale_or_unauthorized_boot_witness(fence_binary, lifecycle, fence, manual):
    result = observation(fence_binary, manual, lifecycle, fence)
    assert result["qualified"] == "0"
    assert result["fence"] == ("-1" if fence == "missing" else str(fence))


def test_only_fresh_armed_manual_wake_consumes_permit(fence_binary):
    r = observation(fence_binary, "claim", 1, 83)
    assert r["begin"] == "0"
    assert r["qualified"] == "1"
    assert r["fence"] == "82"  # BOOT_ACTIVE, durably written before RAM latch
    assert observation(fence_binary, "claim", 1, 82)["qualified"] == "0"
    assert observation(fence_binary, "claim", 3, 82)["qualified"] == "0"


def test_rearm_requires_host_owned_and_accepted_release_sequence(fence_binary):
    r = observation(fence_binary, "complete", 1, 83)
    assert r["qualified"] == "0"
    assert r["host_before"] == "3"  # ARMED is not PC ownership
    assert r["host"] == "0"
    assert r["release"] == "0"
    assert r["duplicate"] == "3"
    assert r["fence"] == "83"  # READY for exactly next physical WAKE
    assert observation(fence_binary, "claim", 1, r["fence"])["qualified"] == "1"
    # A second legitimate complete cycle is possible; no reuse of BOOT_ACTIVE.
    assert observation(fence_binary, "complete", 1, r["fence"])["fence"] == "83"


def test_no_rearm_without_host_owned_witness(fence_binary):
    r = observation(fence_binary, "nohost", 1, 83)
    assert r["release"] == "3"
    assert r["fence"] == "82"


@pytest.mark.parametrize("failure", ["commit_before", "commit_after", "set", "lock"])
def test_first_write_failure_never_qualifies_boot(fence_binary, failure):
    r = observation(fence_binary, "claim", 1, 83, failure)
    assert r["qualified"] == "0"
    assert r["begin"] != "0"
    if failure == "commit_after":
        # NVS might have committed before reporting an error: stale ACTIVE is
        # still insufficient to requalify after power-cycle/reset.
        assert r["fence"] == "82"
        assert observation(fence_binary, "claim", 1, r["fence"])["qualified"] == "0"


def test_rebooted_normal_recorder_is_always_unqualified(fence_binary):
    assert observation(fence_binary, "normal", 0, 82)["qualified"] == "0"
    assert observation(fence_binary, "normal", 0, 83)["qualified"] == "0"


def test_production_seam_is_not_a_release_enable():
    runtime = (ROOT / "firmware/main/task49_runtime.c").read_text(encoding="utf-8")
    assert runtime.index("release_cycle_fence_begin_boot(manual_wake)") < runtime.index("task87_wake_recovery_init(")
    assert runtime.index("release_cycle_fence_note_host_owned()") < runtime.index("usb_cdc_protocol_open_session();", runtime.index("static void recorder_handle_usb_host_owned"))
    assert runtime.index("usb_msc_ownership_complete_release_quiesce()") < runtime.index("release_cycle_fence_note_release_completed()")
    # The dependent #119 + #144 composite now wires only a proven frozen
    # final-manifest witness; invalid USB / RTC / boot / host states omit it.
    body = runtime[runtime.index("static bool recorder_cdc_status("):runtime.index("static usb_cdc_set_time_result_t")]
    assert "out->release_cycle_id = NULL;" in body
    assert "release_cycle_fence_host_owned_qualified()" in body
    assert "release_cycle_witness_frozen_id()" in body
    assert "state == RECORDER_STATE_USB_SYNC" in body


@pytest.mark.parametrize("failure,expected_marker", [
    ("rearm_commit_before", "82"),
    ("rearm_commit_after", "83"),
    ("rearm_set", "82"),
    ("rearm_lock", "82"),
])
def test_rearm_failure_cannot_retry_same_boot(fence_binary, failure, expected_marker):
    r = observation(fence_binary, "complete", 1, 83, failure)
    assert r["begin"] == "0"
    assert r["host"] == "0"
    assert r["release"] != "0"
    assert r["duplicate"] == "3"
    assert r["qualified"] == "0"
    assert r["fence"] == expected_marker
    if expected_marker == "82":
        # A crash/failed write leaves used BOOT_ACTIVE and cannot requalify,
        # even if stale SHUTDOWN_ARMED and a new physical WAKE are observed.
        assert observation(fence_binary, "claim", 1, 82)["qualified"] == "0"
    else:
        # The hardware may have durably committed READY before an I/O error
        # was reported. The previous host release was already accepted; this
        # simulates a *new* physical WAKE, not an unattended retry.
        assert observation(fence_binary, "claim", 1, 83)["qualified"] == "1"
