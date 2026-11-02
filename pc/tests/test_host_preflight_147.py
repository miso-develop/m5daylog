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
from contextlib import closing
import uuid
from pathlib import Path
from unittest.mock import patch

from m5daylog.dev_install_release_fence import DevInstallReceipt, DevInstallReleaseFence
from m5daylog.host_preflight_147 import (
    HpreStatus, inspect_current_user_hpre, inspect_hpre_locked,
)
from m5daylog.release_cycle_fence import CycleAttemptFence, WindowsRegistryAnchor
from test_release_cycle_fence_143 import (
    DEVICE, CYCLE1, ATTEMPT1, FileFakeAnchor, make_fence,
)

OTHER_DEVICE = "00000000-0000-4000-8000-000000000099"


class HpreSourceTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        (self.root / "device_media").mkdir()
        self.fence = make_fence(self.root)
        self.dev_database = self.root / "dev_install_release_v1.sqlite"
        self.dev_anchor = FileFakeAnchor(self.root / "dev-anchor.json")
        # Mirror the production for_current_user() DEV cutover binding.
        self.fence.bind_dev_cutover_witness(self.dev_database, self.dev_anchor)

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

    def test_unbound_direct_fence_cannot_be_a_p0_candidate(self):
        self.prepared()
        # Direct helpers must not bypass the canonical DEV witness requirement.
        unbound = make_fence(self.root)
        with unbound.serialized():
            result = inspect_hpre_locked(
                unbound, expected_previous_device_id=DEVICE,
            )
        self.assertEqual(result.status, HpreStatus.UNREADABLE_OR_UNKNOWN)
        self.assertIsNone(result.lineage_digest)

    def test_er_135_hpre_02_dangling_alias_before_binding_is_nonpositive(self):
        # The original failing condition: Path.resolve() hides a dangling
        # symlink to an absent DEV store before Hpre checks is_symlink().
        self.prepared()
        before = self.snapshot()
        missing_target = self.root / "previous-dev-store-missing.sqlite"
        try:
            self.dev_database.symlink_to(missing_target)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"host cannot create disposable symlink: {type(error).__name__}")
        rebound = make_fence(self.root)
        rebound.bind_dev_cutover_witness(self.dev_database, self.dev_anchor)
        self.assertEqual(rebound._dev_cutover_database, missing_target)
        self.assertEqual(rebound._dev_cutover_source_path, self.dev_database)
        with rebound.serialized():
            with patch("m5daylog.host_preflight_147.sqlite3.connect",
                       side_effect=AssertionError("aliased DEV must deny before SQLite")):
                result = inspect_hpre_locked(
                    rebound, expected_previous_device_id=DEVICE,
                )
        self.assertEqual(result.status, HpreStatus.UNREADABLE_OR_UNKNOWN)
        self.assertIsNone(result.lineage_digest)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.dev_database.readlink(), missing_target)
        self.assertFalse(missing_target.exists())
        self.assertIsNone(self.dev_anchor.read())

    def test_er_135_hpre_02_alias_check_is_platform_independent(self):
        # This exercises production Hpre denial on Windows CI even when
        # creating real NTFS symlinks requires privileges.
        self.prepared()
        before = self.snapshot()
        real_is_symlink = Path.is_symlink
        def simulated_alias(candidate):
            return candidate == self.dev_database or real_is_symlink(candidate)
        with patch.object(Path, "is_symlink", simulated_alias):
            with patch("m5daylog.host_preflight_147.sqlite3.connect",
                       side_effect=AssertionError("aliased DEV must deny before SQLite")):
                result = self.inspect()
        self.assertEqual(result.status, HpreStatus.UNREADABLE_OR_UNKNOWN)
        self.assertIsNone(result.lineage_digest)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.dev_database.exists())
        self.assertIsNone(self.dev_anchor.read())

    def test_er_135_hpre_02_parent_alias_before_binding_is_nonpositive(self):
        self.prepared()
        before = self.snapshot()
        parent_alias = self.root / "dev-parent-alias"
        missing_parent = self.root / "old-dev-root-missing"
        try:
            parent_alias.symlink_to(missing_parent, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"host cannot create disposable symlink: {type(error).__name__}")
        source = parent_alias / "dev_install_release_v1.sqlite"
        rebound = make_fence(self.root)
        rebound.bind_dev_cutover_witness(source, self.dev_anchor)
        with rebound.serialized():
            with patch("m5daylog.host_preflight_147.sqlite3.connect",
                       side_effect=AssertionError("parent alias must deny before SQLite")):
                result = inspect_hpre_locked(
                    rebound, expected_previous_device_id=DEVICE,
                )
        self.assertEqual(result.status, HpreStatus.UNREADABLE_OR_UNKNOWN)
        self.assertIsNone(result.lineage_digest)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(parent_alias.readlink(), missing_parent)
        self.assertFalse(missing_parent.exists())

    def test_spent_dev_first_use_and_valid_v1_never_qualifies_p0(self):
        # Reproduce SEC-135-HPRE-01 using the actual source-only DEV first-use
        # consumption + R3 initial-cycle APIs (synthetic verifier, no Device).
        receipt = DevInstallReceipt(
            device_id=DEVICE,
            generation_id="00000000-0000-4000-8000-000000000021",
            first_release_attempt_id=ATTEMPT1,
            physical_target_digest="1" * 64,
            host_lineage_digest="2" * 64,
            image_set_digest="3" * 64,
            partition_layout_digest="4" * 64,
            nvs_write_set_digest="5" * 64,
            admission_inventory_digest="6" * 64,
            external_permit_digest="7" * 64,
            media_branch="R",
        )
        dev = DevInstallReleaseFence(
            cycles=self.fence, database=self.dev_database,
            device_root=self.root / "device_media", anchor=self.dev_anchor,
        )
        with self.fence.serialized():
            dev.consume_locked(receipt, verify_external=lambda _: True)
            dev.initialize_first_dev_cycle_locked(
                receipt,
                status={"releaseCycleId": CYCLE1,
                        "releaseCycleOrigin": "SUPERVISED_FIRST_USE",
                        "rtcCorrectionPending": False, "state": "USB_SYNC"},
                cycle_attempt_id="00000000-0000-4000-8000-000000000022",
                verify_first_wake=lambda *_: True,
            )
        # Both independent stores of each ledger now match, with R3 V1 seq=1,
        # yet the next SAME_HOST DEV first use cannot consume the spent DEV.
        with self.fence.serialized():
            self.assertEqual(self.fence.read_enrolled_state_locked(DEVICE)["seq"], 1)
            dev.verify_consumed_locked(receipt)
        before = (self.snapshot(), self.dev_database.read_bytes(),
                  self.dev_anchor.path.read_bytes())
        with patch("m5daylog.host_preflight_147.sqlite3.connect",
                   side_effect=AssertionError("spent DEV must deny before SQLite")):
            result = self.inspect()
        self.assertEqual(result.status, HpreStatus.UNREADABLE_OR_UNKNOWN)
        self.assertIsNone(result.lineage_digest)
        self.assertEqual(before, (self.snapshot(), self.dev_database.read_bytes(),
                                  self.dev_anchor.path.read_bytes()))

    def test_partial_dev_witness_and_recovery_sidecar_block_read_only(self):
        self.prepared()
        baseline = self.snapshot()
        sidecar = self.dev_database.with_name(self.dev_database.name + "-journal")
        for case in ("dev_db_only", "dev_anchor_only", "sidecar_only"):
            with self.subTest(case=case):
                if case == "dev_db_only":
                    self.dev_database.write_bytes(b"synthetic-consumed")
                elif case == "dev_anchor_only":
                    self.dev_anchor.write({"state": "CONSUMED"})
                else:
                    sidecar.write_bytes(b"uncertain-recovery")
                before = (self.snapshot(),
                          self.dev_database.read_bytes() if self.dev_database.exists() else None,
                          self.dev_anchor.path.read_bytes() if self.dev_anchor.path.exists() else None,
                          sidecar.read_bytes() if sidecar.exists() else None)
                with patch("m5daylog.host_preflight_147.sqlite3.connect",
                           side_effect=AssertionError("ambiguous DEV must deny before SQLite")):
                    result = self.inspect()
                self.assertEqual(result.status, HpreStatus.UNREADABLE_OR_UNKNOWN)
                self.assertIsNone(result.lineage_digest)
                self.assertEqual(before, (
                    self.snapshot(),
                    self.dev_database.read_bytes() if self.dev_database.exists() else None,
                    self.dev_anchor.path.read_bytes() if self.dev_anchor.path.exists() else None,
                    sidecar.read_bytes() if sidecar.exists() else None,
                ))
                self.dev_database.unlink(missing_ok=True)
                self.dev_anchor.path.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
        self.assertEqual(self.snapshot(), baseline)
        self.assertEqual(self.inspect().status, HpreStatus.HEALTHY_V1_MATCHING_SEQ_GE_1)

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

        # Chain corruption remains blocked independently of split anchor.
        with closing(sqlite3.connect(self.fence.database)) as db:
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
        with closing(sqlite3.connect(self.fence.database)) as db:
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
        self.registry_key = "Software" + chr(92) + "M5DaylogHpreTest_" + uuid.uuid4().hex
        self.addCleanup(self._clean_registry)
        anchor = WindowsRegistryAnchor()
        anchor.KEY = self.registry_key
        self.fence = CycleAttemptFence(
            self.root / "release.sqlite", self.root / "release.lock",
            self.root / "device_media", anchor,
        )
        self.dev_registry_key = self.registry_key + "_Dev"
        dev_anchor = WindowsRegistryAnchor()
        dev_anchor.KEY = self.dev_registry_key
        self.fence.bind_dev_cutover_witness(self.root / "dev.sqlite", dev_anchor)

    def _clean_registry(self):
        for key in (self.registry_key, self.dev_registry_key):
            try:
                self.winreg.DeleteKey(self.winreg.HKEY_CURRENT_USER, key)
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
