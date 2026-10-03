"""Regression coverage for Task #51 review findings.

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


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def valid_device() -> dict:
    return {
        "schemaVersion": 1,
        "deviceId": DEVICE_ID,
        "model": "M5Capsule-SYNTHETIC",
        "firmwareVersion": "0.0.0-test",
        "audioCapabilities": {},
    }


def valid_manifest(*, updated_at: str = "2026-10-03T01:05:00+09:00") -> dict:
    return {
        "schemaVersion": 1,
        "deviceId": DEVICE_ID,
        "updatedAt": updated_at,
        "recordings": [],
    }


def write_device(mount: Path, payload: bytes = b"synthetic-review-regression") -> None:
    root = mount / "M5DAYLOG"
    recordings = root / "recordings"
    recordings.mkdir(parents=True)
    (recordings / "source.wav").write_bytes(payload)
    (root / "device.json").write_text(json.dumps(valid_device()), encoding="utf-8")
    manifest = valid_manifest()
    manifest["recordings"] = [
        {
            "recordingId": RECORDING_ID,
            "filename": "recordings/source.wav",
            "startedAt": "2026-10-03T01:02:03+09:00",
            "durationMs": 1000,
            "sizeBytes": len(payload),
            "sha256": digest(payload),
            "sampleRate": 16000,
            "bitDepth": 16,
            "channels": 1,
            "state": "finalized",
            "firmwareVersion": "0.0.0-test",
        }
    ]
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


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


class ContractSemanticParityTests(unittest.TestCase):
    def test_rejects_uuid_form_accepted_by_uuid_parser_but_not_canonical_format(self):
        document = valid_device()
        document["deviceId"] = "11111111111141118111111111111111"
        with self.assertRaises(sync.ContractValidationError):
            sync.validate_contract_document(document, CONTRACTS / "device.schema.json")

    def test_rejects_week_date_datetime_not_accepted_by_canonical_format(self):
        document = valid_manifest(updated_at="2026-W40-6T12:00:00+09:00")
        with self.assertRaises(sync.ContractValidationError):
            sync.validate_contract_document(document, CONTRACTS / "manifest.schema.json")

    def test_accepts_integral_json_number_for_integer_keyword(self):
        document = valid_device()
        document["schemaVersion"] = 1.0
        sync.validate_contract_document(document, CONTRACTS / "device.schema.json")


@unittest.skipIf(not hasattr(os, "symlink"), "symlink unsupported")
class NestedDeviceTargetTests(unittest.TestCase):
    def run_sync(self, mount: Path, library: Path, db_path: Path):
        return sync.sync_device(
            mount,
            library_root=library,
            db_path=db_path,
            contracts_dir=CONTRACTS,
        )

    def test_nested_raw_symlink_into_device_is_rejected_without_device_write(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "drive"
            library = root / "library"
            db_path = root / "state.db"
            write_device(mount)

            trap = mount / "M5DAYLOG" / "write-trap"
            trap.mkdir()
            (trap / "marker.bin").write_bytes(b"device-must-remain-unchanged")

            raw = library / "2026" / "10" / "03" / "raw"
            raw.parent.mkdir(parents=True)
            try:
                raw.symlink_to(trap, target_is_directory=True)
            except OSError as exc:
                self.skipTest(type(exc).__name__)

            before = snapshot(mount)
            with self.assertRaises(sync.SyncValidationError):
                self.run_sync(mount, library, db_path)
            self.assertEqual(before, snapshot(mount))

    def test_partial_file_symlink_into_device_is_rejected_without_device_write(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "drive"
            library = root / "library"
            db_path = root / "state.db"
            write_device(mount)

            trap = mount / "M5DAYLOG" / "write-trap"
            trap.mkdir()
            device_target = trap / "partial-target.bin"
            device_target.write_bytes(b"device-must-remain-unchanged")

            raw = library / "2026" / "10" / "03" / "raw"
            raw.mkdir(parents=True)
            partial = raw / f"{RECORDING_ID}.wav.partial"
            try:
                partial.symlink_to(device_target)
            except OSError as exc:
                self.skipTest(type(exc).__name__)

            before = snapshot(mount)
            with self.assertRaises(sync.SyncValidationError):
                self.run_sync(mount, library, db_path)
            self.assertEqual(before, snapshot(mount))


if __name__ == "__main__":
    unittest.main()
