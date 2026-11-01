"""SDB-1.1 enrolled-host append-only DEV generation, synthetic persistence only."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from m5daylog.release import PreconditionBlocked
from m5daylog.release_cycle_fence import CycleAttemptFence
from test_dev_install_release_fence_135 import DEV, GEN, CYCLE, FileFakeAnchor

OLD = "00000000-0000-4000-8000-000000000091"
OLD_CYCLE = "00000000-0000-4000-8000-000000000092"
OLD_ATTEMPT = "00000000-0000-4000-8000-000000000093"
FIRST_ATTEMPT = "00000000-0000-4000-8000-000000000094"
LATER_CYCLE = "00000000-0000-4000-8000-000000000095"
LATER_ATTEMPT = "00000000-0000-4000-8000-000000000096"
GRANT = "a" * 64


def digest(state):
    return hashlib.sha256(json.dumps(
        state, sort_keys=True, separators=(",", ":")
    ).encode("ascii")).hexdigest()


class GenerationAppendTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "device").mkdir()
        # Isolated R3 hash-chain primitive; coupled DEV-store admission is
        # exercised via SourceOnlyReleaseSequence in test_dev_generation_release.
        self.fence = CycleAttemptFence(
            self.root / "r3.sqlite", self.root / "shared.lock",
            self.root / "device", FileFakeAnchor(self.root / "r3-anchor.json"),
        )
        with self.fence.serialized():
            self.fence.initialize_first_cycle_locked(
                OLD, OLD_CYCLE, OLD_ATTEMPT
            )
            self.prior = self.fence.read_enrolled_state_locked(OLD)
        with closing(sqlite3.connect(self.fence.database)) as db:
            self.old_row = db.execute(
                "SELECT * FROM consumed WHERE seq=1"
            ).fetchone()

    def append(self, *, prior=None, grant=GRANT):
        with self.fence.serialized():
            self.fence.append_dev_generation_first_cycle_locked(
                previous_device_id=OLD, new_device_id=DEV,
                generation_id=GEN, grant_digest=grant,
                prior_state_digest=digest(prior or self.prior),
                release_cycle_id=CYCLE, attempt_id=FIRST_ATTEMPT,
            )

    def test_healthy_enrolled_host_appends_once_preserving_prior_rows(self):
        self.append()
        with self.fence.serialized():
            self.fence.verify_dev_generation_locked(
                DEV, GEN, GRANT, CYCLE, FIRST_ATTEMPT
            )
            with self.assertRaises(PreconditionBlocked):
                self.fence.check_locked(OLD)
            self.fence.reserve_locked(DEV, LATER_CYCLE, LATER_ATTEMPT)
            self.fence.verify_reserved_locked(DEV, LATER_CYCLE, LATER_ATTEMPT)
        with closing(sqlite3.connect(self.fence.database)) as db:
            self.assertEqual(db.execute(
                "SELECT * FROM consumed WHERE seq=1"
            ).fetchone(), self.old_row)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM generation_transition"
            ).fetchone(), (1,))
            self.assertEqual(db.execute(
                "SELECT v, seq FROM ledger_state"
            ).fetchone(), (2, 3))
        with self.assertRaises(PreconditionBlocked):
            self.append()
        with self.fence.serialized():
            with self.assertRaises(PreconditionBlocked):
                self.fence.reserve_locked(DEV, CYCLE, FIRST_ATTEMPT)

    def test_same_device_identity_rejected_before_r3_generation_write(self):
        with self.fence.serialized():
            with self.assertRaises(PreconditionBlocked):
                self.fence.append_dev_generation_first_cycle_locked(
                    previous_device_id=OLD, new_device_id=OLD,
                    generation_id=GEN, grant_digest=GRANT,
                    prior_state_digest=digest(self.prior),
                    release_cycle_id=CYCLE, attempt_id=FIRST_ATTEMPT,
                )
            self.assertEqual(self.fence.read_enrolled_state_locked(OLD), self.prior)
        with closing(sqlite3.connect(self.fence.database)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM consumed"
            ).fetchone(), (1,))
            self.assertFalse(db.execute(
                "SELECT name FROM sqlite_master WHERE name='generation_transition'"
            ).fetchall())

    def test_wrong_prior_receipt_or_grant_cannot_advance_generation(self):
        with self.assertRaises(PreconditionBlocked):
            self.append(prior={"v": 1, "seq": 999})
        with self.fence.serialized():
            self.assertEqual(self.fence.read_enrolled_state_locked(OLD), self.prior)
        self.append()
        with self.fence.serialized():
            with self.assertRaises(PreconditionBlocked):
                self.fence.verify_dev_generation_locked(
                    DEV, GEN, "b" * 64, CYCLE, FIRST_ATTEMPT
                )
            with self.assertRaises(PreconditionBlocked):
                self.fence.verify_dev_generation_locked(
                    DEV, GEN, GRANT, CYCLE, LATER_ATTEMPT
                )

    def test_one_sided_anchor_rollback_before_append_blocks_without_write(self):
        self.fence.anchor.write(dict(self.prior, tip="f" * 64))
        with self.assertRaises(PreconditionBlocked):
            self.append()
        with closing(sqlite3.connect(self.fence.database)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM consumed"
            ).fetchone(), (1,))
        self.assertFalse((self.root / "r3.sqlite-journal").exists())

    def test_sqlite_commit_then_failed_anchor_is_spent_never_repaired(self):
        self.fence.anchor.fail_write = True
        with self.assertRaises(PreconditionBlocked):
            self.append()
        self.fence.anchor.fail_write = False
        with self.assertRaises(PreconditionBlocked):
            self.append()
        with self.fence.serialized():
            with self.assertRaises(PreconditionBlocked):
                self.fence.check_locked(DEV)
        with closing(sqlite3.connect(self.fence.database)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM consumed"
            ).fetchone(), (2,))

    def test_missing_witness_or_second_generation_is_denied(self):
        self.append()
        (self.root / "r3-anchor.json").unlink()
        with self.fence.serialized():
            with self.assertRaises(PreconditionBlocked):
                self.fence.check_locked(DEV)
            with self.assertRaises(PreconditionBlocked):
                self.fence.append_dev_generation_first_cycle_locked(
                    previous_device_id=OLD, new_device_id=DEV,
                    generation_id=GEN, grant_digest=GRANT,
                    prior_state_digest=digest(self.prior),
                    release_cycle_id=CYCLE, attempt_id=FIRST_ATTEMPT,
                )


if __name__ == "__main__":
    unittest.main()
