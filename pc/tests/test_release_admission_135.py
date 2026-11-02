"""#135 source-only canonical release-sequence regressions; fake host/CDC only."""
from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from m5daylog.capacity import VerifiedSession
from m5daylog.first_use_admission import CycleOrigin
from m5daylog.release import (
    EjectTarget, PreconditionBlocked, ReleaseIndeterminate,
    SyncReleaseCoordinator,
)
from m5daylog.release_admission_135 import SourceOnlyReleaseSequence
from m5daylog.release_cycle_candidate import QualifiedCycleCandidate
from m5daylog.sync import SyncSummary
from test_dev_install_release_fence_135 import DEV, ATTEMPT, CYCLE, make, receipt

NEXT_CYCLE = "00000000-0000-4000-8000-000000000012"
TARGET = EjectTarget(
    DEV, "E:\\",
    "\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\",
    8, "disk", "removable", "usb-parent",
)


class FakePort:
    """No Win32/Device connections. A fake #89 lock/dismount/CDC port."""

    def __init__(self):
        self.events = []
        self.sent = []
        self.indeterminate = False

    def resolve_target(self, mount_root, expected_device_id):
        self.events.append("resolve")
        if mount_root != TARGET.mount_root or expected_device_id != DEV:
            raise AssertionError("wrong target")
        return TARGET

    def revalidate_target(self, target, cdc_port):
        self.events.append("revalidate")
        if target != TARGET or cdc_port != "COM5":
            raise AssertionError("stale target")

    def volume_available(self, target):
        self.events.append("available")
        return True

    @contextmanager
    def quiesce(self, target):
        self.events.extend(("lock", "dismount"))
        try:
            yield
        finally:
            self.events.append("unlock")

    def release_storage(self, cdc_port, attempt_id, target):
        self.events.append("release")
        self.sent.append(attempt_id)
        if self.indeterminate:
            raise ReleaseIndeterminate("simulated lost response")

    def volume_disappeared(self, target):
        self.events.append("disappeared")
        return True


class SourceOnlySequenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "device").mkdir()
        self.fence = make(self.root)
        self.port = FakePort()
        self.session = VerifiedSession(TARGET, "COM5", "test-session-epoch")
        self.events = self.port.events
        self.epoch = self.session.session_epoch
        self.preseal = lambda target, gate: self._preseal()
        self.seq = self.sequence()

    def _preseal(self):
        self.events.append("preseal")
        return True

    def sequence(self, *, session=None, port=None, preseal=None):
        port = port or self.port
        return SourceOnlyReleaseSequence(
            fence=self.fence, coordinator=SyncReleaseCoordinator(port),
            session=session or self.session,
            sync=lambda: self._synced(port),
            current_epoch=lambda: self.epoch,
            cdc_mutations=lambda: port.events.append("rtc"),
            before_seal_observation=preseal or (lambda t, g: self._preseal()),
        )

    @staticmethod
    def _synced(port):
        port.events.append("sync")
        return SyncSummary(copied=1)

    def dev_first(self, *, seq=None, verify=None):
        return (seq or self.seq).dev_first_release(
            receipt=receipt(),
            verify_external=verify or (lambda r, s, t: True),
        )

    def first_cycle(self, *, seq=None, status=None, verify=None):
        seq = seq or self.seq
        status = status or {
            "state": "USB_SYNC",
            "rtcCorrectionPending": False,
            "releaseCycleId": CYCLE,
            "releaseCycleOrigin": "SUPERVISED_FIRST_USE",
        }
        candidate = QualifiedCycleCandidate(
            DEV, CYCLE, CycleOrigin.SUPERVISED_FIRST_USE, seq.session,
        )
        return seq.first_supervised_cycle(
            receipt=receipt(), candidate=candidate, status=status,
            verify_first_wake=verify or (lambda r, s, sess, target: True),
        )

    def h0_first(self, *, status=None, verifier=None):
        status = status if status is not None else {
            "state": "USB_SYNC",
            "rtcCorrectionPending": False,
            "releaseCycleId": CYCLE,
            "releaseCycleOrigin": "SUPERVISED_FIRST_USE",
        }
        candidate = QualifiedCycleCandidate(
            DEV, CYCLE, CycleOrigin.SUPERVISED_FIRST_USE, self.session,
        )
        return self.seq.h0_first_recording_release(
            receipt=receipt(), candidate=candidate,
            read_current_status=lambda: dict(status),
            verify_external=verifier or (lambda r, c, s, binding, target: True),
        )

    def test_h0_consumes_dev_and_r3_seq1_before_single_release_then_normal(self):
        first = self.h0_first()
        self.assertTrue(first.success)
        self.assertEqual(first.attempt_id, ATTEMPT)
        self.assertEqual(self.port.sent, [ATTEMPT])
        self.assertLess(self.events.index("sync"), self.events.index("rtc"))
        self.assertLess(self.events.index("rtc"), self.events.index("lock"))
        self.assertLess(self.events.index("lock"), self.events.index("release"))
        with self.fence.cycles.serialized():
            self.fence.verify_consumed_locked(receipt())
            self.fence.cycles.verify_reserved_locked(DEV, CYCLE, ATTEMPT)
            state = self.fence.cycles.read_enrolled_state_locked(DEV)
            self.assertEqual(state["v"], 1)
            self.assertEqual(state["seq"], 1)
        self.assertEqual(self.h0_first().reason, "PRECONDITION_BLOCKED")
        self.assertEqual(self.port.sent, [ATTEMPT])

        normal_cycle = QualifiedCycleCandidate(
            DEV, NEXT_CYCLE, CycleOrigin.NORMAL_WAKE, self.session,
        )
        normal = self.seq.normal_cycle(
            candidate=normal_cycle,
            verify_current_cycle=lambda c, target: True,
            read_current_status=lambda: {
                "state": "USB_SYNC",
                "releaseCycleId": NEXT_CYCLE,
                "releaseCycleOrigin": "NORMAL_WAKE",
                "rtcCorrectionPending": False,
            },
        )
        self.assertTrue(normal.success)
        with self.fence.cycles.serialized():
            self.fence.cycles.verify_reserved_locked(
                DEV, NEXT_CYCLE, normal.attempt_id,
            )
        self.assertEqual(len(self.port.sent), 2)

    def test_h0_requires_independent_grant_and_current_first_origin(self):
        denied = self.h0_first(verifier=lambda *args: False)
        self.assertEqual(denied.reason, "PRECONDITION_BLOCKED")
        self.assertEqual(self.port.sent, [])
        self.assertNotIn("rtc", self.events)
        self.assertFalse(self.fence.database.exists())
        self.assertFalse(self.fence.cycles.database.exists())

        invalid = self.h0_first(status={
            "state": "USB_SYNC",
            "rtcCorrectionPending": False,
            "releaseCycleId": CYCLE,
            "releaseCycleOrigin": "NORMAL_WAKE",
        })
        self.assertEqual(invalid.reason, "PRECONDITION_BLOCKED")
        self.assertEqual(self.port.sent, [])
        self.assertFalse(self.fence.database.exists())

    def test_h0_external_verifier_error_cannot_spend_or_send(self):
        calls = []

        def failing_verifier(*args):
            calls.append(1)
            raise RuntimeError("synthetic private evidence unavailable")

        outcome = self.h0_first(verifier=failing_verifier)
        self.assertEqual(outcome.reason, "PRECONDITION_BLOCKED")
        self.assertEqual(calls, [1])
        self.assertEqual(self.port.sent, [])
        self.assertNotIn("rtc", self.events)
        self.assertFalse(self.fence.database.exists())
        self.assertFalse(self.fence.cycles.database.exists())

    def test_h0_split_dev_or_r3_commit_never_retries(self):
        for anchor in ("dev", "r3"):
            with self.subTest(anchor=anchor), tempfile.TemporaryDirectory() as d:
                self.root = Path(d)
                (self.root / "device").mkdir()
                self.fence = make(self.root)
                self.port = FakePort()
                self.events = self.port.events
                self.seq = self.sequence()
                broken = (self.fence.anchor if anchor == "dev"
                          else self.fence.cycles.anchor)
                broken.fail_write = True
                first = self.h0_first()
                self.assertEqual(first.reason, "PRECONDITION_BLOCKED")
                self.assertEqual(self.port.sent, [])
                self.assertNotIn("rtc", self.events)
                broken.fail_write = False
                second = self.h0_first()
                self.assertEqual(second.reason, "PRECONDITION_BLOCKED")
                self.assertEqual(self.port.sent, [])

    def test_h0_changed_pair_after_rtc_blocks_without_resend(self):
        status = {
            "state": "USB_SYNC",
            "rtcCorrectionPending": False,
            "releaseCycleId": CYCLE,
            "releaseCycleOrigin": "SUPERVISED_FIRST_USE",
        }
        self.seq.cdc_mutations = lambda: status.update(
            releaseCycleId=NEXT_CYCLE,
        )
        first = self.h0_first(status=status)
        self.assertEqual(first.reason, "PRECONDITION_BLOCKED")
        self.assertEqual(self.port.sent, [])
        self.assertNotIn("lock", self.events)
        status["releaseCycleId"] = CYCLE
        self.assertEqual(self.h0_first(status=status).reason, "PRECONDITION_BLOCKED")
        self.assertEqual(self.port.sent, [])

    def test_first_dev_attempt_is_pinned_consumed_before_rtc_and_sent_once(self):
        result = self.dev_first()
        self.assertTrue(result.success)
        self.assertEqual(result.attempt_id, ATTEMPT)
        self.assertEqual(self.port.sent, [ATTEMPT])
        self.assertLess(self.events.index("sync"), self.events.index("rtc"))
        self.assertLess(self.events.index("rtc"), self.events.index("preseal"))
        self.assertLess(self.events.index("preseal"), self.events.index("lock"))
        self.assertLess(self.events.index("lock"), self.events.index("release"))
        self.assertFalse(self.fence.cycles.database.exists())
        with self.fence.cycles.serialized():
            self.fence.verify_consumed_locked(receipt())
        restarted = self.sequence(port=FakePort())
        retried = self.dev_first(seq=restarted)
        self.assertEqual(retried.reason, "PRECONDITION_BLOCKED")
        self.assertNotIn("rtc", restarted.coordinator.port.events)
        self.assertEqual(restarted.coordinator.port.sent, [])

    def test_external_dev_denial_or_stale_epoch_never_consumes_or_sends(self):
        denied = self.dev_first(verify=lambda r, s, t: False)
        self.assertEqual(denied.reason, "PRECONDITION_BLOCKED")
        self.assertEqual(self.port.sent, [])
        self.assertNotIn("rtc", self.events)
        self.assertFalse(self.fence.database.exists())
        self.epoch = "stale-epoch"
        with self.assertRaises(PreconditionBlocked):
            self.dev_first()
        self.assertFalse(self.fence.database.exists())

    def test_split_commit_or_preseal_failure_burns_first_attempt(self):
        self.fence.anchor.fail_write = True
        result = self.dev_first()
        self.assertEqual(result.reason, "PRECONDITION_BLOCKED")
        self.assertNotIn("rtc", self.events)
        self.assertEqual(self.port.sent, [])
        self.fence.anchor.fail_write = False
        second = self.dev_first()
        self.assertEqual(second.reason, "PRECONDITION_BLOCKED")
        self.assertEqual(self.port.sent, [])

    def test_preseal_denial_or_lost_cdc_response_never_reissues(self):
        seq = self.sequence(preseal=lambda t, gate: False)
        result = self.dev_first(seq=seq)
        self.assertEqual(result.reason, "PRECONDITION_BLOCKED")
        self.assertEqual(self.port.sent, [])
        self.assertEqual(self.dev_first().reason, "PRECONDITION_BLOCKED")

        with tempfile.TemporaryDirectory() as other:
            self.root = Path(other)
            (self.root / "device").mkdir()
            self.fence = make(self.root)
            self.port = FakePort()
            self.port.indeterminate = True
            self.events = self.port.events
            self.seq = self.sequence()
            result = self.dev_first()
            self.assertEqual(result.reason, "INDETERMINATE")
            self.assertEqual(self.port.sent, [ATTEMPT])
            self.port.indeterminate = False
            self.assertEqual(self.dev_first().reason, "PRECONDITION_BLOCKED")
            self.assertEqual(self.port.sent, [ATTEMPT])

    def test_supervised_post_wake_has_different_r3_attempt_then_normal_cycle(self):
        self.assertTrue(self.dev_first().success)
        self.events.clear()
        result = self.first_cycle()
        self.assertTrue(result.success)
        self.assertNotEqual(result.attempt_id, ATTEMPT)
        with self.fence.cycles.serialized():
            self.fence.cycles.verify_reserved_locked(DEV, CYCLE, result.attempt_id)
        self.assertEqual(self.first_cycle().reason, "PRECONDITION_BLOCKED")
        self.assertEqual(len(self.port.sent), 2)

        normal = QualifiedCycleCandidate(
            DEV, NEXT_CYCLE, CycleOrigin.NORMAL_WAKE, self.session,
        )
        normal_status = lambda: {
            "state": "USB_SYNC", "releaseCycleId": NEXT_CYCLE,
            "releaseCycleOrigin": "NORMAL_WAKE",
            "rtcCorrectionPending": False,
        }
        result2 = self.seq.normal_cycle(
            candidate=normal, verify_current_cycle=lambda c, t: True,
            read_current_status=normal_status,
        )
        self.assertTrue(result2.success)
        with self.fence.cycles.serialized():
            self.fence.cycles.verify_reserved_locked(
                DEV, NEXT_CYCLE, result2.attempt_id,
            )
        self.assertEqual(
            self.seq.normal_cycle(
                candidate=normal, verify_current_cycle=lambda c, t: True,
                read_current_status=normal_status,
            ).reason,
            "PRECONDITION_BLOCKED",
        )
        self.assertEqual(len(self.port.sent), 3)

    def test_origin_mismatch_and_unverified_first_wake_fail_closed(self):
        self.assertTrue(self.dev_first().success)
        with self.assertRaises(PreconditionBlocked):
            self.first_cycle(status={
                "state": "USB_SYNC",
                "rtcCorrectionPending": False,
                "releaseCycleId": CYCLE,
                "releaseCycleOrigin": "NORMAL_WAKE",
            })
        self.events.clear()
        denied = self.first_cycle(verify=lambda r, s, sess, target: False)
        self.assertEqual(denied.reason, "PRECONDITION_BLOCKED")
        self.assertNotIn("rtc", self.events)
        self.assertEqual(len(self.port.sent), 1)
        self.assertFalse(self.fence.cycles.database.exists())

    def test_post_consume_anchor_tamper_blocks_at_presend(self):
        def tamper(target, gate):
            self.fence.anchor.write({"v": 1, "kind": "CORRUPT"})
            return True
        seq = self.sequence(preseal=tamper)
        result = self.dev_first(seq=seq)
        self.assertEqual(result.reason, "PRECONDITION_BLOCKED")
        self.assertEqual(self.port.sent, [])
        self.assertIn("lock", self.events)
        self.assertNotIn("release", self.events)


if __name__ == "__main__":
    unittest.main()
