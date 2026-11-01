"""SDB-1.1 already-enrolled DEV first recording: source-only fake #89 path."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from m5daylog.capacity import VerifiedSession
from m5daylog.first_use_admission import CycleOrigin
from m5daylog.release import PreconditionBlocked, SyncReleaseCoordinator
from m5daylog.release_admission_135 import SourceOnlyReleaseSequence
from m5daylog.release_cycle_candidate import QualifiedCycleCandidate
from m5daylog.sync import SyncSummary
from test_dev_install_release_fence_135 import DEV, GEN, ATTEMPT, CYCLE, make, receipt
from test_release_admission_135 import FakePort, TARGET

OLD = "00000000-0000-4000-8000-000000000091"
OLD_CYCLE = "00000000-0000-4000-8000-000000000092"
OLD_ATTEMPT = "00000000-0000-4000-8000-000000000093"
NEXT_CYCLE = "00000000-0000-4000-8000-000000000095"


class SdbGenerationReleaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "device").mkdir()
        self.fence = make(self.root)
        with self.fence.cycles.serialized():
            self.fence.cycles.initialize_first_cycle_locked(
                OLD, OLD_CYCLE, OLD_ATTEMPT
            )
            old = self.fence.cycles.read_enrolled_state_locked(OLD)
        host_digest = hashlib.sha256(json.dumps(
            old, sort_keys=True, separators=(",", ":")
        ).encode("ascii")).hexdigest()
        self.receipt = replace(receipt(), host_lineage_digest=host_digest)
        self.port = FakePort()
        self.session = VerifiedSession(TARGET, "COM5", "live-epoch")
        self.epoch = "live-epoch"
        self.events = self.port.events
        self.status = {
            "state": "USB_SYNC",
            "releaseCycleId": CYCLE,
            "releaseCycleOrigin": "SUPERVISED_FIRST_USE",
            "rtcCorrectionPending": False,
        }
        self.seq = SourceOnlyReleaseSequence(
            fence=self.fence, coordinator=SyncReleaseCoordinator(self.port),
            session=self.session,
            sync=lambda: self.sync(),
            current_epoch=lambda: self.epoch,
            cdc_mutations=lambda: self.events.append("rtc"),
            before_seal_observation=lambda target, gate: True,
        )

    def sync(self):
        self.events.append("sync")
        return SyncSummary(copied=1)

    def current(self):
        self.events.append("get_status")
        return dict(self.status)

    def attempt(self, *, external=None, current=None, previous_device_id=OLD):
        candidate = QualifiedCycleCandidate(
            DEV, CYCLE, CycleOrigin.SUPERVISED_FIRST_USE, self.session
        )
        return self.seq.dev_generation_first_release(
            receipt=self.receipt, previous_device_id=previous_device_id,
            candidate=candidate, read_current_status=current or self.current,
            verify_external=external or (lambda r, c, s, sess, target: True),
        )

    def test_positive_enrolled_generation_is_consumed_before_any_cdc_send(self):
        outcome = self.attempt()
        self.assertTrue(outcome.success)
        self.assertEqual(self.port.sent, [ATTEMPT])
        self.assertLess(self.events.index("sync"), self.events.index("get_status"))
        self.assertLess(self.events.index("get_status"), self.events.index("rtc"))
        self.assertLess(self.events.index("rtc"), self.events.index("lock"))
        self.assertLess(self.events.index("lock"), self.events.index("release"))
        with self.fence.cycles.serialized():
            self.fence.verify_consumed_locked(self.receipt)
            self.fence.cycles.verify_dev_generation_locked(
                DEV, GEN, self.receipt.external_permit_digest, CYCLE, ATTEMPT
            )
        with closing(sqlite3.connect(self.fence.cycles.database)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM consumed"
            ).fetchone(), (2,))
        self.assertEqual(self.attempt().reason, "PRECONDITION_BLOCKED")
        self.assertEqual(self.port.sent, [ATTEMPT])

    def test_real_normal_origin_then_second_distinct_cycle_succeeds_in_model(self):
        self.assertTrue(self.attempt().success)
        normal = QualifiedCycleCandidate(
            DEV, NEXT_CYCLE, CycleOrigin.NORMAL_WAKE, self.session
        )
        out = self.seq.normal_cycle(
            candidate=normal, verify_current_cycle=lambda c, target: True
        )
        self.assertTrue(out.success)
        with self.fence.cycles.serialized():
            self.fence.cycles.verify_reserved_locked(
                DEV, NEXT_CYCLE, out.attempt_id
            )
        self.assertEqual(self.seq.normal_cycle(
            candidate=normal, verify_current_cycle=lambda c, target: True
        ).reason, "PRECONDITION_BLOCKED")
        self.assertEqual(len(self.port.sent), 2)

    def test_wrong_live_origin_epoch_or_external_denial_sends_zero_bytes(self):
        self.status["releaseCycleOrigin"] = "NORMAL_WAKE"
        self.assertEqual(self.attempt().reason, "PRECONDITION_BLOCKED")
        self.assertEqual(self.port.sent, [])
        self.status["releaseCycleOrigin"] = "SUPERVISED_FIRST_USE"
        self.epoch = "stale-epoch"
        with self.assertRaises(PreconditionBlocked):
            self.attempt()
        self.epoch = "live-epoch"
        self.assertEqual(self.attempt(external=lambda *args: False).reason,
                         "PRECONDITION_BLOCKED")
        self.assertFalse(self.fence.database.exists())
        self.assertEqual(self.port.sent, [])

    def test_external_proof_failure_cannot_abort_monitor_with_raw_exception(self):
        def unavailable(*args):
            raise RuntimeError("synthetic private verification error")
        self.assertEqual(self.attempt(external=unavailable).reason,
                         "PRECONDITION_BLOCKED")
        self.assertFalse(self.fence.database.exists())
        self.assertEqual(self.port.sent, [])
        self.assertNotIn("rtc", self.events)

    def test_prior_host_anchor_corrupt_does_not_write_or_send(self):
        self.fence.cycles.anchor.write({"v": 1, "device": "f" * 64,
                                       "seq": 1, "tip": "a" * 64})
        self.assertEqual(self.attempt().reason, "PRECONDITION_BLOCKED")
        self.assertFalse(self.fence.database.exists())
        self.assertEqual(self.port.sent, [])

    def test_split_dev_or_r3_commit_denies_retries_and_sends_zero_bytes(self):
        self.fence.anchor.fail_write = True
        self.assertEqual(self.attempt().reason, "PRECONDITION_BLOCKED")
        self.fence.anchor.fail_write = False
        self.assertEqual(self.attempt().reason, "PRECONDITION_BLOCKED")
        self.assertEqual(self.port.sent, [])
        self.assertNotIn("rtc", self.events)

        with tempfile.TemporaryDirectory() as other:
            self.setUp_in_new_root(Path(other))
            self.fence.cycles.anchor.fail_write = True
            self.assertEqual(self.attempt().reason, "PRECONDITION_BLOCKED")
            self.fence.cycles.anchor.fail_write = False
            self.assertEqual(self.attempt().reason, "PRECONDITION_BLOCKED")
            self.assertEqual(self.port.sent, [])

    def setUp_in_new_root(self, root, *, old_device=OLD):
        (root / "device").mkdir()
        self.root = root
        self.fence = make(root)
        with self.fence.cycles.serialized():
            self.fence.cycles.initialize_first_cycle_locked(
                old_device, OLD_CYCLE, OLD_ATTEMPT
            )
            old = self.fence.cycles.read_enrolled_state_locked(old_device)
        digest = hashlib.sha256(json.dumps(
            old, sort_keys=True, separators=(",", ":")
        ).encode("ascii")).hexdigest()
        self.receipt = replace(receipt(), host_lineage_digest=digest)
        self.port = FakePort()
        self.events = self.port.events
        self.seq = SourceOnlyReleaseSequence(
            fence=self.fence, coordinator=SyncReleaseCoordinator(self.port),
            session=self.session, sync=lambda: self.sync(),
            current_epoch=lambda: self.epoch,
            cdc_mutations=lambda: self.events.append("rtc"),
            before_seal_observation=lambda t, g: True,
        )

    def test_same_old_and_new_device_id_blocks_before_dev_consumption(self):
        with tempfile.TemporaryDirectory() as other:
            self.setUp_in_new_root(Path(other), old_device=DEV)
            with self.fence.cycles.serialized():
                before = self.fence.cycles.read_enrolled_state_locked(DEV)
            self.assertEqual(
                self.attempt(previous_device_id=DEV).reason,
                "PRECONDITION_BLOCKED",
            )
            self.assertFalse(self.fence.database.exists())
            self.assertIsNone(self.fence.anchor.read())
            with self.fence.cycles.serialized():
                self.assertEqual(self.fence.cycles.read_enrolled_state_locked(DEV), before)
            self.assertNotIn("rtc", self.events)
            self.assertEqual(self.port.sent, [])

    def test_dev_commit_before_r3_begin_blocks_old_reservation_and_sender(self):
        # Reproduce ER-135-SDB-02 at the exact DEV-committed / R3-not-begun
        # boundary. Old V1 DB+anchor are still individually healthy.
        with patch.object(
            self.fence.cycles, "append_dev_generation_first_cycle_locked",
            side_effect=PreconditionBlocked("synthetic R3 BEGIN failure"),
        ):
            self.assertEqual(self.attempt().reason, "PRECONDITION_BLOCKED")
        self.assertEqual(self.fence._read()["kind"],
                         "SAME_HOST_DEV_GENERATION_FIRST_RELEASE")
        with self.fence.cycles.serialized():
            self.assertEqual(self.fence.cycles.read_enrolled_state_locked(OLD)["v"], 1)
            with self.assertRaises(PreconditionBlocked):
                self.fence.cycles.reserve_locked(OLD, NEXT_CYCLE, ATTEMPT)
            with self.assertRaises(PreconditionBlocked):
                self.fence.cycles.verify_reserved_locked(OLD, OLD_CYCLE, OLD_ATTEMPT)
        self.assertEqual(self.attempt().reason, "PRECONDITION_BLOCKED")
        self.assertEqual(self.port.sent, [])
        self.assertNotIn("rtc", self.events)

        old_target = replace(TARGET, expected_device_id=OLD)

        class OldPort(FakePort):
            def resolve_target(self, mount_root, expected_device_id):
                self.events.append("resolve")
                if (mount_root, expected_device_id) != (old_target.mount_root, OLD):
                    raise AssertionError("wrong synthetic old Device")
                return old_target

            def revalidate_target(self, target, cdc_port):
                self.events.append("revalidate")
                if target != old_target or cdc_port != "COM5":
                    raise AssertionError("stale synthetic old Device")

        old_port = OldPort()
        old_session = VerifiedSession(old_target, "COM5", self.epoch)
        old_seq = SourceOnlyReleaseSequence(
            fence=self.fence,
            coordinator=SyncReleaseCoordinator(old_port),
            session=old_session, sync=lambda: SyncSummary(copied=1),
            current_epoch=lambda: self.epoch,
            cdc_mutations=lambda: old_port.events.append("rtc"),
            before_seal_observation=lambda t, g: True,
        )
        old_candidate = QualifiedCycleCandidate(
            OLD, NEXT_CYCLE, CycleOrigin.NORMAL_WAKE, old_session,
        )
        self.assertEqual(
            old_seq.normal_cycle(
                candidate=old_candidate, verify_current_cycle=lambda c, t: True
            ).reason, "PRECONDITION_BLOCKED"
        )
        self.assertNotIn("rtc", old_port.events)
        self.assertEqual(old_port.sent, [])

    def test_lost_release_reply_consumes_once_no_retry(self):
        self.port.indeterminate = True
        self.assertEqual(self.attempt().reason, "INDETERMINATE")
        self.assertEqual(self.port.sent, [ATTEMPT])
        self.port.indeterminate = False
        self.assertEqual(self.attempt().reason, "PRECONDITION_BLOCKED")
        self.assertEqual(self.port.sent, [ATTEMPT])

    def test_current_status_provider_throws_zero_mutation(self):
        self.assertEqual(self.attempt(current=lambda: (_ for _ in ()).throw(
            RuntimeError("synthetic read failure")
        )).reason, "PRECONDITION_BLOCKED")
        self.assertFalse(self.fence.database.exists())
        self.assertEqual(self.port.sent, [])


if __name__ == "__main__":
    unittest.main()
