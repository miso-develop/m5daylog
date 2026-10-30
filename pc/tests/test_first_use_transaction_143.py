"""#143 R5-FU synthetic host transaction fault matrix, no Device/CDC operations."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from multiprocessing import get_context
from pathlib import Path

from m5daylog.first_use_admission import (
    HostHistory, InitialBootObservation, InitialIntent, MaintenanceReceipt,
    PriorEnrollment,
)
from m5daylog.first_use_transaction import (
    FileFirstUseIntent, consume_first_use_source_only,
)
from m5daylog.release import PreconditionBlocked
from m5daylog.release_cycle_fence import CycleAttemptFence


DEVICE = "00000000-0000-4000-8000-000000000001"
ATTEMPT = "00000000-0000-4000-8000-000000000002"
FIRST_ATTEMPT = "00000000-0000-4000-8000-000000000003"
SECOND_ATTEMPT = "00000000-0000-4000-8000-000000000004"
CYCLE = "00000000-0000-4000-8000-000000000005"
CYCLE_2 = "00000000-0000-4000-8000-000000000006"


class FileAnchor:
    """A separate synthetic host witness, NOT the Windows HKCU implementation."""

    def __init__(self, path):
        self.path = Path(path)
        self.fail = False

    def read(self):
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="ascii"))

    def write(self, state):
        if self.fail:
            raise OSError("synthetic anchor failure")
        with self.path.open("w", encoding="ascii") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())


def make_fence(root):
    root = Path(root)
    return CycleAttemptFence(
        root / "state" / "ledger.sqlite", root / "state" / "global.lock",
        root / "device_media", FileAnchor(root / "other" / "anchor.json"),
    )


def facts():
    history = HostHistory(
        PriorEnrollment.VERIFIED_NEVER_ENROLLED, False, False, True
    )
    intent = InitialIntent(
        DEVICE, "parent-before-release", "original-usb-session", ATTEMPT,
        exact_human_permit_verified=True, device_physical_stage_verified=True,
        running_handler_verified=True, original_media_preserved=True,
    )
    maintenance = MaintenanceReceipt(
        DEVICE, "parent-before-release", "original-usb-session", ATTEMPT,
        accepted_response_verified=True, exact_target_disappeared=True,
        device_shutdown_armed_verified=True,
    )
    boot = InitialBootObservation(
        DEVICE, "parent-after-wake", "new-usb-session",
        cross_session_custody_verified=True,
        first_boot_transition_verified=True,
        current_run_terminal_pcm_verified=True,
        raw_wav_sync_durable=True,
        rtc_validity_verified=True,
        exact_target_verified=True,
    )
    status = {
        "state": "USB_SYNC", "rtcCorrectionPending": False,
        "releaseCycleId": CYCLE,
        "releaseCycleOrigin": "SUPERVISED_FIRST_USE",
    }
    return dict(status=status, history=history, intent=intent,
                maintenance=maintenance, boot=boot,
                release_attempt_id=FIRST_ATTEMPT)


def prepare(root):
    root = Path(root)
    (root / "device_media").mkdir(exist_ok=True)
    (root / "other").mkdir(exist_ok=True)
    path = root / "state" / "approved-intent.json"
    path.parent.mkdir(exist_ok=True)
    stored = FileFirstUseIntent(path, device_root=root / "device_media")
    # Fixture issuance only. Product code deliberately cannot create this.
    path.write_text(json.dumps(stored.expected(facts()["intent"],
                                               state="UNCONSUMED")),
                    encoding="utf-8")
    return stored


def run_model(root, **changes):
    arguments = facts()
    arguments.update(changes)
    return consume_first_use_source_only(
        fence=make_fence(root),
        intent_file=FileFirstUseIntent(
            Path(root) / "state" / "approved-intent.json",
            device_root=Path(root) / "device_media",
        ), **arguments,
    )


def race_worker(root, start, result, attempt):
    start.wait(timeout=10)
    try:
        run_model(root, release_attempt_id=attempt)
        result.put("COMMITTED")
    except PreconditionBlocked:
        result.put("BLOCKED")


class FirstUseTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = prepare(self.root)

    def test_first_cycle_is_initially_consumed_and_reread_both_witnesses(self):
        self.assertIsNone(run_model(self.root))
        self.assertEqual(self.store.read()["state"], "CONSUMED")
        fence = make_fence(self.root)
        with fence.serialized():
            fence.verify_reserved_locked(DEVICE, CYCLE, FIRST_ATTEMPT)
            with self.assertRaises(PreconditionBlocked):
                fence.check_unenrolled_locked()
            with self.assertRaises(PreconditionBlocked):
                fence.reserve_locked(DEVICE, CYCLE, SECOND_ATTEMPT)
            # Prove schema remains compatible with separate ordinary R3
            # reservations; this is not a real subsequent Device boot.
            fence.reserve_locked(DEVICE, CYCLE_2, SECOND_ATTEMPT)
        with sqlite3.connect(fence.database) as db:
            rows = db.execute("SELECT seq, cycle, attempt FROM consumed ORDER BY seq").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row[0] for row in rows], [1, 2])
        self.assertNotIn(DEVICE, repr(rows))
        self.assertNotIn(ATTEMPT, repr(rows))

    def test_no_reenrollment_even_with_new_attempt_or_new_host_model(self):
        run_model(self.root)
        for changed in ({}, {"release_attempt_id": SECOND_ATTEMPT}):
            with self.subTest(changed=changed):
                with self.assertRaises(PreconditionBlocked):
                    run_model(self.root, **changed)

    def test_intent_must_be_independently_present_before_enrollment(self):
        self.store.path.unlink()
        with self.assertRaises(PreconditionBlocked):
            run_model(self.root)
        self.assertFalse(make_fence(self.root).database.exists())

    def test_unqualified_history_or_receipt_never_burns_intent(self):
        base = facts()
        invalid = [
            {"history": replace(base["history"], independently_verified_never_enrolled=False)},
            {"history": replace(base["history"], prior=PriorEnrollment.ALREADY_ENROLLED)},
            {"intent": replace(base["intent"], exact_human_permit_verified=False)},
            {"maintenance": replace(base["maintenance"], maintenance_attempt_id=SECOND_ATTEMPT)},
            {"boot": replace(base["boot"], current_run_terminal_pcm_verified=False)},
            {"status": {**base["status"], "releaseCycleOrigin": "NORMAL_WAKE"}},
            {"status": {**base["status"], "releaseCycleOrigin": "UNKNOWN"}},
            {"status": {**base["status"], "releaseCycleId": None}},
            {"release_attempt_id": ATTEMPT},
        ]
        for changed in invalid:
            with self.subTest(changed=changed):
                with self.assertRaises(PreconditionBlocked):
                    run_model(self.root, **changed)
                self.assertEqual(self.store.read()["state"], "UNCONSUMED")
                self.assertFalse(make_fence(self.root).database.exists())

    def test_previous_or_partial_enrollment_blocks_even_with_positive_history(self):
        fence = make_fence(self.root)
        fence.enroll(DEVICE, supervised=True)
        with self.assertRaises(PreconditionBlocked):
            run_model(self.root)
        self.assertEqual(self.store.read()["state"], "UNCONSUMED")
        # Also block anchor-only witness after a broken prior transaction.
        fence.database.unlink()
        with self.assertRaises(PreconditionBlocked):
            run_model(self.root)

    def test_intent_mismatch_and_reused_receipt_fail_closed(self):
        raw = dict(self.store.read())
        raw["maintenance_attempt"] = "0" * 64
        self.store.path.write_text(json.dumps(raw), encoding="ascii")
        with self.assertRaises(PreconditionBlocked):
            run_model(self.root)
        self.assertFalse(make_fence(self.root).database.exists())
        self.store.path.write_text(
            json.dumps(self.store.expected(facts()["intent"], state="CONSUMED")),
            encoding="ascii",
        )
        with self.assertRaises(PreconditionBlocked):
            run_model(self.root)

    def test_interrupted_intent_temp_file_is_not_auto_repaired(self):
        self.store.pending.write_bytes(b"interrupted")
        with self.assertRaises(PreconditionBlocked):
            run_model(self.root)
        self.assertFalse(make_fence(self.root).database.exists())

    def test_consumed_intent_with_missing_ledger_blocks_restart(self):
        class CrashAfterBurn(FileFirstUseIntent):
            def consume(self, intent):
                super().consume(intent)
                raise RuntimeError("simulated abrupt process exit")
        with self.assertRaises(RuntimeError):
            consume_first_use_source_only(
                fence=make_fence(self.root),
                intent_file=CrashAfterBurn(
                    self.store.path, device_root=self.root / "device_media"
                ), **facts(),
            )
        self.assertEqual(self.store.read()["state"], "CONSUMED")
        self.assertFalse(make_fence(self.root).database.exists())
        with self.assertRaises(PreconditionBlocked):
            run_model(self.root)

    def test_split_sqlite_anchor_commit_blocks_restart(self):
        fence = make_fence(self.root)
        fence.anchor.fail = True
        with self.assertRaises(PreconditionBlocked):
            consume_first_use_source_only(
                fence=fence, intent_file=self.store, **facts(),
            )
        self.assertTrue(fence.database.exists())
        self.assertEqual(self.store.read()["state"], "CONSUMED")
        with self.assertRaises(PreconditionBlocked):
            run_model(self.root)

    def test_after_both_witnesses_before_return_does_not_reenroll(self):
        class CrashAfterCommit(CycleAttemptFence):
            def initialize_first_cycle_locked(self, *args):
                super().initialize_first_cycle_locked(*args)
                raise RuntimeError("synthetic crash after anchor verification")
        regular = make_fence(self.root)
        crash = CrashAfterCommit(regular.database, regular.lock_file,
                                 self.root / "device_media", regular.anchor)
        with self.assertRaises(RuntimeError):
            consume_first_use_source_only(
                fence=crash, intent_file=self.store, **facts(),
            )
        with regular.serialized():
            regular.verify_reserved_locked(DEVICE, CYCLE, FIRST_ATTEMPT)
        with self.assertRaises(PreconditionBlocked):
            run_model(self.root)

    def test_db_only_and_anchor_only_rollback_detected_after_cycle(self):
        run_model(self.root)
        fence = make_fence(self.root)
        db_backup = self.root / "older-sqlite"
        anchor_backup = self.root / "older-anchor"
        shutil.copyfile(fence.database, db_backup)
        shutil.copyfile(fence.anchor.path, anchor_backup)
        with fence.serialized():
            fence.reserve_locked(DEVICE, CYCLE_2, SECOND_ATTEMPT)
        shutil.copyfile(db_backup, fence.database)
        with fence.serialized():
            with self.assertRaises(PreconditionBlocked):
                fence.check_locked(DEVICE)
        # Restore current DB so that the anchor-alone rewind is independent.
        with sqlite3.connect(fence.database) as db:
            # Current anchor still corresponds to seq 2; this is already a STOP.
            self.assertEqual(db.execute("SELECT count(*) FROM consumed").fetchone(), (1,))
        self.assertEqual(self.store.read()["state"], "CONSUMED")

    def test_two_independent_processes_only_one_initializes(self):
        ctx = get_context("spawn")
        start, outcomes = ctx.Event(), ctx.Queue()
        procs = [
            ctx.Process(target=race_worker,
                        args=(self.root, start, outcomes, FIRST_ATTEMPT)),
            ctx.Process(target=race_worker,
                        args=(self.root, start, outcomes, SECOND_ATTEMPT)),
        ]
        for proc in procs:
            proc.start()
        try:
            start.set()
            values = [outcomes.get(timeout=20) for _ in procs]
            for proc in procs:
                proc.join(timeout=20)
            self.assertEqual([proc.exitcode for proc in procs], [0, 0])
            self.assertEqual(sorted(values), ["BLOCKED", "COMMITTED"])
            self.assertEqual(self.store.read()["state"], "CONSUMED")
        finally:
            for proc in procs:
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
