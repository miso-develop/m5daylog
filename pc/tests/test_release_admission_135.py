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
        result2 = self.seq.normal_cycle(
            candidate=normal, verify_current_cycle=lambda c, t: True,
        )
        self.assertTrue(result2.success)
        with self.fence.cycles.serialized():
            self.fence.cycles.verify_reserved_locked(
                DEV, NEXT_CYCLE, result2.attempt_id,
            )
        self.assertEqual(
            self.seq.normal_cycle(
                candidate=normal, verify_current_cycle=lambda c, t: True,
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
