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

# R3 DC-03d: full source-linked NVS state interleavings with both the real
# shutdown_armed.c lifecycle and release_cycle_fence.c (one process per boot).
# The 1/83 seed denotes a previously ACCEPTED release, not first-use setup.
@pytest.fixture(scope="module")
def lifecycle_binary(fence_binary):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    assert cc is not None
    root = Path(fence_binary).parent
    exe = root / "release_cycle_lifecycle"
    build = subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-DESP_PLATFORM",
         "-I", str(root / "include"), "-I", str(COMP / "include"),
         str(COMP / "release_cycle_fence.c"),
         str(COMP / "shutdown_armed.c"),
         str(ROOT / "firmware/tests/native/release_cycle_lifecycle_harness.c"),
         "-o", str(exe)],
        capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    return exe


def lifecycle_run(exe, lifecycle=1, fence=83, fault_write=0, phase="none", mode="cycle"):
    result = subprocess.run(
        [str(exe), mode, str(lifecycle), str(fence), str(fault_write), phase],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    return {k: int(v) for k, v in (part.split("=", 1)
                                    for part in result.stdout.strip().split())}


@pytest.mark.parametrize("write", [1, 2, 3, 4, 5, 6])
@pytest.mark.parametrize("phase", ["set", "before", "after"])
def test_each_production_lifecycle_nvs_boundary_fails_closed(lifecycle_binary, write, phase):
    # 1 BOOT_ACTIVE, 2 PENDING, 3 NORMAL, 4 HOST_UNRESOLVED,
    # 5 SHUTDOWN_ARMED after accepted D-031, 6 READY_AFTER_RELEASE.
    stopped = lifecycle_run(lifecycle_binary, fault_write=write, phase=phase)
    assert stopped["result"] != 0
    assert stopped["writes"] == write
    # A reset discards ALL volatile boot latches; a newly asserted WAKE
    # cannot borrow BOOT_ACTIVE, PENDING, or a stale committed ARMED.
    next_boot = lifecycle_run(lifecycle_binary,
                              lifecycle=stopped["lifecycle"],
                              fence=stopped["fence"], mode="reboot")
    permit_present = (stopped["lifecycle"] == 1 and stopped["fence"] == 83)
    assert (next_boot["qualified"] == 1) == permit_present
    if not permit_present:
        assert next_boot["result"] != 0
    # An untouched initial READY (write 1 pre-commit failure) permits an
    # actual NEW physical WAKE; phase 6 post-commit READY follows accepted
    # host release. No interrupted BOOT_ACTIVE is ever requalified.


def test_two_separate_accepted_daily_cycles(lifecycle_binary):
    day1 = lifecycle_run(lifecycle_binary)
    assert day1 == {"result": 0, "lifecycle": 1, "fence": 83,
                    "writes": 6, "qualified": 1, "host": 1, "live": 0}
    # New process boot and a newly asserted manual WAKE, not rotation.
    day2 = lifecycle_run(lifecycle_binary, day1["lifecycle"], day1["fence"])
    assert day2 == day1
    stale = lifecycle_run(lifecycle_binary, 1, 82, mode="reboot")
    assert stale["qualified"] == 0 and stale["result"] != 0
    interrupted = lifecycle_run(lifecycle_binary, 3, 82, mode="reboot")
    assert interrupted["qualified"] == 0 and interrupted["result"] != 0
    normal = lifecycle_run(lifecycle_binary, 0, 82, mode="reboot")
    assert normal["qualified"] == 0 and normal["result"] != 0


def test_no_auto_release_authority_beyond_device_fence():
    runtime = (ROOT / "firmware/main/task49_runtime.c").read_text(encoding="utf-8")
    release = runtime[runtime.index("static void recorder_handle_usb_release("):
                      runtime.index("static void recorder_shutdown_armed_now(")]
    assert release.index("usb_msc_ownership_complete_release_quiesce()") < release.index(
        "release_cycle_fence_note_release_completed()")
    assert "release_cycle_witness_frozen_id() != NULL" in release


# R5-FU source-only: an explicit pre-existing UNQUALIFIED marker plus
# distinct USB-owned physical proof is a staging prerequisite. NVS absence,
# stale/used states and a staged-but-unaccepted release are not boot permits.
@pytest.mark.parametrize("fence", ["missing", 82, 83, 84, 85, 99])
def test_first_use_refuses_missing_or_previously_used_marker(fence_binary, fence):
    r = observation(fence_binary, "fu_complete", 2, fence)
    assert r["stage"] != "0"
    assert r["first_use"] == "0"
    assert r["fence"] == ("-1" if fence == "missing" else str(fence))


def test_first_use_stages_then_requires_canonical_accepted_release(fence_binary):
    staged = observation(fence_binary, "fu_stage", 2, 81)
    assert staged["stage"] == "0" and staged["fence"] == "84"
    assert staged["first_use"] == "1"
    assert observation(fence_binary, "claim", 1, 84)["qualified"] == "0"

    no_accept = observation(fence_binary, "fu_unaccepted", 2, 81)
    assert no_accept["stage"] == "0" and no_accept["release"] != "0"
    assert no_accept["fence"] == "84"
    detached = observation(fence_binary, "fu_cutoff", 2, 81)
    assert detached["stage"] == "0" and detached["release"] != "0"
    assert detached["fence"] == "84"

    released = observation(fence_binary, "fu_complete", 2, 81)
    assert released["stage"] == "0"
    assert released["release"] == "0" and released["fence"] == "85"
    assert released["first_use"] == "0"
    # The next real manual-WAKE consumes FU_READY in the same durable
    # BOOT_ACTIVE transition and retains an exact RAM-only origin.
    boot = observation(fence_binary, "claim", 1, 85)
    assert boot["qualified"] == "1" and boot["fence"] == "82"
    assert boot["origin"] == "SUPERVISED_FIRST_USE"
    assert observation(fence_binary, "claim", 1, 82)["qualified"] == "0"

    # A subsequent independently accepted normal release restores R3 READY.
    day2 = observation(fence_binary, "complete", 1, 85)
    assert day2["fence"] == "83"
    assert day2["origin"] == "NONE"
    normal = observation(fence_binary, "claim", 1, 83)
    assert normal["qualified"] == "1" and normal["origin"] == "NORMAL_WAKE"


@pytest.mark.parametrize("failure", ["set", "commit_before", "commit_after", "lock"])
def test_failed_supervised_staging_never_grants_release(fence_binary, failure):
    r = observation(fence_binary, "fu_complete", 2, 81, failure)
    assert r["stage"] != "0"
    assert r["first_use"] == "0"
    assert r["fence"] in ("81", "84")
    # Even after an uncertain commit, FU_STAGED is not a manual-WAKE permit.
    assert observation(fence_binary, "claim", 1, r["fence"])["qualified"] == "0"


def test_first_use_real_lifecycle_commit_order(lifecycle_binary):
    staged = lifecycle_run(lifecycle_binary, lifecycle=0, fence=81, mode="fu_cycle")
    assert staged == {"result": 0, "lifecycle": 1, "fence": 85,
                      "writes": 4, "qualified": 0, "host": 0, "live": 0}
    supervised_boot = lifecycle_run(
        lifecycle_binary, lifecycle=staged["lifecycle"],
        fence=staged["fence"], mode="cycle")
    assert supervised_boot == {"result": 0, "lifecycle": 1, "fence": 83,
                               "writes": 6, "qualified": 1, "host": 1, "live": 0}
    day2 = lifecycle_run(lifecycle_binary, lifecycle=1, fence=83, mode="cycle")
    assert day2 == supervised_boot
    day3 = lifecycle_run(lifecycle_binary, lifecycle=1, fence=83, mode="cycle")
    assert day3 == supervised_boot


@pytest.mark.parametrize("write", [1, 2, 3, 4])
@pytest.mark.parametrize("phase", ["set", "before", "after"])
def test_first_use_every_commit_boundary_fails_closed(lifecycle_binary, write, phase):
    stopped = lifecycle_run(lifecycle_binary, lifecycle=0, fence=81,
                            fault_write=write, phase=phase, mode="fu_cycle")
    assert stopped["result"] != 0
    assert stopped["writes"] == write
    # A second process loses all volatile proof. A fresh physical WAKE
    # qualifies only if the exact accepted release and FU_READY commit
    # were already durable; staged/unresolved/erased state cannot grant it.
    reboot = lifecycle_run(lifecycle_binary, lifecycle=stopped["lifecycle"],
                           fence=stopped["fence"], mode="reboot")
    ready = stopped["lifecycle"] == 1 and stopped["fence"] == 85
    assert (reboot["qualified"] == 1) == ready
    if not ready:
        assert reboot["result"] != 0


@pytest.mark.parametrize("mode", [
    "fu_bad_hold_before_accept", "fu_bad_hold_after_accept",
])
def test_lost_physical_hold_burns_staged_intent_even_after_accept(fence_binary, mode):
    # The post-stage GPIO/ISR proof may fail while a CDC request is racing.
    # Neither order can turn FU_STAGED into an eligible next-boot permit.
    r = observation(fence_binary, mode, 2, 81)
    assert r["stage"] == "0"
    assert r["first_use"] == "0"
    assert r["release"] != "0"
    assert r["fence"] == "84"
    assert observation(fence_binary, "claim", 1, r["fence"])["qualified"] == "0"
