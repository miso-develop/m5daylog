"""Regression tests for #48 terminal ERROR cause immutability.

The portable lifecycle implementation is compiled and executed on the host so
this test exercises the real recorder_state.c behavior. A source-level binding
also prevents recorder_enter_error() from reintroducing a check-then-act race
outside the lifecycle lock.
"""

from pathlib import Path
import subprocess


REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
STATE_H_DIR = COMP / "include"
STATE_C = COMP / "recorder_state.c"
MAIN = REPO / "firmware/main/main.c"


def test_reentering_terminal_error_preserves_first_cause(tmp_path):
    harness = tmp_path / "terminal_error.c"
    executable = tmp_path / "terminal_error"
    harness.write_text(
        r'''
#include <assert.h>
#include <stdint.h>

#include "recorder_state.h"

int main(void) {
    recorder_state_machine_t machine;
    uint32_t first_transition_count;

    recorder_state_machine_init(&machine);
    assert(recorder_state_transition(&machine,
                                     RECORDER_STATE_ERROR,
                                     RECORDER_REASON_MIC_INIT));
    assert(machine.state == RECORDER_STATE_ERROR);
    assert(machine.reason == RECORDER_REASON_MIC_INIT);
    first_transition_count = machine.transition_count;

    assert(!recorder_state_transition(&machine,
                                      RECORDER_STATE_ERROR,
                                      RECORDER_REASON_SD_WRITE));
    assert(machine.state == RECORDER_STATE_ERROR);
    assert(machine.reason == RECORDER_REASON_MIC_INIT);
    assert(machine.transition_count == first_transition_count);
    return 0;
}
''',
        encoding="utf-8",
    )

    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(STATE_H_DIR),
            str(harness),
            str(STATE_C),
            "-o",
            str(executable),
        ],
        check=True,
    )
    subprocess.run([str(executable)], check=True)


def test_error_entry_delegates_atomic_decision_to_locked_transition():
    main = MAIN.read_text(encoding="utf-8")
    start = main.index("static void recorder_enter_error")
    end = main.index("static bool recorder_manifest_record_wav", start)
    enter_error = main[start:end]

    # recorder_transition_state() owns s_state_lock. A separate current-state
    # check here would recreate a check-then-act window between recorder tasks.
    assert "recorder_current_state()" not in enter_error
    assert (
        "recorder_transition_state(RECORDER_STATE_ERROR, reason, 0)"
        in enter_error
    )
