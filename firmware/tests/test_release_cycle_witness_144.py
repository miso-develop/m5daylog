"""#144 dependent #120: source-linked cycle witness and CDC publication boundaries.

Synthetic portable-C cases do not substitute for real flash/NVS/power/device
verification, or the separate supervised first-use protocol of Spec #143.
"""
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "firmware"
COMP = FIRMWARE / "components/recorder"

@pytest.fixture(scope="module")
def witness(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("native C compiler unavailable")
    tmp = tmp_path_factory.mktemp("release-cycle-witness")
    source = tmp / "test.c"
    source.write_text(textwrap.dedent(r"""
        #include "release_cycle_witness.h"
        #include <assert.h>
        #include <stdio.h>
        #include <string.h>
        #define ID_A "01234567-89ab-4def-8123-456789abcdef"
        #define ID_B "11111111-2222-4333-8444-555555555555"
        int main(int argc, char **argv) {
            assert(argc == 2);
            assert(release_cycle_witness_frozen_id() == NULL);
            if (strcmp(argv[1], "rotate") == 0) {
                /* A was a finalized rotation: only terminal B freezes. */
                assert(release_cycle_witness_frozen_id() == NULL);
                assert(release_cycle_witness_note_committed(ID_B, 32768u, true, false));
                assert(!release_cycle_witness_note_committed(ID_A, 32768u, true, false));
                assert(strcmp(release_cycle_witness_frozen_id(), ID_B) == 0);
            } else if (strcmp(argv[1], "zero") == 0) {
                /* A had nonzero PCM, but the terminal B was empty. */
                assert(!release_cycle_witness_note_committed(ID_B, 0u, true, false));
                assert(release_cycle_witness_frozen_id() == NULL);
            } else if (strcmp(argv[1], "invalid") == 0) {
                assert(!release_cycle_witness_note_committed(ID_A, 3u, true, false));
                assert(!release_cycle_witness_note_committed(
                       "01234567-89AB-4def-8123-456789abcdef", 32768u, true, false));
                assert(!release_cycle_witness_note_committed(
                       "bad\\\"json", 32768u, true, false));
                assert(!release_cycle_witness_note_committed(NULL, 32768u, true, false));
                assert(release_cycle_witness_frozen_id() == NULL);
            } else if (strcmp(argv[1], "rtc_failure") == 0) {
                assert(!release_cycle_witness_note_committed(ID_B, 32768u, false, false));
                assert(!release_cycle_witness_note_committed(ID_B, 32768u, true, true));
                assert(release_cycle_witness_frozen_id() == NULL);
            } else if (strcmp(argv[1], "failed_commit") == 0) {
                /* No publisher may call note_committed on failed upsert. */
                assert(release_cycle_witness_frozen_id() == NULL);
            } else return 5;
            puts("PASS");
            return 0;
        }
    """), encoding="utf-8")
    exe = tmp / "witness"
    result = subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-I", str(COMP / "include"),
         str(COMP / "release_cycle_witness.c"), str(source), "-o", str(exe)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return exe

@pytest.mark.parametrize("scenario", ["rotate", "zero", "invalid", "rtc_failure", "failed_commit"])
def test_immutable_witness_production_c(witness, scenario):
    run = subprocess.run([str(witness), scenario], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "PASS"

def test_source_manifest_commit_order_and_no_recovered_witness():
    src = (FIRMWARE / "main/main.c").read_text(encoding="utf-8")
    helper = src[src.index("static bool recorder_manifest_record_wav("):
                 src.index("// A blank manifest may be created only")]
    assert helper.index("device_manifest_hash_wav_file(") < helper.index("device_manifest_upsert_file(")
    assert helper.index("if (r == DEVICE_MANIFEST_OK)") < helper.index("*out_verified_pcm_bytes = pcm_bytes;")
    assert "release_cycle_witness_note_committed(" not in helper
    rotation = src[src.index("if (reason == WAV_ROTATE_TIME_30MIN"):src.index("// Finalization and hashing of the preceding WAV")]
    assert "release_cycle_witness_note_committed(" not in rotation
    terminal = src[src.index("uint32_t terminal_verified_pcm_bytes = 0;"):]
    assert terminal.index("recorder_manifest_record_wav(") < terminal.index("release_cycle_witness_note_committed(")
    assert "rtc_correction_boot_clock_verified()" in terminal
    assert "rtc_correction_is_pending()" in terminal
    assert "DEVICE_MANIFEST_STATE_FINALIZED" in terminal
    assert src.count("release_cycle_witness_note_committed(") == 1


def test_status_read_only_and_owner_gate():
    src = (FIRMWARE / "main/task49_runtime.c").read_text(encoding="utf-8")
    status = src[src.index("static bool recorder_cdc_status("):
                 src.index("static usb_cdc_set_time_result_t")]
    assert "release_cycle_fence_host_owned_qualified()" in status
    assert "usb_msc_ownership_is_host_owned()" in status
    assert "state == RECORDER_STATE_USB_SYNC" in status
    assert "!out->rtc_correction_pending" in status
    assert "rtc_correction_boot_clock_verified()" in status
    assert "release_cycle_witness_frozen_id()" in status
    assert "device_manifest_" not in status
    assert "nvs_" not in status
    assert "sd_mount_is_mounted()" in status
    # A manual release without a good current-run manifest must not re-arm
    # another boot's automatic release witness.
    release = src[src.index("static void recorder_handle_usb_release("):
                  src.index("static void recorder_shutdown_armed_now(")]
    assert "release_cycle_witness_frozen_id() != NULL" in release

def test_same_owner_usb_session_cannot_replace_witness():
    src = (FIRMWARE / "main/task49_runtime.c").read_text(encoding="utf-8")
    reattach = src[src.index("static void recorder_handle_usb_host_reattached("):
                   src.index("static void recorder_handle_usb_release(")]
    assert "release_cycle_witness_note_committed" not in reattach
    assert "release_cycle_fence_begin_boot" not in reattach
    assert "usb_cdc_protocol_reset_session()" in reattach

def test_rtc_origin_proof_is_current_boot_and_fails_closed():
    rtc = (COMP / "rtc_correction.c").read_text(encoding="utf-8")
    restore = rtc[rtc.index("static void rtc_sync_system_clock_best_effort_locked("):
                  rtc.index("static void rtc_format_epoch(")]
    assert "s_boot_rtc_clock_verified = false;" in restore
    assert "rtc_hw_read_epoch(" in restore
    assert "settimeofday(&tv, NULL) == 0" in restore
    assert "rtc_hw_deinit() != ESP_OK" in restore
    status = rtc[rtc.index("bool rtc_correction_boot_clock_verified(void)"):
                 rtc.index("bool rtc_correction_is_pending(void)")]
    assert "s_boot_rtc_clock_verified" in status
    apply = rtc[rtc.index("rtc_correction_result_t rtc_correction_apply("):]
    assert apply.index("s_boot_rtc_clock_verified = false;") < apply.index("rtc_hw_write_epoch(requested_epoch)")
