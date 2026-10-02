"""Regression contract for #48 diagnostic-event SD write failures."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MAIN = REPO / "firmware/main/main.c"


def test_events_jsonl_write_failure_forces_error_and_stop():
    main = MAIN.read_text(encoding="utf-8")
    start = main.index("static bool recorder_transition_state")
    end = main.index("static void recorder_enter_error", start)
    transition = main[start:end]

    append_fail_at = transition.index("if (!recorder_state_append_event(")
    unlock_at = transition.index("xSemaphoreGive(s_state_lock);", append_fail_at)
    append_fail_path = transition[append_fail_at:unlock_at]

    # Once diagnostics are expected to be writable, an events.jsonl write
    # failure is itself an SD-write failure. It must become fail-loud before
    # the requested lifecycle transition can be reported as successful.
    assert "RECORDER_STATE_ERROR" in append_fail_path
    assert "RECORDER_REASON_SD_WRITE" in append_fail_path
    assert "result: warning, reason: event append" not in append_fail_path

    # The transition helper owns this failure boundary, so every caller gets
    # the same terminal behavior: stop recorder tasks and report failure.
    assert "recorder_request_stop();" in transition
    assert "event_write_failed" in transition
    failure_at = transition.index("if (event_write_failed)")
    assert transition.index("recorder_request_stop();", failure_at) > failure_at
    assert transition.index("return false;", failure_at) > failure_at
