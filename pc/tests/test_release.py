"""#89 fail-closed release tests. No physical Device or Windows runtime required."""

from __future__ import annotations

import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from m5daylog.release import (
    AckFlushResult, AlreadyRemoved, EjectTarget, GateClosed, GateState,
    HostQuiesceError, ReleaseIndeterminate, ReleaseRejected,
    SyncReleaseCoordinator, UsbSessionIoGate, WrongTarget,
)
from m5daylog.sync import SyncSummary
from m5daylog.windows_release import DiskInterface, WindowsHostReleasePort


TARGET = EjectTarget(
    "test-device", "E:\\", "\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\",
    8, "USBSTOR\\DISK&VEN_TEST", "USB\\VID_TEST",
    "USB\\VID_303A&PID_4001\\TEST",
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, delta):
        self.now += delta


class FakePort:
    def __init__(self):
        self.steps = []
        self.failure = None
        self.present = True
        self.disappeared = True
        self.on_quiesce = None
        self.on_revalidate = None

    def resolve_target(self, root, expected):
        self.steps.append("resolve")
        if self.failure == "target":
            raise WrongTarget("target changed")
        return TARGET

    def revalidate_target(self, target, cdc_port):
        self.steps.append("revalidate")
        if self.on_revalidate is not None:
            self.on_revalidate()
        if self.failure == "revalidate":
            raise WrongTarget("device changed")

    def volume_available(self, target):
        self.steps.append("available")
        return self.present

    @contextmanager
    def quiesce(self, target):
        self.steps.append("lock")
        if self.failure == "lock":
            raise HostQuiesceError("volume lock failed")
        self.steps.append("dismount")
        if self.failure == "dismount":
            raise HostQuiesceError("volume dismount failed")
        if self.on_quiesce is not None:
            self.on_quiesce()
        try:
            yield
        finally:
            self.steps.append("unlock")

    def release_storage(self, cdc_port, attempt_id, target):
        self.steps.append("release")
        if self.failure == "reject":
            raise ReleaseRejected("rejected")
        if self.failure == "indeterminate":
            raise ReleaseIndeterminate("response lost")

    def volume_disappeared(self, target):
        self.steps.append("observe")
        return self.disappeared


class ReleaseCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.port = FakePort()
        self.gate = UsbSessionIoGate()
        self.sync_calls = 0
        self.coordinator = SyncReleaseCoordinator(
            self.port, clock=self.clock.time, sleep=self.clock.sleep,
            timeout_s=1.0, poll_interval_s=0.25,
        )

    def run_release(self, *, sync_conflicts=0, sync_error=False,
                    ack=None, cdc=None):
        def perform_sync():
            self.sync_calls += 1
            self.assertEqual(self.gate.state, GateState.OPEN)
            if sync_error:
                raise OSError("disconnect")
            return SyncSummary(copied=1, conflicts=sync_conflicts)

        return self.coordinator.run(
            mount_root="E:\\", cdc_port="COM5",
            device_id=lambda: "test-device", sync=perform_sync,
            gate=self.gate, ack_flush=ack, cdc_mutations=cdc,
        )

    def test_success_order_exactly_one_release(self):
        mutations = []
        outcome = self.run_release(
            ack=lambda: AckFlushResult(handles_closed=True),
            cdc=lambda: mutations.append("SET_TIME-done"),
        )
        self.assertTrue(outcome.success)
        self.assertEqual(self.gate.state, GateState.RELEASED)
        self.assertEqual(self.port.steps, [
            "resolve", "revalidate", "available", "lock",
            "dismount", "release", "observe", "unlock",
        ])
        self.assertEqual(mutations, ["SET_TIME-done"])
        self.assertEqual(self.port.steps.count("release"), 1)
        self.assertIsNotNone(outcome.attempt_id)

    def test_conflict_and_failure_do_not_eject(self):
        for kwargs in ({"sync_conflicts": 1}, {"sync_error": True}):
            with self.subTest(kwargs=kwargs):
                self.setUp()
                outcome = self.run_release(**kwargs)
                self.assertEqual(outcome.status, "SYNC_INTERRUPTED")
                self.assertEqual(self.port.steps, [])

    def test_before_seal_target_failure_never_releases(self):
        for failure in ("target", "revalidate"):
            with self.subTest(failure=failure):
                self.setUp()
                self.port.failure = failure
                outcome = self.run_release()
                self.assertEqual(outcome.reason, "WRONG_TARGET")
                self.assertNotIn("release", self.port.steps)

    def test_already_removed_cannot_be_success(self):
        self.port.present = False
        outcome = self.run_release()
        self.assertEqual(outcome.reason, "ALREADY_REMOVED_UNPROVEN")
        self.assertNotIn("lock", self.port.steps)

    def test_failed_lock_or_dismount_never_sends_cdc(self):
        for failure in ("lock", "dismount"):
            with self.subTest(failure=failure):
                self.setUp()
                self.port.failure = failure
                outcome = self.run_release()
                self.assertEqual(outcome.status, "SYNC_COMPLETE_RELEASE_FAILED")
                self.assertNotIn("release", self.port.steps)
                self.assertEqual(self.gate.state, GateState.SEALED_FOR_RELEASE)

    def test_cdc_rejection_is_not_release_proof(self):
        self.port.failure = "reject"
        outcome = self.run_release()
        self.assertEqual(outcome.reason, "RELEASE_REJECTED")
        self.assertEqual(self.gate.state, GateState.HOST_QUIESCED)
        self.assertNotIn("observe", self.port.steps)

    def test_lost_response_is_indeterminate_not_disappearance_success(self):
        self.port.failure = "indeterminate"
        outcome = self.run_release()
        self.assertEqual(outcome.reason, "INDETERMINATE")
        self.assertEqual(self.port.steps.count("release"), 1)
        self.assertNotIn("observe", self.port.steps)

    def test_deadline_after_acceptance_is_release_failure(self):
        self.port.disappeared = False
        outcome = self.run_release()
        self.assertEqual(outcome.reason, "TIMEOUT")
        self.assertEqual(self.gate.state, GateState.RELEASE_ACCEPTED)
        self.assertEqual(self.port.steps.count("release"), 1)
        self.assertGreaterEqual(self.clock.now, 1.0)

    def test_lease_drain_deadline_is_timeout_without_release(self):
        # Model a separately admitted MSC lease with a deterministic clock.
        # The second clock reading reaches the deadline with the lease active.
        self.coordinator.clock = iter((0.0, 1.0)).__next__
        with self.gate.ordinary("msc"):
            outcome = self.run_release()
            self.assertEqual(self.gate.outstanding_ordinary, 1)
            self.assertEqual(self.gate.state, GateState.SEALED_FOR_RELEASE)

        self.assertEqual(outcome.status, "SYNC_COMPLETE_RELEASE_FAILED")
        self.assertEqual(outcome.reason, "TIMEOUT")
        self.assertEqual(self.port.steps, [
            "resolve", "revalidate", "available",
        ])
        self.assertEqual(self.gate.state, GateState.SEALED_FOR_RELEASE)
        for transport in ("msc", "cdc"):
            with self.subTest(transport=transport):
                with self.assertRaises(GateClosed):
                    with self.gate.ordinary(transport):
                        pass

    def test_post_seal_msc_and_cdc_both_denied(self):
        denied = []
        def attempt_late_io():
            for transport in ("msc", "cdc"):
                with self.assertRaises(GateClosed):
                    with self.gate.ordinary(transport):
                        denied.append(transport)
        self.port.on_quiesce = attempt_late_io
        outcome = self.run_release()
        self.assertTrue(outcome.success)
        self.assertEqual(denied, [])

    def test_ack_missing_is_not_barrier_and_incomplete_handles_are(self):
        outcome = self.run_release()
        self.assertTrue(outcome.success)
        self.setUp()
        outcome = self.run_release(ack=lambda: AckFlushResult(handles_closed=False))
        self.assertEqual(outcome.status, "SYNC_COMPLETE_RELEASE_FAILED")
        self.assertNotIn("lock", self.port.steps)

    def test_cdc_mutation_failure_prevents_lock_and_release(self):
        def fail_mutation():
            raise TimeoutError("SET_TIME response missing")
        outcome = self.run_release(cdc=fail_mutation)
        self.assertEqual(outcome.status, "SYNC_COMPLETE_RELEASE_FAILED")
        self.assertNotIn("resolve", self.port.steps)

    def test_gate_remains_sealed_after_volume_timeout(self):
        self.port.disappeared = False
        self.run_release()
        with self.assertRaises(GateClosed):
            with self.gate.ordinary("msc"):
                pass
        with self.assertRaises(GateClosed):
            with self.gate.ordinary("cdc"):
                pass

    def test_concurrent_revalidation_is_leased_until_drain(self):
        """No host release can pass seal while another run reads device.json."""
        entered = threading.Event()
        resume = threading.Event()
        errors = []
        results = {}

        def on_revalidate():
            if threading.current_thread().name == "slow-revalidation":
                entered.set()
                if not resume.wait(3):
                    raise AssertionError("test revalidation release timed out")

        self.port.on_revalidate = on_revalidate

        def work(label):
            try:
                results[label] = self.coordinator.run(
                    mount_root="E:\\", cdc_port="COM5",
                    device_id=lambda: "test-device",
                    sync=lambda: SyncSummary(copied=1, conflicts=0),
                    gate=self.gate,
                )
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(target=work, args=("first",), name="slow-revalidation")
        second = threading.Thread(target=work, args=("second",), name="sealing-run")
        first.start()
        try:
            self.assertTrue(entered.wait(2), "first run never reached target validation")
            self.assertEqual(self.gate.outstanding_ordinary, 1)
            second.start()
            deadline = time.monotonic() + 2
            while self.gate.state is GateState.OPEN and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(self.gate.state, GateState.SEALED_FOR_RELEASE)
            # The second run is blocked by the first run's tracked MSC lease.
            self.assertEqual(self.gate.outstanding_ordinary, 1)
            self.assertNotIn("lock", self.port.steps)
        finally:
            resume.set()
            first.join(4)
            if second.ident is not None:
                second.join(4)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(results["first"].reason, "GATE_CLOSED")
        self.assertTrue(results["second"].success)
        self.assertEqual(self.port.steps.count("lock"), 1)

    def test_seal_draining_and_control_exclusivity(self):
        gate = UsbSessionIoGate()
        with gate.ordinary("msc"):
            with self.assertRaises(HostQuiesceError):
                gate.seal(timeout_s=0)
        self.assertEqual(gate.state, GateState.SEALED_FOR_RELEASE)
        # Draining after a failed seal does not retroactively grant a barrier.
        with self.assertRaises(GateClosed):
            with gate.release_control():
                pass

        gate = UsbSessionIoGate()
        gate.seal(timeout_s=1)
        with gate.release_control():
            with self.assertRaises(GateClosed):
                with gate.release_control():
                    pass
            gate.host_quiesced()
            gate.accepted()
        gate.released()


