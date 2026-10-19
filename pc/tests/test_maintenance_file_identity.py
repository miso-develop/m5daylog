"""Portable and Windows-host file-identity regressions for AC-02/08."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from m5daylog.maintenance_release import (
    MaintenanceBlocked, _check_index, _hash_file, _inventory, _read_pinned,
)


def record(name: str, payload: bytes) -> dict:
    return {
        "path": name,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


class FileIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "copies").mkdir()
        self.first = record("copies/backup-one.bin", b"backup-one")
        self.second = record("copies/backup-two.bin", b"backup-two")
        for entry, raw in ((self.first, b"backup-one"),
                           (self.second, b"backup-two")):
            (self.root / entry["path"]).write_bytes(raw)

    def _with_reparse_attribute(self, selected: Path):
        """Inject the Windows flag on Linux without depending on POSIX mode."""
        real_lstat = Path.lstat

        def lstat_with_reparse(path, *args, **kwargs):
            original = real_lstat(path, *args, **kwargs)
            if path != selected:
                return original
            return SimpleNamespace(
                st_mode=original.st_mode,
                st_file_attributes=getattr(
                    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
                st_reparse_tag=0,
                st_nlink=original.st_nlink,
                st_size=original.st_size,
                st_dev=original.st_dev,
                st_ino=original.st_ino,
                st_mtime_ns=original.st_mtime_ns,
            )

        return patch.object(Path, "lstat", lstat_with_reparse)

    def test_reparse_flag_on_intermediate_directory_blocks_backup(self):
        with self._with_reparse_attribute(self.root / "copies"):
            with self.assertRaisesRegex(MaintenanceBlocked,
                                        "EVIDENCE_DIRECTORY_UNSAFE"):
                _check_index(self.root, [self.first], 1)

    def test_reparse_flag_on_nominated_root_blocks_backup(self):
        with self._with_reparse_attribute(self.root):
            with self.assertRaisesRegex(MaintenanceBlocked,
                                        "EVIDENCE_DIRECTORY_UNSAFE"):
                _check_index(self.root, [self.first], 1)

    def test_reparse_flag_on_regular_file_blocks_backup(self):
        with self._with_reparse_attribute(self.root / self.first["path"]):
            with self.assertRaisesRegex(MaintenanceBlocked,
                                        "EVIDENCE_FILE_UNSAFE"):
                _check_index(self.root, [self.first], 1)

    def test_reparse_flag_blocks_media_inventory_descent(self):
        with self._with_reparse_attribute(self.root / "copies"):
            with self.assertRaisesRegex(MaintenanceBlocked,
                                        "MEDIA_UNSAFE_ENTRY"):
                _inventory(self.root)

    def test_reparse_flag_on_evidence_parent_blocks_pinned_read(self):
        proof = self.root / "copies" / "immutable-receipt.bin"
        payload = b"synthetic pinned evidence"
        proof.write_bytes(payload)
        with self._with_reparse_attribute(self.root / "copies"):
            with self.assertRaisesRegex(MaintenanceBlocked,
                                        "EVIDENCE_DIRECTORY_UNSAFE"):
                _read_pinned(proof, hashlib.sha256(payload).hexdigest())

    @unittest.skipUnless(os.name == "nt", "Windows NTFS junction only")
    def test_windows_ntfs_junction_outside_backup_tree_is_blocked(self):
        # Source bytes and hash match exactly, so only containment protection
        # prevents a false successful host backup evidence admission.
        approved = self.root / "approved"
        outside = self.root / "outside-approved"
        approved.mkdir()
        outside.mkdir()
        payload = b"matching preserved content"
        (outside / "backup.bin").write_bytes(payload)
        junction = approved / "copies"
        proc = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J",
             str(junction), str(outside)],
            capture_output=True, check=False,
        )
        if proc.returncode != 0:
            self.skipTest("NTFS junction creation unavailable")
        self.addCleanup(lambda: junction.rmdir() if junction.exists() else None)
        self.assertTrue(
            junction.lstat().st_file_attributes &
            getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        with self.assertRaisesRegex(MaintenanceBlocked,
                                    "EVIDENCE_DIRECTORY_UNSAFE"):
            _check_index(approved, [record("copies/backup.bin", payload)], 1)
        with self.assertRaisesRegex(MaintenanceBlocked,
                                    "EVIDENCE_DIRECTORY_UNSAFE"):
            _check_index(junction, [record("backup.bin", payload)], 1)
        with self.assertRaisesRegex(MaintenanceBlocked,
                                    "MEDIA_UNSAFE_ENTRY"):
            _inventory(approved)

    def test_distinct_objects_with_distinct_ids_are_accepted(self):
        self.assertEqual(
            _check_index(self.root, [self.first, self.second], 2),
            {self.first["path"], self.second["path"]},
        )

    def test_distinct_names_with_reused_file_id_are_rejected(self):
        original_hash = _hash_file

        def colliding_identity(path):
            value, size, _ = original_hash(path)
            return value, size, (1234, 5678)

        with patch("m5daylog.maintenance_release._hash_file",
                   side_effect=colliding_identity):
            with self.assertRaisesRegex(MaintenanceBlocked,
                                        "EVIDENCE_DUPLICATE_FILE_ID"):
                _check_index(self.root, [self.first, self.second], 2)

    @unittest.skipUnless(os.name == "nt", "Windows host-only file alias test")
    def test_case_variant_alias_is_not_second_backup(self):
        alias = dict(self.first, path="COPIES/BACKUP-ONE.BIN")
        if not (self.root / alias["path"]).is_file():
            self.skipTest("case-sensitive Windows directory")
        with self.assertRaisesRegex(MaintenanceBlocked,
                                    "EVIDENCE_DUPLICATE_FILE_ID"):
            _check_index(self.root, [self.first, alias], 2)

    def test_hard_link_alias_is_not_independent_backup(self):
        alias_name = "copies/backup-hard-link.bin"
        try:
            (self.root / alias_name).hardlink_to(self.root / self.first["path"])
        except (OSError, NotImplementedError):
            self.skipTest("host does not support hard links")
        with self.assertRaisesRegex(MaintenanceBlocked, "EVIDENCE_FILE_UNSAFE"):
            _check_index(self.root, [self.first,
                                     dict(self.first, path=alias_name)], 2)


if __name__ == "__main__":
    unittest.main()
