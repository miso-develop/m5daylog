"""#121 synthetic-only maintenance release matrix (no Device or Windows calls)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from m5daylog.maintenance_release import (
    GatePermit, MaintenanceBlocked, MaintenanceReleaseCoordinator,
    OneShotGuard, Preflight, verify_post_clear,
)
from m5daylog.release import EjectTarget, UsbSessionIoGate, GateState


TARGET = EjectTarget(
    "test-device", "E:\\", "test-volume", 8, "test-disk",
    "test-eject", "test-physical-usb",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MediaFixture:
    def __init__(self, root: Path):
        self.mount = root / "device"
        self.backup = root / "backup"
        self.mount.mkdir()
        self.backup.mkdir()
        (self.mount / "M5DAYLOG" / "recordings").mkdir(parents=True)
        (self.mount / "M5DAYLOG" / "acks").mkdir()
        (self.mount / "M5DAYLOG" / "quarantine").mkdir()
        self.backups = []
        self.protected = []
        for i in range(117):
            rel = f"copy/{i:03d}.bin"
            self.backups.append(self.write(self.backup, rel, b"old-pcm" + bytes([i])))
        self.protected.append(self.write(
            self.mount, "M5DAYLOG/device.json", b"synthetic-device"))
        for i in range(37):
            self.protected.append(self.write(
                self.mount, f"root-file-{i:02d}", b"protected" + bytes([i])))
        self.receipt = root / "receipt.json"
        self.payload = {
            "schema_version": 1,
            "issue": 119,
            "device_id": "test-device",
            "clear_guard_consumed": True,
            "deleted": {
                "wav": 76, "manifests": 2, "events": 1,
                "recording_directories": 10,
            },
            "backups": self.backups,
            "protected": self.protected,
        }
        self.save()

    @staticmethod
    def write(root: Path, name: str, data: bytes) -> dict:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {"path": name, "size": len(data), "sha256": digest(data)}

    def save(self):
        data = json.dumps(self.payload, sort_keys=True).encode("utf-8")
        self.receipt.write_bytes(data)
        self.pin = digest(data)

    def check(self, pin=None):
        with patch("m5daylog.maintenance_release.inspect_device",
                   return_value={"deviceId": "test-device"}):
            return verify_post_clear(
                mount_root=self.mount, backup_root=self.backup,
                receipt_path=self.receipt,
                pinned_receipt_sha256=self.pin if pin is None else pin,
            )


class MediaEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fx = MediaFixture(Path(self.temp.name))

    def test_valid_clear_receipt_checks_all_files_and_is_read_only(self):
        result = self.fx.check()
        self.assertEqual((result.verified_backups, result.verified_protected),
                         (117, 38))
        self.assertEqual(result.receipt_sha256, self.fx.pin)
        self.assertFalse((self.fx.mount / "M5DAYLOG/manifest.json").exists())

    def test_missing_or_changed_receipt_fails_before_success(self):
        with self.assertRaises(MaintenanceBlocked):
            self.fx.check("0" * 64)
        self.fx.payload["clear_guard_consumed"] = False
        self.fx.save()
        with self.assertRaisesRegex(MaintenanceBlocked, "CLEAR_NOT_PROVEN"):
            self.fx.check()

    def test_prohibited_deletion_counts_and_duplicate_entries(self):
        self.fx.payload["deleted"]["wav"] = 75
        self.fx.save()
        with self.assertRaisesRegex(MaintenanceBlocked, "CLEAR_NOT_PROVEN"):
            self.fx.check()
        self.fx.payload["deleted"]["wav"] = 76
        self.fx.payload["backups"][1] = dict(self.fx.payload["backups"][0])
        self.fx.save()
        with self.assertRaisesRegex(MaintenanceBlocked, "INVALID_EVIDENCE_RECORD"):
            self.fx.check()

    def test_backup_hash_and_protected_hash_mismatch(self):
        (self.fx.backup / self.fx.backups[1]["path"]).write_bytes(b"altered")
        with self.assertRaisesRegex(MaintenanceBlocked, "EVIDENCE_HASH_MISMATCH"):
            self.fx.check()
        self.fx.write(self.fx.backup, self.fx.backups[1]["path"], b"old-pcm" + bytes([1]))
        (self.fx.mount / self.fx.protected[0]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(MaintenanceBlocked, "EVIDENCE_HASH_MISMATCH"):
            self.fx.check()

    def test_new_wav_part_quarantine_or_recovered_directory_fails(self):
        additions = [
            "M5DAYLOG/recordings/2026-10-19/clip.wav",
            "M5DAYLOG/recordings/new.wav.part",
            "M5DAYLOG/quarantine/new.part",
            "M5DAYLOG/manifest.json",
            "M5DAYLOG/events.jsonl",
            "new-metadata.bin",
        ]
        for relative in additions:
            with self.subTest(path=relative):
                f = self.fx.mount / relative
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(b"new")
                with self.assertRaises(MaintenanceBlocked):
                    self.fx.check()
                f.unlink()
                if f.parent.name == "2026-10-19":
                    f.parent.rmdir()

    def test_empty_unexpected_folder_and_symlink_denied(self):
        suspect = self.fx.mount / "unlisted-dir"
        suspect.mkdir()
        with self.assertRaisesRegex(MaintenanceBlocked, "UNEXPECTED_MEDIA_DIRECTORY"):
            self.fx.check()
        suspect.rmdir()
        try:
            (self.fx.mount / "strange-link").symlink_to(self.fx.receipt)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this host")
        with self.assertRaisesRegex(MaintenanceBlocked, "MEDIA_UNSAFE_ENTRY"):
            self.fx.check()

    def test_wrong_device_id_or_unsafe_receipt_path(self):
        with patch("m5daylog.maintenance_release.inspect_device",
                   return_value={"deviceId": "swapped"}):
            with self.assertRaisesRegex(MaintenanceBlocked, "DEVICE_ID_CHANGED"):
                verify_post_clear(
                    mount_root=self.fx.mount, backup_root=self.fx.backup,
                    receipt_path=self.fx.receipt,
                    pinned_receipt_sha256=self.fx.pin,
                )
        self.fx.payload["backups"][0]["path"] = "../secret"
        self.fx.save()
        with self.assertRaisesRegex(MaintenanceBlocked, "INVALID_EVIDENCE_PATH"):
            self.fx.check()

    def test_guard_must_be_host_local_and_replay_is_denied(self):
        guard = OneShotGuard(Path(self.temp.name) / "guard.json")
        permit = GatePermit(119, self.fx.pin, TARGET, str(uuid.uuid4()), True)
        guard.consume(permit)
        obj = json.loads(guard.path.read_text(encoding="ascii"))
        self.assertTrue(obj["consumed"])
        with self.assertRaisesRegex(MaintenanceBlocked, "ATTEMPT_ALREADY_CONSUMED"):
            guard.consume(permit)


class FakePort:
    def __init__(self):
        self.steps = []
        self.fail = None
        self.disappear = True

    def resolve_target(self, root, device_id):
        self.steps.append("resolve")
        if self.fail == "resolve":
            raise RuntimeError("changed")
        return TARGET

    def revalidate_target(self, target, cdc_port):
        self.steps.append("revalidate")
        if self.fail == "revalidate":
            raise RuntimeError("target changed")

    def volume_available(self, target):
        self.steps.append("available")
        return True

    @contextmanager
    def quiesce(self, target):
        self.steps.append("lock")
        if self.fail == "lock":
            raise RuntimeError("lock failed")
        self.steps.append("dismount")
        if self.fail == "dismount":
            raise RuntimeError("dismount failed")
        yield

    def release_storage(self, cdc_port, attempt_id, target):
        self.steps.append("release")
        if self.fail == "transport":
            raise RuntimeError("response lost")

    def volume_disappeared(self, target):
        self.steps.append("observe")
        return self.disappear


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.port = FakePort()
        self.mount = Path(self.tmp.name) / "mounted-device"
        self.mount.mkdir()
        self.guard = OneShotGuard(Path(self.tmp.name) / "guard.json")
        self.gate = UsbSessionIoGate()
        self.proof = Preflight("a" * 64, "test-device", 117, 38)
        self.calls = []
        self.attempt = str(uuid.uuid4())
        self.now = 0.0
        self.coordinator = MaintenanceReleaseCoordinator(
            self.port, timeout_s=1,
            clock=lambda: self.now,
            sleep=lambda seconds: setattr(self, "now", self.now + seconds))

    def preflight(self):
        self.calls.append("preflight")
        return self.proof

    def permit(self, proof, target):
        self.calls.append("qualify")
        return GatePermit(119, proof.receipt_sha256, target, self.attempt, True)

    def run_case(self, preflight=None, qualifier=None):
        return self.coordinator.run(
            mount_root=str(self.mount), cdc_port="COM5", gate=self.gate,
            fresh_preflight=self.preflight if preflight is None else preflight,
            trusted_human_gate_qualifier=self.permit if qualifier is None else qualifier,
            guard=self.guard)

    def test_qualified_success_only_maintenance_not_sync(self):
        result = self.run_case()
        self.assertTrue(result.success)
        self.assertEqual(result.status, "MAINTENANCE_RELEASE_ONLY")
        self.assertEqual(self.calls, ["preflight", "preflight", "qualify"])
        self.assertEqual(self.port.steps, [
            "resolve", "revalidate", "available", "revalidate",
            "lock", "dismount", "release", "observe"])
        self.assertEqual(self.gate.state, GateState.RELEASED)
        self.assertTrue(self.guard.path.exists())
        self.assertEqual(self.port.steps.count("release"), 1)

    def test_guard_on_device_media_blocks_before_quiesce(self):
        self.guard = OneShotGuard(self.mount / "forbidden-guard.json")
        outcome = self.run_case()
        self.assertEqual(outcome.reason, "GUARD_ON_DEVICE_MEDIA")
        self.assertNotIn("lock", self.port.steps)
        self.assertNotIn("release", self.port.steps)
        self.assertFalse(self.guard.path.exists())

    def test_guard_fsync_failure_never_sends_and_consumes_uncertain_attempt(self):
        with patch("m5daylog.maintenance_release.os.fsync",
                   side_effect=OSError("simulated fsync failure")):
            outcome = self.run_case()
        self.assertEqual(outcome.reason, "GUARD_DURABILITY_UNVERIFIED")
        self.assertTrue(self.guard.path.exists())
        self.assertNotIn("release", self.port.steps)
        self.gate = UsbSessionIoGate()
        second = self.run_case()
        self.assertEqual(second.reason, "ATTEMPT_ALREADY_CONSUMED")
        self.assertNotIn("release", self.port.steps)

    def test_changed_cdc_target_prevents_quiescence(self):
        self.port.fail = "revalidate"
        outcome = self.run_case()
        self.assertEqual(outcome.status, "BLOCKED")
        self.assertNotIn("lock", self.port.steps)
        self.assertNotIn("release", self.port.steps)

    def test_missing_provenance_or_approval_sends_no_release(self):
        for permit in (None, GatePermit(119, self.proof.receipt_sha256,
                  TARGET, self.attempt, False),
                  GatePermit(119, "b" * 64, TARGET, self.attempt, True)):
            with self.subTest(permit=permit):
                self.setUp()
                result = self.run_case(qualifier=lambda *_: permit)
                self.assertEqual(result.status, "BLOCKED")
                self.assertNotIn("lock", self.port.steps)
                self.assertNotIn("release", self.port.steps)

    def test_media_changed_between_inventory_and_final_check(self):
        reads = 0
        def changed():
            nonlocal reads
            reads += 1
            return (self.proof if reads == 1 else
                    Preflight("b" * 64, "test-device", 117, 38))
        result = self.run_case(preflight=changed)
        self.assertEqual(result.reason, "PREFLIGHT_DRIFT")
        self.assertNotIn("release", self.port.steps)

    def test_lock_and_dismount_failure_never_consume_or_send(self):
        for failure in ("lock", "dismount"):
            with self.subTest(failure=failure):
                self.setUp()
                self.port.fail = failure
                outcome = self.run_case()
                self.assertEqual(outcome.status, "BLOCKED")
                self.assertNotIn("release", self.port.steps)
                self.assertFalse(self.guard.path.exists())

    def test_transport_loss_consumes_attempt_no_replay(self):
        self.port.fail = "transport"
        result = self.run_case()
        self.assertEqual(result.status, "BLOCKED")
        self.assertTrue(self.guard.path.exists())
        self.assertEqual(self.port.steps.count("release"), 1)
        # A new synthetic process/USB admission gate cannot bypass host guard.
        self.gate = UsbSessionIoGate()
        self.port.fail = None
        again = self.run_case()
        self.assertEqual(again.reason, "ATTEMPT_ALREADY_CONSUMED")
        self.assertEqual(self.port.steps.count("release"), 1)

    def test_disappearance_without_correlated_acceptance_not_success(self):
        self.port.fail = "transport"
        self.port.disappear = True
        result = self.run_case()
        self.assertFalse(result.success)
        self.assertNotIn("observe", self.port.steps)

    def test_timeout_after_acceptance_remains_consumed(self):
        self.port.disappear = False
        result = self.run_case()
        self.assertEqual(result.reason, "POST_ACCEPT_TIMEOUT")
        self.assertEqual(self.gate.state, GateState.RELEASE_ACCEPTED)
        self.assertTrue(self.guard.path.exists())
        self.assertEqual(self.port.steps.count("release"), 1)

    def test_preflight_exception_never_seals_or_sends(self):
        result = self.run_case(preflight=lambda: (_ for _ in ()).throw(
            MaintenanceBlocked("BLOCKED_PROVENANCE")))
        self.assertEqual(result.reason, "BLOCKED_PROVENANCE")
        self.assertEqual(self.gate.state, GateState.OPEN)
        self.assertEqual(self.port.steps, [])


if __name__ == "__main__":
    unittest.main()
