"""#143 R3: synthetic host dual-witness failure tests; no real CDC/USB access."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from multiprocessing import get_context
from contextlib import closing
from pathlib import Path

from m5daylog.release import PreconditionBlocked
from m5daylog.release_cycle_fence import CycleAttemptFence


DEVICE = "00000000-0000-4000-8000-000000000001"
CYCLE1 = "00000000-0000-4000-8000-000000000002"
CYCLE2 = "00000000-0000-4000-8000-000000000003"
ATTEMPT1 = "00000000-0000-4000-8000-000000000004"
ATTEMPT2 = "00000000-0000-4000-8000-000000000005"


class FileFakeAnchor:
    """Synthetic separate-store anchor; NOT a Windows durability test."""

    def __init__(self, path):
        self.path = Path(path)
        self.fail_next_write = False

    def read(self):
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="ascii"))

    def write(self, state):
        if self.fail_next_write:
            self.fail_next_write = False
            raise OSError("synthetic interrupted anchor flush")
        with self.path.open("w", encoding="ascii") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())


def make_fence(root):
    root = Path(root)
    return CycleAttemptFence(
        root / "ledger.sqlite", root / "global.lock", root / "device_media",
        FileFakeAnchor(root / "outside-anchor.json")
    )


def _attempt_in_subprocess(root, cycle, attempt, start, results):
    fence = make_fence(root)
    start.wait(timeout=5)
    try:
        with fence.serialized():
            fence.reserve_locked(DEVICE, cycle, attempt)
        results.put("RESERVED")
    except PreconditionBlocked:
        results.put("BLOCKED")


class CycleFenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "device_media").mkdir()
        self.fence = make_fence(self.root)

    def enroll(self):
        self.fence.enroll(DEVICE, supervised=True)

    def reserve(self, cycle, attempt):
        with self.fence.serialized():
            self.fence.reserve_locked(DEVICE, cycle, attempt)

    def test_no_silent_enrollment_or_re_enrollment(self):
        with self.assertRaises(PreconditionBlocked):
            self.fence.reserve_locked(DEVICE, CYCLE1, ATTEMPT1)
        with self.assertRaises(PreconditionBlocked):
            self.fence.enroll(DEVICE)
        self.enroll()
        with self.assertRaises(PreconditionBlocked):
            self.fence.enroll(DEVICE, supervised=True)

    def test_two_distinct_cycles_and_repeat_attempt_are_durable(self):
        self.enroll()
        self.reserve(CYCLE1, ATTEMPT1)
        self.reserve(CYCLE2, ATTEMPT2)
        restarted = make_fence(self.root)
        with restarted.serialized():
            restarted.check_locked(DEVICE)
            with self.assertRaises(PreconditionBlocked):
                restarted.reserve_locked(DEVICE, CYCLE1, ATTEMPT2)
            with self.assertRaises(PreconditionBlocked):
                restarted.reserve_locked(DEVICE, CYCLE2, ATTEMPT1)
        with closing(sqlite3.connect(self.root / "ledger.sqlite")) as db:
            data = str(db.execute("SELECT * FROM consumed").fetchall())
        self.assertNotIn(DEVICE, data)
        self.assertNotIn(CYCLE1, data)
        self.assertNotIn(ATTEMPT1, data)

    def test_db_only_rollback_never_resends(self):
        self.enroll()
        saved = self.root / "old-ledger-copy"
        shutil.copyfile(self.root / "ledger.sqlite", saved)
        self.reserve(CYCLE1, ATTEMPT1)
        shutil.copyfile(saved, self.root / "ledger.sqlite")
        with self.assertRaises(PreconditionBlocked):
            self.reserve(CYCLE1, ATTEMPT2)

    def test_anchor_only_rollback_never_resends(self):
        self.enroll()
        saved = (self.root / "outside-anchor.json").read_bytes()
        self.reserve(CYCLE1, ATTEMPT1)
        (self.root / "outside-anchor.json").write_bytes(saved)
        with self.assertRaises(PreconditionBlocked):
            self.reserve(CYCLE2, ATTEMPT2)

    def test_split_commit_blocks_after_anchor_failure(self):
        self.enroll()
        self.fence.anchor.fail_next_write = True
        with self.assertRaises(PreconditionBlocked):
            self.reserve(CYCLE1, ATTEMPT1)
        with self.assertRaises(PreconditionBlocked):
            self.reserve(CYCLE2, ATTEMPT2)

    def test_corruption_and_missing_anchor_fail_closed(self):
        self.enroll()
        self.reserve(CYCLE1, ATTEMPT1)
        (self.root / "outside-anchor.json").unlink()
        with self.assertRaises(PreconditionBlocked):
            self.reserve(CYCLE2, ATTEMPT2)
        with self.assertRaises(PreconditionBlocked):
            self.fence.enroll(DEVICE, supervised=True)

    def test_chain_corruption_with_unchanged_tip_detected(self):
        self.enroll()
        self.reserve(CYCLE1, ATTEMPT1)
        with closing(sqlite3.connect(self.root / "ledger.sqlite")) as db:
            db.execute("UPDATE consumed SET attempt=? WHERE seq=1", ("0" * 64,))
            db.commit()
        with self.assertRaises(PreconditionBlocked):
            self.reserve(CYCLE2, ATTEMPT2)

    def test_uncanonical_identity_never_creates_reservation(self):
        self.enroll()
        for bad in ("", "not-a-uuid", "AAAAAAAA-0000-4000-8000-000000000002", CYCLE1 + " "):
            with self.subTest(bad=bad):
                with self.assertRaises(PreconditionBlocked):
                    self.reserve(bad, ATTEMPT1)
        with self.fence.serialized():
            self.fence.check_locked(DEVICE)

    def test_independent_processes_compete_on_same_cycle(self):
        self.enroll()
        ctx = get_context("spawn")
        start = ctx.Event()
        results = ctx.Queue()
        procs = [
            ctx.Process(target=_attempt_in_subprocess,
                        args=(str(self.root), CYCLE1, ATTEMPT1, start, results)),
            ctx.Process(target=_attempt_in_subprocess,
                        args=(str(self.root), CYCLE1, ATTEMPT2, start, results)),
        ]
        for proc in procs:
            proc.start()
        try:
            start.set()
            outcomes = [results.get(timeout=15) for _ in procs]
            for proc in procs:
                proc.join(timeout=15)
            self.assertEqual([p.exitcode for p in procs], [0, 0])
            self.assertEqual(sorted(outcomes), ["BLOCKED", "RESERVED"])
        finally:
            for proc in procs:
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
