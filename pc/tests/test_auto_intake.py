"""#135 source-only admission / intake tests. No physical MSC/CDC actions."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from m5daylog.auto_intake import (
    Arrival, IntakeCoordinator, PollingMonitor, exclusive_intake,
)
from m5daylog.capacity import (
    CapacityStatus, FreeSpace64, VerifiedSession, WARNING_THRESHOLD_BYTES,
)
from m5daylog.release import EjectTarget
from m5daylog.sync import SyncSummary


DEVICE = "00000000-0000-4000-8000-000000000135"
TARGET = EjectTarget(
    DEVICE, "E:\\", "\\\\?\\Volume{00000000-0000-0000-0000-000000000135}\\",
    7, "synthetic-disk", "synthetic-eject", "synthetic-parent",
)


class Port:
    def __init__(self):
        self.calls = []
        self.reject_at = 0
        self.present = True

    def revalidate_target(self, target, port):
        self.calls.append("revalidate")
        if (target != TARGET or port != "COM7"
                or len([x for x in self.calls if x == "revalidate"]) == self.reject_at):
            raise ValueError("synthetic mismatch")

    def volume_available(self, target):
        self.calls.append("available")
        return self.present


class Reader:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.value = FreeSpace64(
            WARNING_THRESHOLD_BYTES, 4_000_000_000, WARNING_THRESHOLD_BYTES
        )

    def query(self, root):
        self.calls.append(root)
        if self.fail:
            raise OSError("synthetic API error")
        return self.value


class Provider:
    def __init__(self):
        self.items = []

    def scan(self):
        return list(self.items)


class AutoIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.port = Port()
        self.reader = Reader()
        self.events = []
        self.recorded = []
        self.epoch = "synthetic-session-1"
        self.binding = VerifiedSession(TARGET, "COM7", self.epoch)
        self.arrival = Arrival(self.binding, usb_sync_confirmed=True)
        self.sync_result = SyncSummary(copied=1)
        self.preflight_id = DEVICE
        self.coordinator = IntakeCoordinator(
            port=self.port, capacity_reader=self.reader,
            trusted_device_ids=frozenset({DEVICE}),
            local_lock_file=self.base / "state" / "monitor.lock",
            library_root=self.base / "raw",
            db_path=self.base / "state" / "m5daylog.db",
            preflight=lambda root: self._preflight(root),
            sync=lambda root: self._sync(root),
        )

    def _preflight(self, root):
        self.events.append("preflight")
        return self.preflight_id

    def _sync(self, root):
        self.events.append("sync")
        return self.sync_result

    def intake(self, arrival=None):
        return self.coordinator.intake(
            self.arrival if arrival is None else arrival,
            current_session_epoch=lambda: self.epoch,
        )

    def test_authorized_intake_is_safe_to_retry_and_never_releases(self):
        result = self.intake()
        self.assertEqual(result.status, "RELEASE_BLOCKED")
        self.assertEqual(result.reason, "AUTO_RELEASE_NOT_QUALIFIED")
        self.assertEqual(result.copied, 1)
        self.assertEqual(self.events, ["preflight", "sync"])
        self.assertEqual(result.capacity.status, CapacityStatus.READY_FOR_NEXT_16H)
        self.assertEqual(self.reader.calls, [TARGET.volume_guid])
        self.assertGreaterEqual(self.port.calls.count("revalidate"), 4)
        output = json.dumps(result.public_notice(), ensure_ascii=False)
        self.assertNotIn(DEVICE, output)
        self.assertNotIn("COM7", output)
        self.assertNotIn("synthetic-parent", output)
        self.assertNotIn(str(self.base), output)
        self.assertNotIn("SYNCED_RELEASED", output)
        self.assertFalse(result.public_notice()["safe_to_leave_usb_connected"])
        self.assertFalse(result.public_notice()["device_shutdown_armed_proven"])

        self.sync_result = SyncSummary(copied=0, reused=1)
        second = self.intake()
        self.assertEqual((second.copied, second.reused), (0, 1))
        self.assertEqual(second.status, "RELEASE_BLOCKED")

    def test_reject_before_copy_when_not_authorized_or_usb_sync_unproven(self):
        cases = [
            Arrival(self.binding, False),
            Arrival(VerifiedSession(replace(TARGET, expected_device_id="unknown"),
                                    "COM7", self.epoch), True),
            Arrival(VerifiedSession(replace(TARGET, mount_root="C:\\fake\\"),
                                    "COM7", self.epoch), True),
            Arrival(VerifiedSession(replace(TARGET, volume_guid="E:\\"),
                                    "COM7", self.epoch), True),
            Arrival(VerifiedSession(replace(TARGET, usb_physical_instance_id=""),
                                    "COM7", self.epoch), True),
            Arrival(VerifiedSession(TARGET, "COM7", ""), True),
        ]
        for bad in cases:
            with self.subTest(candidate=repr(bad)):
                result = self.intake(bad)
                self.assertEqual(result.status, "RELEASE_BLOCKED")
                self.assertEqual(result.reason, "UNAUTHORIZED_OR_UNPROVEN_SESSION")
                self.assertEqual(self.events, [])
                self.assertEqual(self.port.calls, [])

    def test_identity_mismatch_or_session_change_blocks_before_sync(self):
        self.preflight_id = "mismatch"
        self.assertEqual(self.intake().reason, "DEVICE_IDENTITY_MISMATCH")
        self.assertEqual(self.events, ["preflight"])
        self.events.clear()
        self.preflight_id = DEVICE
        self.epoch = "resume"
        self.assertEqual(self.intake().reason, "STALE_SESSION")
        self.assertEqual(self.events, [])
        self.epoch = self.binding.session_epoch
        self.port.calls.clear()  # independent fault phase; fail the first revalidation
        self.port.reject_at = 1
        self.assertEqual(self.intake().status, "SYNC_FAILED")
        self.assertEqual(self.events, [])

    def test_copy_error_conflict_and_post_sync_identity_failure_never_claim_release(self):
        self.sync_result = SyncSummary(copied=0, conflicts=1)
        result = self.intake()
        self.assertEqual((result.status, result.reason), ("SYNC_FAILED", "SYNC_CONFLICT"))
        self.assertEqual(self.reader.calls, [])
        self.setUp()  # isolated synthetic test objects
        self.port.reject_at = 4
        result = self.intake()
        self.assertEqual(result.reason, "POST_SYNC_IDENTITY_UNVERIFIED")
        self.assertEqual(result.status, "RELEASE_BLOCKED")
        self.assertEqual(self.reader.calls, [TARGET.volume_guid])
        self.assertTrue(result.capacity.release_blocked)

    def test_capacity_api_failure_warns_without_false_success(self):
        self.reader.fail = True
        result = self.intake()
        self.assertEqual(result.status, "RELEASE_BLOCKED")
        self.assertEqual(result.capacity.status, CapacityStatus.UNKNOWN)
        self.assertFalse(result.capacity.release_blocked)
        self.assertTrue(result.public_notice()["capacity"]["action_required"])

    def test_local_lock_conflict_has_no_copy_and_no_cdc(self):
        with exclusive_intake(self.coordinator.local_lock_file) as held:
            self.assertTrue(held)
            result = self.intake()
            self.assertEqual(result.reason, "OTHER_INTAKE_ACTIVE")
            self.assertEqual(self.events, [])

    def test_polling_debounces_coalesces_and_invalidates_after_absence(self):
        provider = Provider()
        notices = []
        monitor = PollingMonitor(
            provider, self.coordinator, lambda: self.epoch,
            notices.append,
        )
        provider.items = [self.arrival]
        monitor.poll_once()
        self.assertEqual(len(notices), 0)
        monitor.poll_once()
        self.assertEqual([n["sync_release"] for n in notices],
                         ["SYNCING", "RELEASE_BLOCKED"])
        monitor.poll_once()
        self.assertEqual(len(self.events), 2)
        provider.items = []
        monitor.poll_once()
        provider.items = [self.arrival, self.arrival]
        monitor.poll_once()
        self.assertEqual(len(self.events), 2)
        provider.items = [self.arrival]
        monitor.poll_once()
        monitor.poll_once()
        self.assertEqual(len(self.events), 4)

    def test_monitor_active_notice_and_invalid_poll_interval(self):
        provider = Provider()
        seen = []
        monitor = PollingMonitor(provider, self.coordinator, lambda: self.epoch,
                                 seen.append)
        monitor.run_forever(stop=lambda: True)
        self.assertEqual(seen[0]["sync_release"], "WAITING_FOR_DEVICE")
        with self.assertRaises(ValueError):
            monitor.run_forever(interval_s=0)


if __name__ == "__main__":
    unittest.main()
