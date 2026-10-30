"""#135 Windows observer and opt-in tests with synthetic Win32 ports only."""

import json
import tempfile
import unittest
from pathlib import Path

from m5daylog.windows_auto_intake import WindowsArrivalProvider, read_opt_in
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
