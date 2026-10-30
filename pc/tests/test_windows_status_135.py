"""#135 fake Win32 CDC GET_STATUS transport; never opens a real COM port."""

import ctypes
import json
import unittest
from ctypes import wintypes

from m5daylog.release import HostQuiesceError
from m5daylog.windows_release import CdcInterface, NativeWin32DeviceApi


class FakeKernel:
    def __init__(self, mode="valid"):
        self.mode = mode
        self.request = None
        self.reply = b""
        self.pos = 0
        self.closed = 0

    def SetCommTimeouts(self, handle, settings):
        return 1

    def WriteFile(self, handle, buf, count, written, overlapped):
        self.request = json.loads(ctypes.string_at(buf, count).decode("ascii"))
        response = {
            "id": self.request["id"], "ok": True,
            "result": {"state": "USB_SYNC", "rtcCorrectionPending": False},
        }
        if self.mode == "wrong-id":
            response["id"] = "unrelated"
        elif self.mode == "invalid-pending":
            response["result"]["rtcCorrectionPending"] = "false"
        elif self.mode == "not-ok":
            response["ok"] = False
        elif self.mode == "missing-state":
            response["result"].pop("state")
        self.reply = (json.dumps(response) + "\n").encode("ascii")
        ctypes.cast(written, ctypes.POINTER(wintypes.DWORD)).contents.value = count
        return 1

    def ReadFile(self, handle, out, count, received, overlapped):
        assert count == 1
        assert self.pos < len(self.reply)
        ctypes.memmove(out, self.reply[self.pos:self.pos + 1], 1)
        self.pos += 1
        ctypes.cast(received, ctypes.POINTER(wintypes.DWORD)).contents.value = 1
        return 1

    def CloseHandle(self, handle):
        self.closed += 1
        return 1


class ReadOnlyStatusTests(unittest.TestCase):
    def make_api(self, mode="valid"):
        kernel = FakeKernel(mode)
        api = object.__new__(NativeWin32DeviceApi)
        api.k = kernel
        endpoint = CdcInterface("synthetic-cdc-interface", "cdc7", "parent")
        api._cdc_interface = lambda port: endpoint
        api._open = lambda path, access, sharing: 7
        return api, kernel

    def test_status_uses_read_only_command_and_checks_envelope(self):
        api, k = self.make_api()
        result = api.read_status("COM7")
        self.assertEqual(
            result, {"state": "USB_SYNC", "rtcCorrectionPending": False},
        )
        self.assertEqual(k.request["cmd"], "GET_STATUS")
        self.assertEqual(k.request["args"], {})
        self.assertEqual(k.closed, 1)

    def test_invalid_status_is_not_usb_sync_proof(self):
        for mode in ("wrong-id", "invalid-pending", "not-ok", "missing-state"):
            with self.subTest(mode=mode):
                api, k = self.make_api(mode)
                with self.assertRaises(HostQuiesceError):
                    api.read_status("COM7")
                self.assertEqual(k.closed, 1)
                self.assertEqual(k.request["cmd"], "GET_STATUS")

    def test_changed_endpoint_is_never_written(self):
        from m5daylog.release import WrongTarget

        api, k = self.make_api()
        first = CdcInterface("synthetic-cdc-A", "cdc-A", "parent")
        second = CdcInterface("synthetic-cdc-B", "cdc-B", "parent")
        bindings = iter([first, second])
        api._cdc_interface = lambda port: next(bindings)
        with self.assertRaises(WrongTarget):
            api.read_status("COM7")
        self.assertIsNone(k.request)
        self.assertEqual(k.closed, 1)


if __name__ == "__main__":
    unittest.main()
