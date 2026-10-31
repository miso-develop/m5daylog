"""Native Windows-only #135 fence regression; no USB, CDC, or release sender.

All registry keys are per-test disposable HKCU keys. Device and first-use
receipts here are fabricated *test fixtures*, never operational admission.
These tests do not simulate real host power loss or authorize release.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from multiprocessing import get_context
from pathlib import Path
from unittest.mock import patch

from m5daylog.first_use_admission import (
    HostHistory, InitialBootObservation, InitialIntent, MaintenanceReceipt,
    PriorEnrollment,
)
from m5daylog.first_use_transaction import (
    FileFirstUseIntent, consume_first_use_source_only,
)
from m5daylog.release import PreconditionBlocked
from m5daylog.release_cycle_fence import (
    CycleAttemptFence, WindowsRegistryAnchor,
)


DEVICE = "00000000-0000-4000-8000-000000000001"
CYCLE = "00000000-0000-4000-8000-000000000002"
NEXT_CYCLE = "00000000-0000-4000-8000-000000000003"
ATTEMPT = "00000000-0000-4000-8000-000000000004"
NEXT_ATTEMPT = "00000000-0000-4000-8000-000000000005"
MAINTENANCE_ATTEMPT = "00000000-0000-4000-8000-000000000006"


def _fence(root: str | Path, key: str) -> CycleAttemptFence:
    root = Path(root)
    anchor = WindowsRegistryAnchor()
    anchor.KEY = key
    return CycleAttemptFence(
        root / "ledger.sqlite", root / "coordinator.lock",
        root / "fake_device", anchor,
    )


def _reserve_worker(root: str, key: str, cycle: str, attempt: str,
                    start, results) -> None:
    if not start.wait(15):
        results.put("START_TIMEOUT")
        return
    try:
        fence = _fence(root, key)
        with fence.serialized():
            fence.reserve_locked(DEVICE, cycle, attempt)
        results.put("RESERVED")
    except PreconditionBlocked:
        results.put("BLOCKED")
    except Exception:
        results.put("UNEXPECTED_ERROR")


def _hold_worker(root: str, key: str, acquired, release, results) -> None:
    try:
        with _fence(root, key).serialized():
            acquired.set()
            if not release.wait(15):
                results.put("HOLD_TIMEOUT")
                return
        results.put("RELEASED_LOCK")
    except Exception:
        results.put("UNEXPECTED_ERROR")


@unittest.skipUnless(sys.platform == "win32" and os.name == "nt",
                     "actual HKCU and msvcrt locking require Windows")
class NativeWindowsCycleFenceTests(unittest.TestCase):
    def setUp(self) -> None:
        import winreg

        self.winreg = winreg
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "fake_device").mkdir()
        self.key = r"Software\M5DaylogFenceTest_" + uuid.uuid4().hex
        self.addCleanup(self._remove_registry_key)
        self.fence = _fence(self.root, self.key)

    def _remove_registry_key(self) -> None:
        try:
            self.winreg.DeleteKey(self.winreg.HKEY_CURRENT_USER, self.key)
        except FileNotFoundError:
            pass

    def _enroll_fixture(self) -> None:
        # This isolated fixture flag is not a supervised #119 authorization.
        self.fence.enroll(DEVICE, supervised=True)

    def _reserve(self, cycle: str = CYCLE, attempt: str = ATTEMPT) -> None:
        with self.fence.serialized():
            self.fence.reserve_locked(DEVICE, cycle, attempt)

    def _assert_blocked(self, cycle: str = NEXT_CYCLE,
                        attempt: str = NEXT_ATTEMPT) -> None:
        with self.assertRaises(PreconditionBlocked):
            self._reserve(cycle, attempt)

    def test_native_hkcu_flush_sqlite_commit_readback_and_replay(self):
        import winreg

        with patch.object(winreg, "FlushKey", wraps=winreg.FlushKey) as flush:
            self._enroll_fixture()
            self._reserve()
            self.assertGreaterEqual(flush.call_count, 2)

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key) as key:
            raw, kind = winreg.QueryValueEx(key, self.fence.anchor.VALUE)
        self.assertEqual(kind, winreg.REG_SZ)
        self.assertEqual(json.loads(raw), self.fence.anchor.read())
        with sqlite3.connect(self.fence.database) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM consumed").fetchone(), (1,))
        restarted = _fence(self.root, self.key)
        with restarted.serialized():
            restarted.verify_reserved_locked(DEVICE, CYCLE, ATTEMPT)
            with self.assertRaises(PreconditionBlocked):
                restarted.reserve_locked(DEVICE, CYCLE, NEXT_ATTEMPT)

    def test_two_real_processes_only_one_consumes_cycle(self):
        self._enroll_fixture()
        ctx = get_context("spawn")
        start, results = ctx.Event(), ctx.Queue()
        children = [
            ctx.Process(target=_reserve_worker,
                        args=(str(self.root), self.key, CYCLE, attempt, start, results))
            for attempt in (ATTEMPT, NEXT_ATTEMPT)
        ]
        try:
            for child in children:
                child.start()
            start.set()
            values = [results.get(timeout=25) for _ in children]
            for child in children:
                child.join(timeout=15)
            self.assertEqual(sorted(values), ["BLOCKED", "RESERVED"])
            self.assertTrue(all(child.exitcode == 0 for child in children))
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
        self._assert_blocked(CYCLE, NEXT_ATTEMPT)

    def test_windows_lock_is_exclusive_while_other_process_holds_it(self):
        ctx = get_context("spawn")
        acquired, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
        child = ctx.Process(target=_hold_worker,
                            args=(str(self.root), self.key, acquired, release, results))
        try:
            child.start()
            self.assertTrue(acquired.wait(20), "child never acquired OS lock")
            with self.assertRaises(PreconditionBlocked):
                with self.fence.serialized():
                    pass
            release.set()
            self.assertEqual(results.get(timeout=15), "RELEASED_LOCK")
            child.join(timeout=15)
            self.assertEqual(child.exitcode, 0)
        finally:
            release.set()
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
        with self.fence.serialized():
            self.fence.check_unenrolled_locked()

    def test_missing_and_malformed_real_registry_anchor_block(self):
        import winreg

        self._enroll_fixture()
        self._reserve()
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.key)
        self._assert_blocked()
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.key) as key:
            winreg.SetValueEx(key, self.fence.anchor.VALUE, 0, winreg.REG_SZ,
                              "{malformed")
            winreg.FlushKey(key)
        self._assert_blocked()

    def test_single_store_db_rollback_blocks_without_repair(self):
        self._enroll_fixture()
        backup = self.root / "saved-ledger"
        shutil.copyfile(self.fence.database, backup)
        self._reserve()
        shutil.copyfile(backup, self.fence.database)
        self._assert_blocked()

    def test_single_store_registry_rollback_blocks_without_repair(self):
        self._enroll_fixture()
        old_state = self.fence.anchor.read()
        self._reserve()
        self.fence.anchor.write(old_state)
        self._assert_blocked()

    def test_sqlite_commit_then_registry_failure_remains_consumed(self):
        self._enroll_fixture()
        with patch.object(self.fence.anchor, "write", side_effect=OSError("injected")):
            with self.assertRaises(PreconditionBlocked):
                self._reserve()
        with sqlite3.connect(self.fence.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM consumed").fetchone(), (1,))
        self._assert_blocked()
        self._assert_blocked(CYCLE, ATTEMPT)

    def _first_use_fixture(self):
        history = HostHistory(PriorEnrollment.VERIFIED_NEVER_ENROLLED,
                              False, False, True)
        intent = InitialIntent(
            DEVICE, "physical-parent-before", "maintenance-session",
            MAINTENANCE_ATTEMPT, exact_human_permit_verified=True,
            device_physical_stage_verified=True, running_handler_verified=True,
            original_media_preserved=True,
        )
        maintenance = MaintenanceReceipt(
            DEVICE, "physical-parent-before", "maintenance-session",
            MAINTENANCE_ATTEMPT, accepted_response_verified=True,
            exact_target_disappeared=True, device_shutdown_armed_verified=True,
        )
        boot = InitialBootObservation(
            DEVICE, "physical-parent-after", "new-session",
            cross_session_custody_verified=True,
            first_boot_transition_verified=True,
            current_run_terminal_pcm_verified=True,
            raw_wav_sync_durable=True, rtc_validity_verified=True,
            exact_target_verified=True,
        )
        status = {
            "state": "USB_SYNC", "rtcCorrectionPending": False,
            "releaseCycleId": CYCLE, "releaseCycleOrigin": "SUPERVISED_FIRST_USE",
        }
        path = self.root / "approved-intent.json"
        file = FileFirstUseIntent(path, device_root=self.root / "fake_device")
        # Synthetic receipt fixture; product does not issue first-use intents.
        file.path.write_text(
            json.dumps(file.expected(intent, state="UNCONSUMED")), encoding="ascii",
        )
        return file, dict(
            status=status, history=history, intent=intent,
            maintenance=maintenance, boot=boot, release_attempt_id=ATTEMPT,
        )

    def test_windows_first_use_consumes_intent_and_prevents_reenrollment(self):
        file, facts = self._first_use_fixture()
        self.assertIsNone(consume_first_use_source_only(
            fence=self.fence, intent_file=file, **facts,
        ))
        self.assertEqual(file.read()["state"], "CONSUMED")
        restarted = _fence(self.root, self.key)
        with restarted.serialized():
            restarted.verify_reserved_locked(DEVICE, CYCLE, ATTEMPT)
        with self.assertRaises(PreconditionBlocked):
            consume_first_use_source_only(fence=_fence(self.root, self.key),
                                          intent_file=file, **facts)

    def test_partial_first_use_intent_blocks_without_enrollment(self):
        file, facts = self._first_use_fixture()
        file.pending.write_bytes(b"partial")
        with self.assertRaises(PreconditionBlocked):
            consume_first_use_source_only(fence=self.fence,
                                          intent_file=file, **facts)
        self.assertFalse(self.fence.database.exists())
        self.assertIsNone(self.fence.anchor.read())

    def test_windows_first_use_split_commit_never_recovers_itself(self):
        file, facts = self._first_use_fixture()
        with patch.object(self.fence.anchor, "write", side_effect=OSError("injected")):
            with self.assertRaises(PreconditionBlocked):
                consume_first_use_source_only(fence=self.fence,
                                              intent_file=file, **facts)
        self.assertEqual(file.read()["state"], "CONSUMED")
        self.assertTrue(self.fence.database.is_file())
        self.assertIsNone(self.fence.anchor.read())
        with self.assertRaises(PreconditionBlocked):
            consume_first_use_source_only(fence=_fence(self.root, self.key),
                                          intent_file=file, **facts)


if __name__ == "__main__":
    unittest.main()
