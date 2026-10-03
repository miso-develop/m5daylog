"""Regression coverage for Task #51 local-library containment.

All identifiers, metadata, paths, and byte payloads are synthetic.
"""

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
PAYLOAD = b"synthetic-library-containment"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_device(mount: Path) -> None:
    root = mount / "M5DAYLOG"
    recordings = root / "recordings"
    recordings.mkdir(parents=True)
    (recordings / "source.wav").write_bytes(PAYLOAD)
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
                        "sizeBytes": len(PAYLOAD),
                        "sha256": digest(PAYLOAD),
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


def snapshot(root: Path) -> dict[str, tuple[str, bytes | str]]:
    result: dict[str, tuple[str, bytes | str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            result[relative] = ("dir", "")
    return result


@unittest.skipIf(not hasattr(os, "symlink"), "symlink unsupported")
class LibraryContainmentTests(unittest.TestCase):
    def run_sync(self, mount: Path, library: Path, db_path: Path):
        return sync.sync_device(
            mount,
            library_root=library,
            db_path=db_path,
            contracts_dir=CONTRACTS,
        )

    def test_partial_symlink_outside_library_is_rejected_without_external_write(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "drive"
            library = root / "library"
            db_path = root / "state.db"
            external = root / "external"
            external.mkdir()
            sentinel = external / "sentinel.bin"
            sentinel.write_bytes(b"external-must-remain-unchanged")
            write_device(mount)

            raw = library / "2026" / "10" / "03" / "raw"
            raw.mkdir(parents=True)
            partial = raw / f"{RECORDING_ID}.wav.partial"
            try:
                partial.symlink_to(sentinel)
            except OSError as exc:
                self.skipTest(type(exc).__name__)

            before = snapshot(external)
            with self.assertRaises(sync.SyncValidationError):
                self.run_sync(mount, library, db_path)
            self.assertEqual(before, snapshot(external))

    def test_raw_directory_symlink_outside_library_is_rejected_without_external_write(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "drive"
            library = root / "library"
            db_path = root / "state.db"
            external = root / "external"
            external.mkdir()
            (external / "sentinel.bin").write_bytes(b"external-must-remain-unchanged")
            write_device(mount)

            raw = library / "2026" / "10" / "03" / "raw"
            raw.parent.mkdir(parents=True)
            try:
                raw.symlink_to(external, target_is_directory=True)
            except OSError as exc:
                self.skipTest(type(exc).__name__)

            before = snapshot(external)
            with self.assertRaises(sync.SyncValidationError):
                self.run_sync(mount, library, db_path)
            self.assertEqual(before, snapshot(external))


if __name__ == "__main__":
    unittest.main()
