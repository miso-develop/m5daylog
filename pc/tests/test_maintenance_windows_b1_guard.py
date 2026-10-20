"""#126 B1b: isolated temporary-host journal, not production release authority.

Never accesses Device paths, opens raw disks, sends CDC, or induces a crash.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import multiprocessing
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

NAME = "issue-119-maintenance-release.guard"
ATTEMPT = "8471dd1f-14ee-4f11-823a-643953f08f2b"
HISTORY = "a" * 64
HOST = "synthetic-host-volume-id"
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class NotQualified(Exception):
    """No authorization: ambiguous evidence fails terminally closed."""


@dataclass(frozen=True)
class Result:
    verdict: str
    reason: str
    release_authorized: bool = False
    directory_entry_power_loss: str = "NOT_RUN"


def reject_reparse(directory: Path) -> None:
    """Check every existing component; no reliance on resolve() alone."""
    current = Path(directory.anchor)
    for part in directory.parts[1:]:
        current = current / part
        metadata = current.lstat()
        if (stat.S_ISLNK(metadata.st_mode) or
                getattr(metadata, "st_file_attributes", 0) & REPARSE or
                getattr(metadata, "st_reparse_tag", 0)):
            raise NotQualified("PATH_REPARSE_UNTRUSTED")
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotQualified("NOT_DIRECTORY")


class FixtureJournal:
    """O_EXCL process-race probe, with deliberately unqualified durability."""
    def __init__(self, directory: Path, approved: Path, *, trust_acl: bool = True,
                 expected_host: str = HOST):
        self.directory = Path(directory).absolute()
        self.approved = Path(approved).absolute()
        self.trust_acl = trust_acl
        self.expected_host = expected_host

    def consume(self, attempt: str = ATTEMPT, history: str = HISTORY,
                host: str = HOST, *, fault: str | None = None) -> Result:
        try:
            if (not self.trust_acl or host != self.expected_host or
                    type(attempt) is not str or str(uuid.UUID(attempt)) != attempt or
                    type(history) is not str or not re.fullmatch("[0-9a-f]{64}", history)):
                raise NotQualified("ATTEMPT_HISTORY_HOST_OR_ACL_UNTRUSTED")
            reject_reparse(self.directory)
            reject_reparse(self.approved)
            if self.directory.resolve(strict=True) != self.approved.resolve(strict=True):
                raise NotQualified("GUARD_DIRECTORY_CHANGED")
            if fault == "before_create":
                raise NotQualified("INJECTED_BEFORE_CREATE")
            path = self.directory / NAME
            # A fixed marker prevents re-keying to a different attempt/path.
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            fd = os.open(path, flags, 0o600)
            try:
                if fault == "after_create":
                    raise NotQualified("INJECTED_AFTER_CREATE")
                document = json.dumps({"attempt": attempt, "history": history,
                                       "host": host, "consumed": True},
                                      sort_keys=True).encode("ascii") + b"\n"
                with os.fdopen(fd, "wb") as stream:
                    fd = -1
                    stream.write(document)
                    if fault == "after_write":
                        raise NotQualified("INJECTED_AFTER_WRITE")
                    stream.flush()
                    if fault == "after_flush":
                        raise NotQualified("INJECTED_AFTER_FLUSH")
                    os.fsync(stream.fileno())
                    if fault == "after_fsync":
                        raise NotQualified("INJECTED_AFTER_FSYNC")
            finally:
                if fd >= 0:
                    os.close(fd)
            # Even successful file fsync does NOT qualify directory durability.
            return Result("SOURCE_TEST_ONLY", "FILE_CREATED_NAMESPACE_POWER_LOSS_UNPROVEN")
        except FileExistsError:
            return Result("BLOCKED", "ATTEMPT_ALREADY_CONSUMED_OR_UNCERTAIN")
        except (NotQualified, OSError, ValueError, TypeError) as exc:
            return Result("BLOCKED", type(exc).__name__)


def _process_claim(path: str, start, results) -> None:
    start.wait(10)
    results.put(FixtureJournal(Path(path), Path(path)).consume().verdict)


class GuardFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="b1-guard-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "authorized"
        self.root.mkdir()
        self.guard = FixtureJournal(self.root, self.root)

    def test_success_is_never_release_authority_or_power_loss_pass(self):
        result = self.guard.consume()
        self.assertEqual(result.verdict, "SOURCE_TEST_ONLY")
        self.assertFalse(result.release_authorized)
        self.assertEqual(result.directory_entry_power_loss, "NOT_RUN")
        stored = json.loads((self.root / NAME).read_text())
        self.assertEqual((stored["attempt"], stored["history"], stored["host"]),
                         (ATTEMPT, HISTORY, HOST))
        self.assertTrue(stored["consumed"])

    def test_duplicate_after_restart_always_blocked(self):
        self.guard.consume()
        next_process_model = FixtureJournal(self.root, self.root)
        self.assertEqual(next_process_model.consume().verdict, "BLOCKED")
        self.assertEqual(next_process_model.consume(attempt=str(uuid.uuid4())).verdict,
                         "BLOCKED")

    def test_faults_never_authorize_send_or_recreate(self):
        for fault in ("before_create", "after_create", "after_write",
                      "after_flush", "after_fsync"):
            with self.subTest(fault=fault):
                path = self.root / NAME
                path.unlink(missing_ok=True)  # test-only reset of disposable fixture
                outcome = self.guard.consume(fault=fault)
                self.assertEqual(outcome.verdict, "BLOCKED")
                self.assertFalse(outcome.release_authorized)
                if fault != "before_create":
                    self.assertTrue(path.exists())
                    self.assertEqual(self.guard.consume().verdict, "BLOCKED")
                else:
                    self.assertFalse(path.exists())

    def test_corrupt_existing_or_lost_ack_is_terminal(self):
        (self.root / NAME).write_bytes(b"{partial\n")
        self.assertEqual(self.guard.consume().verdict, "BLOCKED")
        # No code here can send, replay, clear, or infer acceptance from ACK.
        self.assertTrue((self.root / NAME).exists())

    def test_alias_and_different_directory_denied(self):
        other = self.root.parent / "other"
        other.mkdir()
        self.assertEqual(FixtureJournal(other, self.root).consume().verdict, "BLOCKED")
        self.assertFalse((other / NAME).exists())

    def test_invalid_identity_and_untrusted_acl_denied(self):
        variants = [dict(attempt="not-a-uuid"), dict(history="b"),
                    dict(host="another-volume")]
        for kwargs in variants:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.guard.consume(**kwargs).verdict, "BLOCKED")
        self.assertEqual(FixtureJournal(self.root, self.root,
                         trust_acl=False).consume().verdict, "BLOCKED")
        self.assertFalse((self.root / NAME).exists())

    def test_permission_failure_denied(self):
        # Deterministic injected denial: Windows hosted runners may be privileged.
        with patch.object(os, "open", side_effect=PermissionError()):
            self.assertEqual(self.guard.consume().verdict, "BLOCKED")
        self.assertFalse((self.root / NAME).exists())

    def test_directory_symlink_rejected(self):
        alias = self.root.parent / "alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("platform cannot create symlink: " + type(exc).__name__)
        self.assertEqual(FixtureJournal(alias, self.root).consume().verdict, "BLOCKED")

    def test_reparse_attribute_injection_rejected(self):
        class FakeStat:
            st_mode = stat.S_IFDIR | 0o700
            st_file_attributes = REPARSE
            st_reparse_tag = 0
        # A portable simulated Windows-junction attribute (not real NTFS).
        with patch.object(Path, "lstat", return_value=FakeStat()):
            with self.assertRaises(NotQualified):
                reject_reparse(self.root)

    @unittest.skipUnless(sys.platform == "win32", "actual junction needs Windows")
    def test_windows_ntfs_junction_denied(self):
        target = self.root.parent / "target"
        target.mkdir()
        junction = self.root.parent / "junction"
        creation = subprocess.run(["cmd", "/c", "mklink", "/J",
                                   str(junction), str(target)],
                                  text=True, capture_output=True)
        if creation.returncode != 0:
            if os.environ.get("M5DAYLOG_REQUIRE_B1_JUNCTION") == "1":
                self.fail("Windows runner failed required disposable junction creation")
            self.skipTest("Windows junction not supported in this local session")
        self.assertEqual(FixtureJournal(junction, target).consume().verdict, "BLOCKED")
        self.assertFalse((target / NAME).exists())
        # os.rmdir deletes just the junction, never follows it into target.
        os.rmdir(junction)

    def test_actual_competing_process_create_is_single_winner(self):
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        results = ctx.Queue()
        workers = [ctx.Process(target=_process_claim,
                               args=(str(self.root), start, results)) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            start.set()
            outcomes = sorted(results.get(timeout=20) for _ in workers)
            self.assertEqual(outcomes, ["BLOCKED", "SOURCE_TEST_ONLY"])
        finally:
            for worker in workers:
                worker.join(timeout=20)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
            self.assertTrue(all(worker.exitcode == 0 for worker in workers))


if __name__ == "__main__":
    unittest.main()
