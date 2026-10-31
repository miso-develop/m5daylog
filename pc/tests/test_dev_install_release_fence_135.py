"""#147 DEV_FRESH_INSTALL source-only negative/consumption tests; no real IO."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from multiprocessing import get_context
from pathlib import Path

from m5daylog.dev_install_release_fence import DevInstallReceipt, DevInstallReleaseFence
from m5daylog.release import PreconditionBlocked
from m5daylog.release_cycle_fence import CycleAttemptFence

DEV = "00000000-0000-4000-8000-000000000001"
GEN = "00000000-0000-4000-8000-000000000002"
ATTEMPT = "00000000-0000-4000-8000-000000000003"
CYCLE = "00000000-0000-4000-8000-000000000010"
CYCLE_ATTEMPT = "00000000-0000-4000-8000-000000000011"


class FileFakeAnchor:
    """Separate file is synthetic, NOT a Windows Registry durability claim."""
    def __init__(self, path):
        self.path = Path(path)
        self.fail_write = False

    def read(self):
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="ascii"))

    def write(self, state):
        if self.fail_write:
            raise OSError("synthetic anchor interruption")
        with self.path.open("w", encoding="ascii") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())


def receipt():
    return DevInstallReceipt(
        device_id=DEV, generation_id=GEN, first_release_attempt_id=ATTEMPT,
        physical_target_digest="1" * 64, host_lineage_digest="2" * 64,
        image_set_digest="3" * 64, partition_layout_digest="4" * 64,
        nvs_write_set_digest="5" * 64, admission_inventory_digest="6" * 64,
        external_permit_digest="7" * 64, media_branch="R",
    )


def make(root):
    root = Path(root)
    cycles = CycleAttemptFence(
        root / "r3.sqlite", root / "shared.lock", root / "device",
        FileFakeAnchor(root / "r3-anchor.json"),
    )
    return DevInstallReleaseFence(
        cycles=cycles, database=root / "dev.sqlite",
        device_root=root / "device",
        anchor=FileFakeAnchor(root / "dev-anchor.json"),
    )


def worker(root, start, queue):
    fence = make(root)
    start.wait(10)
    try:
        with fence.cycles.serialized():
            fence.consume_locked(receipt(), verify_external=lambda _: True)
        queue.put("CONSUMED")
    except PreconditionBlocked:
        queue.put("BLOCKED")


class DevInstallReleaseFenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "device").mkdir()
        self.fence = make(self.root)

    def consume(self, item=None, checker=lambda _: True):
        with self.fence.cycles.serialized():
            self.fence.consume_locked(item or receipt(), verify_external=checker)

    def test_consumed_persists_across_restart_and_cannot_replay(self):
        self.consume()
        restarted = make(self.root)
        with restarted.cycles.serialized():
            restarted.verify_consumed_locked(receipt())
            with self.assertRaises(PreconditionBlocked):
                restarted.consume_locked(receipt(), verify_external=lambda _: True)
        with closing(sqlite3.connect(self.root / "dev.sqlite")) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            row = str(db.execute("SELECT * FROM dev_consumed").fetchall())
        for value in (DEV, GEN, ATTEMPT):
            self.assertNotIn(value, row)

    def test_requires_shared_lock_and_explicit_external_verifier(self):
        with self.assertRaises(PreconditionBlocked):
            self.fence.consume_locked(receipt(), verify_external=lambda _: True)
        with self.fence.cycles.serialized():
            for checker in (None, lambda _: False):
                with self.assertRaises(PreconditionBlocked):
                    self.fence.consume_locked(receipt(), verify_external=checker)
        self.assertFalse((self.root / "dev.sqlite").exists())

    def test_invalid_receipt_fails_before_writing(self):
        invalid = [
            replace(receipt(), device_id="bad"),
            replace(receipt(), generation_id="AAAAAAAA-0000-4000-8000-000000000002"),
            replace(receipt(), image_set_digest="g" * 64),
            replace(receipt(), media_branch="R-RECOVERY"),
            replace(receipt(), first_release_attempt_id=ATTEMPT + " "),
        ]
        for item in invalid:
            with self.subTest(item=item):
                with self.assertRaises(PreconditionBlocked):
                    self.consume(item)
        self.assertFalse((self.root / "dev.sqlite").exists())

    def test_prior_enrollment_blocks_dev_fresh_h0(self):
        self.fence.cycles.enroll(DEV, supervised=True)
        with self.assertRaises(PreconditionBlocked):
            self.consume()
        self.assertFalse((self.root / "dev.sqlite").exists())

    def test_wrong_generation_host_image_branch_or_attempt_never_matches(self):
        self.consume()
        invalid = [
            replace(receipt(), generation_id=CYCLE),
            replace(receipt(), host_lineage_digest="a" * 64),
            replace(receipt(), image_set_digest="b" * 64),
            replace(receipt(), media_branch="C"),
            replace(receipt(), first_release_attempt_id=CYCLE_ATTEMPT),
        ]
        with self.fence.cycles.serialized():
            for item in invalid:
                with self.subTest(item=item):
                    with self.assertRaises(PreconditionBlocked):
                        self.fence.verify_consumed_locked(item)
                    with self.assertRaises(PreconditionBlocked):
                        self.fence.consume_locked(item, verify_external=lambda _: True)

    def test_sqlite_then_anchor_split_commit_permanently_denied(self):
        self.fence.anchor.fail_write = True
        with self.assertRaises(PreconditionBlocked):
            self.consume()
        self.fence.anchor.fail_write = False
        with self.assertRaises(PreconditionBlocked):
            self.consume()
        with self.fence.cycles.serialized():
            with self.assertRaises(PreconditionBlocked):
                self.fence.verify_consumed_locked(receipt())

    def test_single_store_missing_denied_no_auto_repair(self):
        for missing in ("dev.sqlite", "dev-anchor.json"):
            with self.subTest(missing=missing):
                # Separate clean temporary per variant.
                with tempfile.TemporaryDirectory() as d:
                    root = Path(d)
                    (root / "device").mkdir()
                    fence = make(root)
                    with fence.cycles.serialized():
                        fence.consume_locked(receipt(), verify_external=lambda _: True)
                    (root / missing).unlink()
                    with fence.cycles.serialized():
                        with self.assertRaises(PreconditionBlocked):
                            fence.consume_locked(receipt(), verify_external=lambda _: True)
                        with self.assertRaises(PreconditionBlocked):
                            fence.verify_consumed_locked(receipt())
                    self.assertFalse((root / missing).exists())

    def test_tamper_or_sqlite_recovery_journal_denied(self):
        self.consume()
        with closing(sqlite3.connect(self.root / "dev.sqlite")) as db:
            db.execute("UPDATE dev_consumed SET receipt=?", ("f" * 64,))
            db.commit()
        with self.fence.cycles.serialized():
            with self.assertRaises(PreconditionBlocked):
                self.fence.verify_consumed_locked(receipt())
        with closing(sqlite3.connect(self.root / "dev.sqlite")) as db:
            db.execute("UPDATE dev_consumed SET receipt=?", (receipt().fingerprint(),))
            db.commit()
        (self.root / "dev.sqlite-journal").write_bytes(b"uncertain")
        with self.fence.cycles.serialized():
            with self.assertRaises(PreconditionBlocked):
                self.fence.verify_consumed_locked(receipt())

    def test_dev_journal_not_on_device_or_shared_with_normal_ledger(self):
        for path in (self.root / "device" / "dev.sqlite", self.fence.cycles.database,
                     self.fence.cycles.lock_file):
            with self.assertRaises(PreconditionBlocked):
                DevInstallReleaseFence(
                    cycles=self.fence.cycles, database=path,
                    device_root=self.root / "device",
                    anchor=FileFakeAnchor(self.root / "unused-anchor.json"),
                )

    def test_first_cycle_requires_separate_dev_consumption_and_device_origin(self):
        status = {"state": "USB_SYNC", "rtcCorrectionPending": False,
                  "releaseCycleOrigin": "SUPERVISED_FIRST_USE", "releaseCycleId": CYCLE}
        with self.fence.cycles.serialized():
            with self.assertRaises(PreconditionBlocked):
                self.fence.initialize_first_dev_cycle_locked(
                    receipt(), status=status, cycle_attempt_id=CYCLE_ATTEMPT,
                    verify_first_wake=lambda *args: True)
        self.consume()
        with self.fence.cycles.serialized():
            for broken in (
                {**status, "releaseCycleOrigin": "NORMAL_WAKE"},
                {**status, "releaseCycleId": None},
                {**status, "rtcCorrectionPending": "false"},
            ):
                with self.assertRaises(PreconditionBlocked):
                    self.fence.initialize_first_dev_cycle_locked(
                        receipt(), status=broken, cycle_attempt_id=CYCLE_ATTEMPT,
                        verify_first_wake=lambda *args: True)
            for bad_attempt, check in ((ATTEMPT, lambda *args: True),
                                       (CYCLE_ATTEMPT, lambda *args: False)):
                with self.assertRaises(PreconditionBlocked):
                    self.fence.initialize_first_dev_cycle_locked(
                        receipt(), status=status, cycle_attempt_id=bad_attempt,
                        verify_first_wake=check)
            self.fence.initialize_first_dev_cycle_locked(
                receipt(), status=status, cycle_attempt_id=CYCLE_ATTEMPT,
                verify_first_wake=lambda *args: True)
            self.fence.cycles.verify_reserved_locked(DEV, CYCLE, CYCLE_ATTEMPT)
            with self.assertRaises(PreconditionBlocked):
                self.fence.initialize_first_dev_cycle_locked(
                    receipt(), status=status, cycle_attempt_id=CYCLE_ATTEMPT,
                    verify_first_wake=lambda *args: True)

    def test_receipt_repr_does_not_expose_identifiers(self):
        rendered = repr(receipt())
        for value in (DEV, GEN, ATTEMPT, "1" * 64, "7" * 64):
            self.assertNotIn(value, rendered)

    def test_sqlite_read_only_uri_handles_reserved_host_path(self):
        with tempfile.TemporaryDirectory(prefix="host # and space ") as location:
            root = Path(location)
            (root / "device").mkdir()
            fence = make(root)
            with fence.cycles.serialized():
                fence.consume_locked(receipt(), verify_external=lambda _: True)
                fence.verify_consumed_locked(receipt())

    def test_two_process_consumption_is_at_most_once(self):
        ctx = get_context("spawn")
        start, results = ctx.Event(), ctx.Queue()
        jobs = [ctx.Process(target=worker, args=(str(self.root), start, results))
                for _ in range(2)]
        for job in jobs:
            job.start()
        try:
            start.set()
            outcomes = [results.get(timeout=20) for _ in jobs]
            for job in jobs:
                job.join(timeout=20)
            self.assertEqual([job.exitcode for job in jobs], [0, 0])
            self.assertEqual(sorted(outcomes), ["BLOCKED", "CONSUMED"])
        finally:
            for job in jobs:
                if job.is_alive():
                    job.terminate()
                    job.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
