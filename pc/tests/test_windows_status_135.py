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
        if self.request["cmd"] == "SET_TIME":
            response["result"] = {
                "time": self.request["args"]["time"], "eventPending": True,
            }
            if self.mode == "event-not-pending":
                response["result"]["eventPending"] = False
            if self.mode == "wrong-time":
                response["result"]["time"] = "2026-01-01T00:00:00Z"
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



class RtcAdapterTests(unittest.TestCase):
    make_api = ReadOnlyStatusTests.make_api
    """#50 RTC write frame is synthetic; no native COM connection is opened."""

    UTC = "2026-10-30T10:00:00Z"

    def frozen(self):
        return CdcInterface("synthetic-cdc-interface", "cdc7", "parent")

    def test_qualified_time_event_is_required(self):
        api, kernel = self.make_api()
        api.set_time("COM7", self.frozen(), self.UTC)
        self.assertEqual(kernel.request["cmd"], "SET_TIME")
        self.assertEqual(kernel.request["args"], {"time": self.UTC})
        self.assertEqual(kernel.closed, 1)

    def test_no_write_if_rtc_input_or_endpoint_invalid(self):
        from m5daylog.release import WrongTarget, ReleaseRejected
        for candidate in ("2026-10-30T10:00:00+00:00",
                          "2026-02-30T10:00:00Z", "invalid", ""):
            with self.subTest(value=candidate):
                api, kernel = self.make_api()
                with self.assertRaises(ReleaseRejected):
                    api.set_time("COM7", self.frozen(), candidate)
                self.assertIsNone(kernel.request)
        api, kernel = self.make_api()
        with self.assertRaises(WrongTarget):
            api.set_time(
                "COM7", CdcInterface("another", "changed", "parent"), self.UTC
            )
        self.assertIsNone(kernel.request)

    def test_rejected_and_uncertain_rtc_never_prove_completion(self):
        from m5daylog.release import ReleaseIndeterminate, ReleaseRejected
        for mode, error in (
            ("not-ok", ReleaseRejected),
            ("wrong-id", ReleaseIndeterminate),
            ("wrong-time", ReleaseIndeterminate),
            ("event-not-pending", ReleaseIndeterminate),
        ):
            with self.subTest(mode=mode):
                api, kernel = self.make_api(mode)
                with self.assertRaises(error):
                    api.set_time("COM7", self.frozen(), self.UTC)
                self.assertEqual(kernel.closed, 1)

if __name__ == "__main__":
    unittest.main()
