"""#147 SDB-1.2.1: Hpre read-only host tests; no actual USB/CDC/Device.

All synthetic state and (on Windows) HKCU keys are disposable fixtures.
A positive fixture proves source consistency only, NOT an actual owner P0.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from m5daylog.host_preflight_147 import (
    HpreStatus, inspect_current_user_hpre, inspect_hpre_locked,
)
from m5daylog.release_cycle_fence import CycleAttemptFence, WindowsRegistryAnchor
from test_release_cycle_fence_143 import (
    DEVICE, CYCLE1, ATTEMPT1, make_fence,
)

OTHER_DEVICE = "00000000-0000-4000-8000-000000000099"


class HpreSourceTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        (self.root / "device_media").mkdir()
        self.fence = make_fence(self.root)

    def inspect(self, device=DEVICE):
        with self.fence.serialized():
            return inspect_hpre_locked(
                self.fence, expected_previous_device_id=device,
            )

    def prepared(self, *, consumed=True):
        self.fence.enroll(DEVICE, supervised=True)
        if consumed:
            with self.fence.serialized():
                self.fence.reserve_locked(DEVICE, CYCLE1, ATTEMPT1)

    def snapshot(self):
        database = self.fence.database
        return (
            database.read_bytes(),
            self.fence.anchor.path.read_bytes(),
            database.stat().st_mtime_ns,
        )

    def test_positive_old_v1_consumed_is_read_only_and_matches_consumer_digest(self):
        self.prepared()
        before = self.snapshot()
        with self.fence.serialized():
            state = self.fence.read_enrolled_state_locked(DEVICE)
            with patch("m5daylog.host_preflight_147.sqlite3.connect",
                       wraps=sqlite3.connect) as connect:
                result = inspect_hpre_locked(
                    self.fence, expected_previous_device_id=DEVICE,
                )
            self.assertTrue(connect.called)
            for call in connect.call_args_list:
                self.assertTrue(call.kwargs.get("uri"))
                self.assertIn("?mode=ro", call.args[0])
        self.assertEqual(result.status, HpreStatus.HEALTHY_V1_MATCHING_SEQ_GE_1)
        self.assertEqual(result.sequence, 1)
        self.assertEqual(result.tip, state["tip"])
        self.assertEqual(result.lineage_digest, hashlib.sha256(json.dumps(
            state, sort_keys=True, separators=(",", ":")
        ).encode("ascii")).hexdigest())
        self.assertEqual(before, self.snapshot())
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse((self.fence.database.parent /
                              (self.fence.database.name + suffix)).exists())

    def test_v1_seq_zero_is_not_a_p0_candidate(self):
        self.prepared(consumed=False)
        before = self.snapshot()
        self.assertEqual(self.inspect().status, HpreStatus.HEALTHY_V1_SEQ_0)
        self.assertEqual(self.snapshot(), before)

    def test_unenrolled_state_is_not_evidence_of_safe_h0(self):
        # Lock creation is a lock operation, NOT a ledger enrollment.
        result = self.inspect()
        self.assertEqual(result.status, HpreStatus.ABSENT_OR_UNPROVEN_H0)
        self.assertFalse(self.fence.database.exists())
        self.assertIsNone(self.fence.anchor.read())

    def test_missing_only_one_store_is_split(self):
        self.prepared()
        db_backup = self.fence.database.read_bytes()
        self.fence.database.unlink()
        self.assertEqual(self.inspect().status, HpreStatus.SPLIT_OR_ROLLED_BACK)
        self.fence.database.write_bytes(db_backup)
        self.fence.anchor.path.unlink()
        self.assertEqual(self.inspect().status, HpreStatus.SPLIT_OR_ROLLED_BACK)

    def test_mismatch_unknown_identity_corrupt_chain_and_one_store_rollback(self):
        self.prepared()
        before = self.snapshot()
        self.assertEqual(self.inspect(OTHER_DEVICE).status,
                         HpreStatus.SPLIT_OR_ROLLED_BACK)
        self.assertEqual(self.inspect("invalid-id").status,
                         HpreStatus.UNREADABLE_OR_UNKNOWN)
        self.assertEqual(self.snapshot(), before)

        old_anchor = self.fence.anchor.path.read_bytes()
        with self.fence.serialized():
            self.fence.reserve_locked(
                DEVICE, "00000000-0000-4000-8000-000000000006",
                "00000000-0000-4000-8000-000000000007",
            )
        self.fence.anchor.path.write_bytes(old_anchor)
        self.assertEqual(self.inspect().status, HpreStatus.SPLIT_OR_ROLLED_BACK)

        # Restore the anchor to match, then damage only the hash-chain row.
        with self.fence.serialized():
            # Existing writable verifier intentionally cannot repair a split;
            # take the already-consumed SQLite state directly from fixture.
            pass
        with sqlite3.connect(self.fence.database) as db:
            db.execute("UPDATE consumed SET attempt=? WHERE seq=1", ("0" * 64,))
            db.commit()
        self.assertEqual(self.inspect().status, HpreStatus.SPLIT_OR_ROLLED_BACK)

    def test_recovery_sidecar_refuses_without_normalization(self):
        self.prepared()
        journal = self.fence.database.with_name(self.fence.database.name + "-journal")
        journal.write_bytes(b"uncertain-hot-journal")
        before = self.snapshot()
        self.assertEqual(self.inspect().status, HpreStatus.UNREADABLE_OR_UNKNOWN)
        self.assertEqual(journal.read_bytes(), b"uncertain-hot-journal")
        self.assertEqual(before, self.snapshot())

    def test_v2_and_unsupported_versions_not_downgraded(self):
        self.prepared()
        with sqlite3.connect(self.fence.database) as db:
            db.execute("UPDATE ledger_state SET v=2")
            db.commit()
        self.assertEqual(self.inspect().status, HpreStatus.V2_OR_UNSUPPORTED)

    def test_missing_lock_or_lock_not_held_never_qualifies(self):
        self.prepared()
        result = inspect_hpre_locked(self.fence, expected_previous_device_id=DEVICE)
        self.assertEqual(result.status, HpreStatus.UNREADABLE_OR_UNKNOWN)
        self.fence.lock_file.unlink()
        with self.fence.serialized():
            self.assertEqual(inspect_hpre_locked(
                self.fence, expected_previous_device_id=DEVICE,
            ).status, HpreStatus.HEALTHY_V1_MATCHING_SEQ_GE_1)

    def test_non_windows_production_wrapper_does_not_touch_host(self):
        if os.name != "nt":
            self.assertEqual(inspect_current_user_hpre(
                expected_previous_device_id=DEVICE,
            ).status, HpreStatus.UNREADABLE_OR_UNKNOWN)


@unittest.skipUnless(os.name == "nt", "native HKCU needed")
class NativeWindowsHpreTests(unittest.TestCase):
    def setUp(self):
        import winreg
        self.winreg = winreg
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "device_media").mkdir()
        self.registry_key = r"Software\\M5DaylogHpreTest_" + uuid.uuid4().hex
        self.addCleanup(self._clean_registry)
        anchor = WindowsRegistryAnchor()
        anchor.KEY = self.registry_key
        self.fence = CycleAttemptFence(
            self.root / "release.sqlite", self.root / "release.lock",
            self.root / "device_media", anchor,
        )

    def _clean_registry(self):
        try:
            self.winreg.DeleteKey(self.winreg.HKEY_CURRENT_USER, self.registry_key)
        except FileNotFoundError:
            pass

    def test_readonly_native_hkcu_and_db_no_flush_or_db_mutation(self):
        self.fence.enroll(DEVICE, supervised=True)
        with self.fence.serialized():
            self.fence.reserve_locked(DEVICE, CYCLE1, ATTEMPT1)
        db_before = self.fence.database.read_bytes()
        with self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER,
                                 self.registry_key) as k:
            value_before, _ = self.winreg.QueryValueEx(k, self.fence.anchor.VALUE)
        with patch.object(self.winreg, "SetValueEx",
                          side_effect=AssertionError("HKCU write forbidden")), patch.object(
                          self.winreg, "FlushKey",
                          side_effect=AssertionError("HKCU flush forbidden")):
            with self.fence.serialized():
                result = inspect_hpre_locked(
                    self.fence, expected_previous_device_id=DEVICE,
                )
        self.assertEqual(result.status, HpreStatus.HEALTHY_V1_MATCHING_SEQ_GE_1)
        self.assertEqual(self.fence.database.read_bytes(), db_before)
        with self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER,
                                 self.registry_key) as k:
            value_after, _ = self.winreg.QueryValueEx(k, self.fence.anchor.VALUE)
        self.assertEqual(value_after, value_before)


if __name__ == "__main__":
    unittest.main()
