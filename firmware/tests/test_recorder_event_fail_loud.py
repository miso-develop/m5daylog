"""Regression contract for #48 diagnostic-event SD write failures.

The small Python harness mirrors the lifecycle publication boundary in
``recorder_transition_state`` and lets the test inject a failed events.jsonl
append. Source assertions then bind that behavior to the firmware wiring.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MAIN = REPO / "firmware/main/main.c"


class RecorderHarness:
    """Portable model of the fail-loud state/event publication boundary."""

    def __init__(self):
        self.state = "RECOVER"
        self.reason = "recovery"
        self.events_ready = True
        self.stop_requested = False
        self.recording_ready = False

    def transition(self, next_state, reason, append_event):
        previous = self.state
        changed = previous != next_state
        if changed and self.events_ready:
            if not append_event(previous, next_state, reason):
                self.state = "ERROR"
                self.reason = "sd-write"
                self.stop_requested = True
                return False
        self.state = next_state
        self.reason = reason
        return True

    def publish_recording(self, append_event):
        if self.transition("RECORDING", "none", append_event):
            self.recording_ready = True


def test_injected_events_jsonl_write_failure_prevents_recording_publication():
    recorder = RecorderHarness()
    attempts = []

    def fail_event_write(previous, next_state, reason):
        attempts.append((previous, next_state, reason))
        return False

    recorder.publish_recording(fail_event_write)

    assert attempts == [("RECOVER", "RECORDING", "none")]
    assert recorder.state == "ERROR"
    assert recorder.reason == "sd-write"
    assert recorder.stop_requested is True
    assert recorder.recording_ready is False


def test_successful_event_write_allows_recording_publication():
    recorder = RecorderHarness()

    recorder.publish_recording(lambda *_: True)

    assert recorder.state == "RECORDING"
    assert recorder.stop_requested is False
    assert recorder.recording_ready is True


def test_firmware_wiring_matches_fail_loud_model():
    main = MAIN.read_text(encoding="utf-8")
    start = main.index("static bool recorder_transition_state")
    end = main.index("static void recorder_enter_error", start)
    transition = main[start:end]

    # The requested state is staged in a candidate; it must not be published
    # to s_rec_state until the corresponding diagnostic event is durable.
    assert "recorder_state_machine_t candidate" in transition
    assert "candidate = s_rec_state" in transition
    assert "recorder_state_transition(&candidate, next, reason)" in transition
    append_at = transition.index("if (!recorder_state_append_event(")
    commit_at = transition.index("s_rec_state = candidate", append_at)
    assert commit_at > append_at

    unlock_at = transition.index("xSemaphoreGive(s_state_lock);", append_at)
    append_fail_path = transition[append_at:unlock_at]
    assert "RECORDER_STATE_ERROR" in append_fail_path
    assert "RECORDER_REASON_SD_WRITE" in append_fail_path
    assert "result: warning, reason: event append" not in append_fail_path

    # Every caller receives a failed transition and sticky STOP after an
    # event-write failure, so capture cannot publish RECORDING_READY.
    assert "event_write_failed" in transition
    failure_at = transition.index("if (event_write_failed)")
    assert transition.index("recorder_request_stop();", failure_at) > failure_at
    assert transition.index("return false;", failure_at) > failure_at

    capture_start = main.index("static void recorder_capture_task")
    writer_start = main.index("static void recorder_writer_task", capture_start)
    capture = main[capture_start:writer_start]
    transition_call = capture.index(
        "if (!recorder_transition_state(RECORDER_STATE_RECORDING"
    )
    ready_at = capture.index("REC_BIT_RECORDING_READY", transition_call)
    else_at = capture.index("} else {", transition_call)
    assert transition_call < else_at < ready_at
