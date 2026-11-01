"""#135 Windows observer and opt-in tests with synthetic Win32 ports only."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from m5daylog.auto_intake import IntakeResult, PollingMonitor
from m5daylog.windows_auto_intake import (
    WindowsArrivalProvider, enumerate_com_ports, read_opt_in,
)
from m5daylog.windows_release import CdcInterface
from m5daylog.release import EjectTarget

DEVICE = "00000000-0000-4000-8000-000000000135"
TARGET = EjectTarget(
    DEVICE, "E:\\", "\\\\?\\Volume{00000000-0000-0000-0000-000000000135}\\",
    1, "disk", "removable", "parent",
)


class StubWin32:
    def __init__(self):
        self.calls = []
        self.mapping = {
            "COM7": CdcInterface("synthetic-path", "cdc7", "parent"),
            "COM8": CdcInterface("other-path", "cdc8", "other-parent"),
        }

    def cdc_interface(self, name):
        self.calls.append(name)
        if name not in self.mapping:
            raise OSError("no such synthetic port")
        return self.mapping[name]


class StubReleasePort:
    contracts_dir = None

    def __init__(self):
        self.calls = []
        self.present = True

    def resolve_target(self, root, device_id):
        self.calls.append("resolve")
        if root != "E:\\" or device_id != DEVICE:
            raise ValueError("wrong root")
        return TARGET

    def revalidate_target(self, target, cdc):
        self.calls.append("revalidate")
        if target != TARGET or cdc != "COM7":
            raise ValueError("wrong CDC")

    def volume_available(self, target):
        self.calls.append("available")
        return self.present


class SyntheticSerialRegistry:
    """Fake Win32 EnumValue with explicit EOF, partial I/O errors and a cap."""

    HKEY_LOCAL_MACHINE = object()

    def __init__(self, values):
        self.values = list(values)
        self.fail_at = None
        self.fail_winerror = 5

    def OpenKey(self, hive, path):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def EnumValue(self, key, index):
        if index == self.fail_at:
            error = OSError("synthetic non-EOF registry error")
            error.winerror = self.fail_winerror
            raise error
        if index >= len(self.values):
            error = OSError("synthetic end of registry enumeration")
            error.winerror = 259  # ERROR_NO_MORE_ITEMS
            raise error
        return (f"serial{index}", self.values[index], 1)


class RecordingIntake:
    def __init__(self):
        self.sessions = []

    def _authorized(self, arrival):
        return arrival.usb_sync_confirmed

    def intake(self, arrival, *, current_session_epoch):
        self.sessions.append((
            arrival.binding.session_epoch, current_session_epoch(),
        ))
        return IntakeResult("RELEASE_BLOCKED", "AUTO_RELEASE_NOT_QUALIFIED")


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.api = StubWin32()
        self.port = StubReleasePort()
        self.status = {"state": "USB_SYNC", "rtcCorrectionPending": False}
        self.device_id = DEVICE
        self.coms = ["COM7", "COM8"]
        self.provider = WindowsArrivalProvider(
            trusted=frozenset({DEVICE}), api=self.api, port=self.port,
            roots=lambda: ["E:\\"], ports=lambda: self.coms,
            status=lambda com: dict(self.status),
            preflight=lambda root: self.device_id,
        )

    def test_one_authorized_exact_session_and_stable_epoch(self):
        first = self.provider.scan()
        second = self.provider.scan()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].binding.target, TARGET)
        self.assertTrue(first[0].usb_sync_confirmed)
        self.assertEqual(first[0].binding.session_epoch,
                         second[0].binding.session_epoch)
        self.assertEqual(self.provider.current_epoch(),
                         first[0].binding.session_epoch)

    def test_unknown_device_and_non_usb_sync_never_emits_arrival(self):
        self.device_id = "untrusted"
        self.assertEqual(self.provider.scan(), [])
        self.assertEqual(self.port.calls, [])
        self.device_id = DEVICE
        for bad in ("RECORDING", "SHUTDOWN_ARMED", "HOST_UNRESOLVED", None):
            with self.subTest(state=bad):
                self.status["state"] = bad
                self.assertEqual(self.provider.scan(), [])

    def test_ambiguous_cdc_binding_or_missing_target_denied(self):
        self.api.mapping["COM8"] = CdcInterface("synthetic-second", "cdc8", "parent")
        self.assertEqual(self.provider.scan(), [])
        self.api.mapping["COM8"] = CdcInterface("synthetic-second", "cdc8", "other-parent")
        self.port.present = False
        self.assertEqual(self.provider.scan(), [])

    def test_disappearance_invalidates_epoch(self):
        earlier = self.provider.scan()[0].binding.session_epoch
        self.status["state"] = "RECORDING"
        self.assertEqual(self.provider.scan(), [])
        self.status["state"] = "USB_SYNC"
        later = self.provider.scan()[0].binding.session_epoch
        self.assertNotEqual(earlier, later)

    def test_enumeration_failure_revokes_epoch_before_polling_recovery(self):
        # Real Provider.scan + PollingMonitor.poll_once path, with synthetic
        # Windows endpoints only. No physical USB/CDC or release sender.
        class Coordinator:
            def __init__(self):
                self.sessions = []

            def _authorized(self, arrival):
                return arrival.usb_sync_confirmed

            def intake(self, arrival, *, current_session_epoch):
                self.sessions.append((
                    arrival.binding.session_epoch, current_session_epoch(),
                ))
                return IntakeResult("RELEASE_BLOCKED", "AUTO_RELEASE_NOT_QUALIFIED")

        coordinator = Coordinator()
        notices = []
        monitor = PollingMonitor(
            self.provider, coordinator, self.provider.current_epoch,
            notices.append,
        )
        monitor.poll_once()
        self.assertEqual(coordinator.sessions, [])
        monitor.poll_once()
        self.assertEqual(len(coordinator.sessions), 1)
        initial_epoch = coordinator.sessions[-1][0]

        # COM enumeration failure must not preserve an old observer epoch,
        # even when every PnP/mount key appears identical on the next scan.
        def fail_ports():
            raise OSError("synthetic serial enumeration failure")

        self.provider.ports = fail_ports
        monitor.poll_once()
        self.assertEqual(self.provider.current_epoch(), "")
        self.assertEqual(len(coordinator.sessions), 1)
        self.provider.ports = lambda: list(self.coms)
        monitor.poll_once()
        self.assertEqual(len(coordinator.sessions), 1)
        monitor.poll_once()
        self.assertEqual(len(coordinator.sessions), 2)
        after_ports = coordinator.sessions[-1][0]
        self.assertNotEqual(initial_epoch, after_ports)

        # A drive iterator may fail after yielding a plausible first root.
        # Its partial enumeration must not retain or reissue an old epoch.
        def fail_roots():
            yield "E:\\"
            raise OSError("synthetic removable-root enumeration failure")

        self.provider.roots = fail_roots
        monitor.poll_once()
        self.assertEqual(self.provider.current_epoch(), "")
        self.assertEqual(len(coordinator.sessions), 2)
        self.provider.roots = lambda: ["E:\\"]
        monitor.poll_once()
        self.assertEqual(len(coordinator.sessions), 2)
        monitor.poll_once()
        self.assertEqual(len(coordinator.sessions), 3)
        self.assertNotEqual(after_ports, coordinator.sessions[-1][0])
        self.assertTrue(all(stored == current for stored, current in coordinator.sessions))
        self.assertTrue(all(n["safe_to_leave_usb_connected"] is False
                            for n in notices))
        self.assertIn(
            "ARRIVAL_OBSERVATION_ERROR",
            [n["reason"] for n in notices],
        )

    def test_actual_registry_partial_failure_denies_ambiguous_binding_and_rearms(self):
        # Exercise the production EnumValue -> Provider.scan -> Monitor path.
        # A partial COM list must never make two same-parent CDCs look unique.
        registry = SyntheticSerialRegistry(["COM7", "COM8"])
        self.provider.ports = enumerate_com_ports
        coordinator = RecordingIntake()
        notices = []
        monitor = PollingMonitor(
            self.provider, coordinator, self.provider.current_epoch,
            notices.append,
        )
        with mock.patch.dict(sys.modules, {"winreg": registry}):
            monitor.poll_once()
            monitor.poll_once()
            self.assertEqual(len(coordinator.sessions), 1)
            old_epoch = coordinator.sessions[-1][0]

            self.api.mapping["COM8"] = CdcInterface(
                "synthetic-second", "cdc8", "parent",
            )
            registry.fail_at = 1  # COM7 returned; COM8 hidden by WinError 5
            monitor.poll_once()
            self.assertEqual(self.provider.current_epoch(), "")
            self.assertEqual(len(coordinator.sessions), 1)
            self.assertEqual(notices[-1]["reason"], "ARRIVAL_OBSERVATION_ERROR")

            registry.fail_at = None  # complete, genuinely ambiguous enumeration
            monitor.poll_once()
            self.assertEqual(self.provider.current_epoch(), "")
            self.assertEqual(len(coordinator.sessions), 1)

            self.api.mapping["COM8"] = CdcInterface(
                "other-path", "cdc8", "other-parent",
            )
            monitor.poll_once()  # only the first stable recovered observation
            self.assertEqual(len(coordinator.sessions), 1)
            monitor.poll_once()
            self.assertEqual(len(coordinator.sessions), 2)
            self.assertNotEqual(old_epoch, coordinator.sessions[-1][0])
        self.assertTrue(all(
            stored == current for stored, current in coordinator.sessions
        ))
        self.assertTrue(all(
            notice["safe_to_leave_usb_connected"] is False
            for notice in notices
        ))

    def test_actual_registry_cap_requires_eof_not_a_truncated_subset(self):
        registry = SyntheticSerialRegistry(["COM7", "COM8"])
        self.provider.ports = enumerate_com_ports
        coordinator = RecordingIntake()
        notices = []
        monitor = PollingMonitor(
            self.provider, coordinator, self.provider.current_epoch,
            notices.append,
        )
        with mock.patch.dict(sys.modules, {"winreg": registry}):
            self.assertEqual(enumerate_com_ports(), ["COM7", "COM8"])
            monitor.poll_once()
            monitor.poll_once()
            self.assertEqual(len(coordinator.sessions), 1)
            old_epoch = coordinator.sessions[-1][0]

            registry.values = ["COM7"] + ["COM8"] * 128
            monitor.poll_once()  # 129th registry entry proves the cap truncates
            self.assertEqual(self.provider.current_epoch(), "")
            self.assertEqual(len(coordinator.sessions), 1)
            self.assertEqual(notices[-1]["reason"], "ARRIVAL_OBSERVATION_ERROR")

            registry.values = ["COM7"] + ["COM8"] * 127
            self.assertEqual(enumerate_com_ports(), ["COM7", "COM8"])
            monitor.poll_once()
            self.assertEqual(len(coordinator.sessions), 1)
            monitor.poll_once()
            self.assertEqual(len(coordinator.sessions), 2)
            self.assertNotEqual(old_epoch, coordinator.sessions[-1][0])
        self.assertTrue(all(
            notice["safe_to_leave_usb_connected"] is False
            for notice in notices
        ))

    def test_invalid_status_response_does_not_admit(self):
        for value in (None, {}, {"state": "USB_SYNC"}, {"state": "USB_SYNC",
                        "rtcCorrectionPending": "false"}):
            with self.subTest(value=value):
                self.provider.status = lambda name, value=value: value
                self.assertEqual(self.provider.scan(), [])


class ConsentTests(unittest.TestCase):
    def test_disabled_missing_or_bad_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.json"
            with self.assertRaises(ValueError):
                read_opt_in(path)
            for invalid in (
                {}, {"enabled": False, "trustedDeviceIds": [DEVICE]},
                {"enabled": True, "trustedDeviceIds": []},
                {"enabled": True, "trustedDeviceIds": ["not-a-uuid"]},
                {"enabled": True, "trustedDeviceIds": [DEVICE, DEVICE]},
            ):
                with self.subTest(invalid=invalid):
                    path.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        read_opt_in(path)
            path.write_text(json.dumps({
                "enabled": True, "trustedDeviceIds": [DEVICE],
            }), encoding="utf-8")
            self.assertEqual(read_opt_in(path), frozenset({DEVICE}))


if __name__ == "__main__":
    unittest.main()
