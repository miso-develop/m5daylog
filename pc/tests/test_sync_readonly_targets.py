"""Regression tests that keep Task #51 Device mounts strictly read-only."""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from m5daylog import sync

REPO = Path(__file__).resolve().parents[2]
CONTRACTS = REPO / "contracts"
DEVICE_ID = "11111111-1111-4111-8111-111111111111"
RECORDING_ID = "22222222-2222-4222-8222-222222222222"


def snapshot(root: Path):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            result[relative] = ("dir", "")
    return result


def write_device(mount: Path) -> None:
    payload = b"synthetic-read-only-regression"
    root = mount / "M5DAYLOG"
    recordings = root / "recordings"
    recordings.mkdir(parents=True)
    (recordings / "source.wav").write_bytes(payload)
    (root / "device.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "deviceId": DEVICE_ID,
                "model": "M5Capsule-SYNTHETIC",
                "firmwareVersion": "0.0.0-test",
                "audioCapabilities": {},
            }
        ),
        encoding="utf-8",
    )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "deviceId": DEVICE_ID,
                "updatedAt": "2026-10-03T01:05:00+09:00",
                "recordings": [
                    {
                        "recordingId": RECORDING_ID,
                        "filename": "recordings/source.wav",
                        "startedAt": "2026-10-03T01:02:03+09:00",
                        "durationMs": 1000,
                        "sizeBytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "sampleRate": 16000,
                        "bitDepth": 16,
                        "channels": 1,
                        "state": "finalized",
                        "firmwareVersion": "0.0.0-test",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


class DeviceReadOnlyTargetTests(unittest.TestCase):
    def test_library_root_inside_device_mount_is_rejected_without_write(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "drive"
            write_device(mount)
            before = snapshot(mount)
            with self.assertRaises(sync.SyncValidationError):
                sync.sync_device(
                    mount,
                    library_root=mount / "local-output",
                    db_path=root / "state.db",
                    contracts_dir=CONTRACTS,
                )
            self.assertEqual(before, snapshot(mount))

    def test_db_path_inside_device_mount_is_rejected_without_write(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "drive"
            write_device(mount)
            before = snapshot(mount)
            with self.assertRaises(sync.SyncValidationError):
                sync.sync_device(
                    mount,
                    library_root=root / "library",
                    db_path=mount / "state" / "m5daylog.db",
                    contracts_dir=CONTRACTS,
                )
            self.assertEqual(before, snapshot(mount))


if __name__ == "__main__":
    unittest.main()