class FakeWindowsApi:
    def __init__(self):
        self.guid = TARGET.volume_guid
        self.disks = (8,)
        self.interfaces = [DiskInterface(8, TARGET.disk_instance_id, TARGET.eject_instance_id, TARGET.usb_physical_instance_id)]
        self.accessible = True
        self.mount_point_active = True
        self.calls = []

    def volume_guid(self, mount_root):
        return self.guid

    def volume_disk_numbers(self, guid):
        return self.disks

    def disk_interfaces(self):
        return list(self.interfaces)

    def filesystem_accessible(self, guid):
        return self.accessible

    def disk_instance_present(self, instance_id):
        self.calls.append("pnp-presence")
        return any(
            iface.instance_id.casefold() == instance_id.casefold()
            for iface in self.interfaces
        )

    def mount_point_present(self, mount_root):
        self.calls.append("dos-namespace")
        return self.mount_point_active

    @contextmanager
    def lock_and_dismount(self, guid):
        self.calls.append("lock-dismount")
        yield

    def release_cdc(self, port, attempt):
        self.calls.append("cdc-release")

    def cdc_physical_id(self, port):
        return getattr(self, "cdc_usb_id", TARGET.usb_physical_instance_id)


class WindowsAdapterTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeWindowsApi()
        self.adapter = WindowsHostReleasePort(self.api)

    def test_unique_volume_single_disk_removable_ancestor(self):
        self.assertEqual(self.adapter.resolve_target("E:\\", "test-device"), TARGET)

    def test_reject_ambiguous_or_unsupported_topology(self):
        scenarios = [
            ((8, 9), [DiskInterface(8, "a", "b", TARGET.usb_physical_instance_id)]),
            ((8,), []),
            ((8,), [DiskInterface(8, "a", "b", TARGET.usb_physical_instance_id), DiskInterface(8, "c", "d", TARGET.usb_physical_instance_id)]),
            ((8,), [DiskInterface(8, "a", None, TARGET.usb_physical_instance_id)]),
        ]
        for extents, interfaces in scenarios:
            with self.subTest(extents=extents, interfaces=interfaces):
                self.api.disks = extents
                self.api.interfaces = interfaces
                with self.assertRaises(Exception):
                    self.adapter.resolve_target("E:\\", "test-device")

    def test_changed_guid_disk_devnode_and_device_id_fail_closed(self):
        with patch("m5daylog.windows_release.inspect_device", return_value={"deviceId": "test-device"}):
            for mutate in (
                lambda: setattr(self.api, "guid", "different"),
                lambda: setattr(self.api, "disks", (7,)),
                lambda: setattr(self.api, "interfaces", [DiskInterface(8, "changed", "usb", TARGET.usb_physical_instance_id)]),
            ):
                self.setUp()
                mutate()
                with self.assertRaises(Exception):
                    self.adapter.revalidate_target(TARGET, "COM5")
        self.setUp()
        with patch("m5daylog.windows_release.inspect_device", return_value={"deviceId": "swapped"}):
            with self.assertRaises(WrongTarget):
                self.adapter.revalidate_target(TARGET, "COM5")

    def test_cdc_from_another_usb_device_cannot_release_target(self):
        self.api.cdc_usb_id = "USB\\\\VID_303A&PID_4001\\\\OTHER"
        with patch("m5daylog.windows_release.inspect_device",
                   return_value={"deviceId": "test-device"}):
            with self.assertRaises(WrongTarget):
                self.adapter.revalidate_target(TARGET, "COM5")
        with self.assertRaises(WrongTarget):
            self.adapter.release_storage("COM5", "attempt", TARGET)
        self.assertEqual(self.api.calls, [])

    def test_dismount_is_not_accepted_as_release_disappearance(self):
        self.assertFalse(self.adapter.volume_disappeared(TARGET))
        self.api.accessible = False
        # Locked/dismounted volume is unreadable but the disk still exists.
        self.assertFalse(self.adapter.volume_disappeared(TARGET))
        self.api.interfaces = []
        # Disappearance requires BOTH passive proofs, not only one.
        self.assertFalse(self.adapter.volume_disappeared(TARGET))
        self.api.mount_point_active = False
        self.assertTrue(self.adapter.volume_disappeared(TARGET))

    def test_post_acceptance_observation_never_uses_disk_or_filesystem_io(self):
        # Detect the prior disk_interfaces() CreateFile/IOCTL and
        # GetVolumeInformationW filesystem paths after acceptance.
        with (patch.object(self.api, "disk_interfaces",
                           side_effect=AssertionError("disk I/O after acceptance")),
              patch.object(self.api, "filesystem_accessible",
                           side_effect=AssertionError("filesystem I/O after acceptance"))):
            self.assertFalse(self.adapter.volume_disappeared(TARGET))
            self.api.interfaces = []
            self.assertFalse(self.adapter.volume_disappeared(TARGET))
            self.api.mount_point_active = False
            self.assertTrue(self.adapter.volume_disappeared(TARGET))
        self.assertEqual(
            self.api.calls,
            ["pnp-presence", "pnp-presence", "dos-namespace",
             "pnp-presence", "dos-namespace"],
        )

    def test_native_pnp_observer_never_opens_disk_or_sends_ioctl(self):
        # Exercise the actual NativeWin32DeviceApi observer against Win32 spies,
        # without requiring a real Windows host or removable device.
        import ctypes
        from types import SimpleNamespace
        from m5daylog.windows_release import NativeWin32DeviceApi, SP_DEVINFO_DATA

        present = {"value": True}

        def prohibited_device_io(*_args):
            raise AssertionError("target-directed Win32 disk I/O after acceptance")

        def enum_interfaces(_devs, _data, _guid, index, _out):
            return present["value"] and index == 0

        def details(_devs, _interface, _buffer, _length, _needed, info):
            ctypes.cast(
                info, ctypes.POINTER(SP_DEVINFO_DATA)
            ).contents.DevInst = 7
            return True

        api = NativeWin32DeviceApi.__new__(NativeWin32DeviceApi)
        api.k = SimpleNamespace(
            CreateFileW=prohibited_device_io,
            DeviceIoControl=prohibited_device_io,
            QueryDosDeviceW=lambda *_args: 0,
        )
        api.s = SimpleNamespace(
            SetupDiGetClassDevsW=lambda *_args: 4,
            SetupDiEnumDeviceInterfaces=enum_interfaces,
            SetupDiGetDeviceInterfaceDetailW=details,
            SetupDiDestroyDeviceInfoList=lambda *_args: True,
        )
        api._instance_id = lambda _devinst: TARGET.disk_instance_id
        observer = WindowsHostReleasePort(api)

        self.assertFalse(observer.volume_disappeared(TARGET))
        present["value"] = False
        # 259: end of PnP enumeration; 2: DOS-drive link no longer found.
        with patch("m5daylog.windows_release.ctypes.get_last_error",
                   create=True, side_effect=[259, 2]):
            self.assertTrue(observer.volume_disappeared(TARGET))

    def test_no_other_volume_is_chosen_on_bad_mount(self):
        for root in ("F:\\M5DAYLOG", ".", "E:", "\\\\server\\share"):
            with self.subTest(root=root), self.assertRaises(Exception):
                self.adapter.resolve_target(root, "test-device")


if __name__ == "__main__":
    unittest.main()
