"""Portable and Windows-host file-identity regressions for AC-02/08."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from m5daylog.maintenance_release import (
    MaintenanceBlocked, _check_index, _hash_file,
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
