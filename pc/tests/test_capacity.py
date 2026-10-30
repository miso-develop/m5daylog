"""#137 synthetic capacity verification; no actual Windows/Device I/O."""

import ctypes
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from m5daylog.capacity import (
    CapacityStatus, FreeSpace64, VerifiedSession, classify_free_space,
    probe_capacity, RESERVE_BYTES, WARNING_THRESHOLD_BYTES, UINT64_MAX,
)
from m5daylog.release import EjectTarget, UsbSessionIoGate
from m5daylog.windows_capacity import WindowsCapacityReader

TARGET = EjectTarget(
    "synthetic", "E:\\", "\\\\?\\Volume{00000000-0000-0000-0000-000000000137}\\",
    8, "disk", "eject", "parent",
)


class Reader:
    def __init__(self):
        self.calls = []
        self.data = FreeSpace64(
            WARNING_THRESHOLD_BYTES, 9_000_000_000, WARNING_THRESHOLD_BYTES
        )
        self.err = False
        self.side_effect = None

    def query(self, root):
        self.calls.append(root)
        if self.side_effect:
            self.side_effect()
        if self.err:
            raise OSError("unavailable")
        return self.data


class Validator:
    def __init__(self):
        self.n = 0
        self.fail_at = None

    def revalidate_target(self, target, cdc_port):
        self.n += 1
        if target != TARGET or cdc_port != "COM7" or self.n == self.fail_at:
            raise ValueError("identity")

    def volume_available(self, target):
        return True


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.reader = Reader()
        self.validator = Validator()
        self.gate = UsbSessionIoGate()
        self.epoch = "session-1"
        self.sync = True
        self.binding = VerifiedSession(TARGET, "COM7", "session-1")

    def probe(self):
        return probe_capacity(
            self.binding, gate=self.gate, validator=self.validator,
            reader=self.reader, durable_sync_complete=lambda: self.sync,
            current_session_epoch=lambda: self.epoch,
            clock=lambda: datetime(2026, 10, 30, tzinfo=timezone.utc),
        )

    def test_exact_guid_and_freshness(self):
        r = self.probe()
        self.assertEqual(self.reader.calls, [TARGET.volume_guid])
        self.assertEqual(self.validator.n, 2)
        self.assertEqual(r.status, CapacityStatus.READY_FOR_NEXT_16H)
        self.assertEqual(r.estimated_seconds, 57600)
        self.assertTrue(r.valid_for(session_epoch=self.epoch, session_open=True))
        self.assertFalse(r.valid_for(session_epoch=self.epoch, session_open=False))
        self.assertFalse(r.valid_for(session_epoch="new", session_open=True))
        self.assertFalse(r.public_notice()["guaranteed_16h"])
        self.assertFalse(r.public_notice()["unacked_raw_auto_reclaim"])

    def test_threshold_zero_and_floor(self):
        for value, status, seconds in (
            (0, CapacityStatus.ACTION_REQUIRED, 0),
            (RESERVE_BYTES - 1, CapacityStatus.ACTION_REQUIRED, 0),
            (RESERVE_BYTES + 31999, CapacityStatus.ACTION_REQUIRED, 0),
            (WARNING_THRESHOLD_BYTES - 1, CapacityStatus.ACTION_REQUIRED, 57599),
            (WARNING_THRESHOLD_BYTES, CapacityStatus.READY_FOR_NEXT_16H, 57600),
            (WARNING_THRESHOLD_BYTES + 1, CapacityStatus.READY_FOR_NEXT_16H, 57600),
        ):
            with self.subTest(value=value):
                got = classify_free_space(
                    FreeSpace64(value, max(value, 1), value),
                    epoch="s", observed_at_utc="t",
                )
                self.assertEqual((got.status, got.estimated_seconds), (status, seconds))
                self.assertEqual(got.public_notice()["estimated_minutes"], seconds // 60)

    def test_quota_and_u64(self):
        r = classify_free_space(
            FreeSpace64(500, 600, 1000), epoch="s", observed_at_utc="t"
        )
        self.assertEqual(r.usable_free_bytes, 500)
        r = classify_free_space(
            FreeSpace64(UINT64_MAX, UINT64_MAX, UINT64_MAX),
            epoch="s", observed_at_utc="t",
        )
        self.assertGreater(r.estimated_seconds, 57600)

    def test_invalid_counters_and_error_are_unknown_not_block(self):
        bad = [
            FreeSpace64(-1, 100, 100), FreeSpace64(True, 100, 100),
            FreeSpace64(UINT64_MAX + 1, 100, 100), FreeSpace64(2, 1, 3),
            FreeSpace64(3, 9, 2), FreeSpace64(0, 0, 0), (1, 2, 3),
        ]
        for value in bad:
            with self.subTest(value=value):
                self.reader.data = value
                r = self.probe()
                self.assertEqual(r.status, CapacityStatus.UNKNOWN)
                self.assertFalse(r.release_blocked)
                self.assertIsNone(r.estimated_seconds)
        self.reader.err = True
        self.assertFalse(self.probe().release_blocked)

    def test_preflight_invalid_sync_and_sealed_never_query(self):
        for failure in ("sync", "wrong_parent", "stale", "sealed"):
            with self.subTest(failure=failure):
                self.setUp()
                if failure == "sync":
                    self.sync = False
                if failure == "wrong_parent":
                    target = replace(TARGET, usb_physical_instance_id="wrong")
                    self.binding = VerifiedSession(target, "COM7", self.epoch)
                if failure == "stale":
                    self.epoch = "next"
                if failure == "sealed":
                    self.gate.seal(timeout_s=0)
                r = self.probe()
                self.assertTrue(r.release_blocked)
                self.assertEqual(self.reader.calls, [])

    def test_post_query_identity_and_resume_block_even_if_api_failed(self):
        self.reader.err = True
        self.validator.fail_at = 2
        r = self.probe()
        self.assertEqual(r.reason, "IDENTITY_CHANGED")
        self.assertTrue(r.release_blocked)
        self.setUp()
        self.reader.side_effect = lambda: setattr(self, "epoch", "resumed")
        self.assertTrue(self.probe().release_blocked)

    def test_fake_consumer_separate_release_axis(self):
        self.reader.data = FreeSpace64(0, 1, 0)
        view = {
            "sync_release": "SYNCED_RELEASED",
            "capacity": self.probe().public_notice(),
        }
        self.assertEqual(view["sync_release"], "SYNCED_RELEASED")
        self.assertEqual(view["capacity"]["status"], "ACTION_REQUIRED")
        self.assertFalse(view["capacity"]["release_blocked_by_capacity_or_identity"])


class FakeFn:
    def __init__(self):
        self.calls = []

    def __call__(self, root, a, t, f):
        self.calls.append(root)
        for ptr, value in ((a, 123), (t, 456), (f, 234)):
            ctypes.cast(
                ptr, ctypes.POINTER(ctypes.c_ulonglong)
            ).contents.value = value
        return True


class AdapterTests(unittest.TestCase):
    def test_api_root_and_counts(self):
        class Kernel:
            GetDiskFreeSpaceExW = FakeFn()
        adapter = WindowsCapacityReader(Kernel())
        self.assertEqual(
            adapter.query(TARGET.volume_guid), FreeSpace64(123, 456, 234)
        )
        self.assertEqual(Kernel.GetDiskFreeSpaceExW.calls, [TARGET.volume_guid])
        for value in (None, "E:\\", ".", ""):
            with self.assertRaises(ValueError):
                adapter.query(value)
        self.assertEqual(len(Kernel.GetDiskFreeSpaceExW.calls), 1)


if __name__ == "__main__":
    unittest.main()
