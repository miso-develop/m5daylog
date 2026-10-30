"""#137 synthetic capacity verification; no actual Windows/Device I/O."""

import ctypes
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from threading import Event, Thread
from time import monotonic, sleep

from m5daylog.capacity import (
    CapacityStatus, FreeSpace64, VerifiedSession, classify_free_space,
    probe_capacity, RESERVE_BYTES, WARNING_THRESHOLD_BYTES, UINT64_MAX,
)
from m5daylog.release import EjectTarget, GateState, ReleaseTimeout, UsbSessionIoGate
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



    def test_unrecognized_shapes_never_query_a_drive(self):
        bad_targets = (
            replace(TARGET, mount_root="E:\\nested\\"),
            replace(TARGET, volume_guid="E:\\"),
            replace(TARGET, disk_number=True),
            replace(TARGET, disk_instance_id=""),
            replace(TARGET, usb_physical_instance_id=""),
        )
        for bad in bad_targets:
            with self.subTest(target=bad.disk_number):
                self.setUp()
                self.binding = VerifiedSession(bad, "COM7", self.epoch)
                result = self.probe()
                self.assertEqual(result.status, CapacityStatus.UNKNOWN)
                self.assertTrue(result.release_blocked)
                self.assertEqual(self.reader.calls, [])

    def test_lost_volume_before_or_after_query_blocks_release(self):
        class AvailabilityValidator(Validator):
            available = True

            def volume_available(self, target):
                return self.available

        for disappear_after_read in (False, True):
            with self.subTest(after_read=disappear_after_read):
                self.setUp()
                self.validator = AvailabilityValidator()
                if disappear_after_read:
                    self.reader.side_effect = lambda: setattr(
                        self.validator, "available", False
                    )
                else:
                    self.validator.available = False
                result = self.probe()
                self.assertEqual(result.status, CapacityStatus.UNKNOWN)
                self.assertTrue(result.release_blocked)
                self.assertIsNone(result.estimated_seconds)
                self.assertEqual(
                    self.reader.calls,
                    [TARGET.volume_guid] if disappear_after_read else [],
                )

    def test_remount_or_changed_parent_after_query_blocks_release(self):
        class TopologyValidator(Validator):
            current_target = TARGET

            def revalidate_target(self, target, cdc_port):
                super().revalidate_target(target, cdc_port)
                if target != self.current_target:
                    raise ValueError("topology changed")

        changes = (
            {"volume_guid": "\\\\?\\Volume{00000000-0000-0000-0000-000000000138}\\"},
            {"usb_physical_instance_id": "replacement-parent"},
            {"disk_number": 9},
            {"mount_root": "F:\\"},
        )
        for fields in changes:
            with self.subTest(fields=tuple(fields)):
                self.setUp()
                self.validator = TopologyValidator()
                self.reader.side_effect = lambda fields=fields: setattr(
                    self.validator, "current_target", replace(TARGET, **fields)
                )
                result = self.probe()
                self.assertEqual(result.status, CapacityStatus.UNKNOWN)
                self.assertEqual(result.reason, "IDENTITY_CHANGED")
                self.assertTrue(result.release_blocked)
                self.assertIsNone(result.usable_free_bytes)
                self.assertEqual(self.reader.calls, [TARGET.volume_guid])

    def test_unknown_notice_is_action_needed_without_fake_duration(self):
        self.reader.err = True
        result = self.probe()
        notice = result.public_notice(last_verified_session=True)
        self.assertEqual(result.status, CapacityStatus.UNKNOWN)
        self.assertFalse(result.release_blocked)
        self.assertEqual(notice["source"], "Device SD (not PC disk)")
        self.assertTrue(notice["action_required"])
        self.assertEqual(notice["observation_kind"], "unverified")
        self.assertIsNone(notice["usable_free_bytes"])
        self.assertIsNone(notice["estimated_minutes"])
        self.assertFalse(notice["guaranteed_16h"])
        self.assertFalse(notice["unacked_raw_auto_reclaim"])
        self.assertFalse(result.valid_for(session_epoch=self.epoch, session_open=True))

    def test_seal_started_in_preflight_denies_capacity_read(self):
        class SealDuringValidation(Validator):
            def revalidate_target(inner_self, target, cdc_port):
                super().revalidate_target(target, cdc_port)
                # A seal during an outstanding ordinary lease times out
                # but transitions admission to SEALED_FOR_RELEASE.
                try:
                    self.gate.seal(timeout_s=0)
                except ReleaseTimeout:
                    pass

        self.validator = SealDuringValidation()
        result = self.probe()
        self.assertTrue(result.release_blocked)
        self.assertEqual(result.status, CapacityStatus.UNKNOWN)
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(self.gate.state, GateState.SEALED_FOR_RELEASE)

    def test_seal_waits_for_inflight_query_and_invalidates_observation(self):
        read_entered = Event()
        allow_read_complete = Event()
        seal_finished = Event()
        recorded = {}

        class BlockingReader(Reader):
            def query(inner_self, root):
                inner_self.calls.append(root)
                recorded["gate_at_query_start"] = self.gate.state
                read_entered.set()
                if not allow_read_complete.wait(timeout=3):
                    raise TimeoutError("synthetic reader timed out")
                return inner_self.data

        self.reader = BlockingReader()

        def run_probe():
            recorded["result"] = self.probe()

        def run_seal():
            try:
                self.gate.seal(timeout_s=3)
                recorded["seal_outcome"] = "complete"
            except Exception as exc:
                recorded["seal_outcome"] = type(exc).__name__
            finally:
                seal_finished.set()

        probe_thread = Thread(target=run_probe, daemon=True)
        seal_thread = Thread(target=run_seal, daemon=True)
        probe_thread.start()
        try:
            self.assertTrue(read_entered.wait(timeout=3))
            seal_thread.start()
            deadline = monotonic() + 3
            while self.gate.state is GateState.OPEN and monotonic() < deadline:
                sleep(0.001)
            self.assertEqual(self.gate.state, GateState.SEALED_FOR_RELEASE)
            self.assertFalse(seal_finished.is_set())
        finally:
            allow_read_complete.set()
            probe_thread.join(timeout=3)
            if seal_thread.ident is not None:
                seal_thread.join(timeout=3)
        self.assertFalse(probe_thread.is_alive())
        self.assertFalse(seal_thread.is_alive())
        self.assertEqual(recorded["gate_at_query_start"], GateState.OPEN)
        self.assertEqual(recorded["seal_outcome"], "complete")
        self.assertEqual(self.reader.calls, [TARGET.volume_guid])
        self.assertEqual(recorded["result"].status, CapacityStatus.UNKNOWN)
        self.assertTrue(recorded["result"].release_blocked)
        self.assertIsNone(recorded["result"].estimated_seconds)

    def test_unavailable_observation_clock_is_capacity_only_unknown(self):
        def clock():
            raise RuntimeError("synthetic timestamp provider failure")

        result = probe_capacity(
            self.binding, gate=self.gate, validator=self.validator,
            reader=self.reader, durable_sync_complete=lambda: self.sync,
            current_session_epoch=lambda: self.epoch, clock=clock,
        )
        self.assertEqual(result.status, CapacityStatus.UNKNOWN)
        self.assertEqual(result.reason, "CAPACITY_TIMESTAMP_UNAVAILABLE")
        self.assertFalse(result.release_blocked)
        self.assertIsNone(result.public_notice()["estimated_minutes"])
        self.assertEqual(result.public_notice()["observation_kind"], "unverified")
        self.assertEqual(self.reader.calls, [TARGET.volume_guid])

    def test_epoch_change_during_observation_clock_fails_closed(self):
        def clock():
            self.epoch = "resumed"
            return datetime(2026, 10, 30, tzinfo=timezone.utc)

        result = probe_capacity(
            self.binding, gate=self.gate, validator=self.validator,
            reader=self.reader, durable_sync_complete=lambda: self.sync,
            current_session_epoch=lambda: self.epoch, clock=clock,
        )
        self.assertEqual(result.status, CapacityStatus.UNKNOWN)
        self.assertTrue(result.release_blocked)
        self.assertIsNone(result.usable_free_bytes)


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



    def test_win32_failure_and_exception_never_return_counters(self):
        class ReturnFalse:
            def __call__(self, root, a, t, f):
                return False

        class Throws:
            def __call__(self, root, a, t, f):
                raise OSError("synthetic Win32 failure")

        for fn in (ReturnFalse(), Throws()):
            with self.subTest(fn=type(fn).__name__):
                class Kernel:
                    GetDiskFreeSpaceExW = fn

                adapter = WindowsCapacityReader(Kernel())
                with self.assertRaises(OSError):
                    adapter.query(TARGET.volume_guid)


if __name__ == "__main__":
    unittest.main()
