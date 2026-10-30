"""R3 pre-RTC reservation tests; fake Device/Windows port only."""

from contextlib import contextmanager
import unittest

from m5daylog.release import (
    EjectTarget, PreconditionBlocked, ReleaseIndeterminate,
    SyncReleaseCoordinator, UsbSessionIoGate,
)
from m5daylog.sync import SyncSummary


TARGET = EjectTarget(
    "test-device", "E:\\",
    "\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\",
    8, "disk", "removable", "usb-parent",
)


class FakePort:
    def __init__(self):
        self.events = []

    def resolve_target(self, mount_root, device_id):
        self.events.append("resolve")
        return TARGET

    def revalidate_target(self, target, cdc_port):
        assert target == TARGET
        self.events.append("revalidate")

    def volume_available(self, target):
        self.events.append("available")
        return True

    @contextmanager
    def quiesce(self, target):
        self.events.extend(("lock", "dismount"))
        yield
        self.events.append("unlock")

    def release_storage(self, cdc_port, attempt_id, target):
        self.events.append("release")

    def volume_disappeared(self, target):
        self.events.append("disappeared")
        return True


class PreRtcReservationTests(unittest.TestCase):
    def setUp(self):
        self.port = FakePort()

    def run_coordinator(self, **hooks):
        return SyncReleaseCoordinator(self.port).run(
            mount_root="E:\\", cdc_port="COM5",
            device_id=lambda: "test-device",
            sync=lambda: SyncSummary(copied=1),
            gate=UsbSessionIoGate(), **hooks,
        )

    def test_reservation_precedes_rtc_and_release(self):
        events = self.port.events
        result = self.run_coordinator(
            before_cdc_mutations=lambda target: events.append("reserve"),
            cdc_mutations=lambda: events.append("rtc"),
            before_release_send=lambda attempt_id, target: events.append("verify"),
        )
        self.assertTrue(result.success)
        self.assertLess(events.index("revalidate"), events.index("reserve"))
        self.assertLess(events.index("reserve"), events.index("rtc"))
        self.assertLess(events.index("rtc"), events.index("lock"))
        self.assertLess(events.index("dismount"), events.index("verify"))
        self.assertLess(events.index("verify"), events.index("release"))
        self.assertEqual(events.count("release"), 1)

    def test_denied_reservation_never_mutates(self):
        def deny(_target):
            raise PreconditionBlocked("cycle already reserved")
        result = self.run_coordinator(
            before_cdc_mutations=deny,
            cdc_mutations=lambda: self.port.events.append("rtc"),
        )
        self.assertEqual(result.reason, "PRECONDITION_BLOCKED")
        for step in ("rtc", "lock", "release"):
            self.assertNotIn(step, self.port.events)

    def test_uncertain_rtc_after_reservation_never_releases(self):
        def rtc():
            raise ReleaseIndeterminate("lost SET_TIME reply")
        result = self.run_coordinator(
            before_cdc_mutations=lambda target: self.port.events.append("reserve"),
            cdc_mutations=rtc,
        )
        self.assertEqual(result.reason, "INDETERMINATE")
        self.assertIn("reserve", self.port.events)
        self.assertNotIn("lock", self.port.events)
        self.assertNotIn("release", self.port.events)


if __name__ == "__main__":
    unittest.main()
