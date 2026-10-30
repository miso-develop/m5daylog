"""R3 pure/synthetic candidate validation; no real volume or CDC access."""

import unittest
from unittest.mock import patch

from m5daylog.capacity import VerifiedSession
from m5daylog.release import EjectTarget, PreconditionBlocked
from m5daylog.release_cycle_candidate import (
    _cycle_from_documents, inspect_release_cycle_candidate,
)

DEVICE = "00000000-0000-4000-8000-000000000001"
CYCLE = "aaaaaaaa-0000-4000-8000-000000000002"
TARGET = EjectTarget(
    DEVICE, "E:\\",
    "\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\",
    8, "disk", "removable", "usb-parent",
)
SESSION = VerifiedSession(TARGET, "COM5", "synthetic-host-epoch")


def recording(**fields):
    result = {
        "recordingId": CYCLE, "state": "finalized",
        "durationMs": 1000, "sizeBytes": 32044,
        "sampleRate": 16000, "bitDepth": 16, "channels": 1,
        "sha256": "a" * 64,
    }
    result.update(fields)
    return result


def evidence(**status_fields):
    status = {
        "state": "USB_SYNC", "rtcCorrectionPending": False,
        "releaseCycleId": CYCLE,
    }
    status.update(status_fields)
    return status


class Port:
    def __init__(self):
        self.events = []
        self.available = True

    def revalidate_target(self, target, cdc):
        self.events.append("binding")
        if target != TARGET or cdc != "COM5":
            raise ValueError("mismatched exact target")

    def volume_available(self, target):
        self.events.append("volume")
        return self.available


class CandidateTests(unittest.TestCase):
    def check(self, status=None, recs=None):
        records = recs if recs is not None else [recording()]
        return _cycle_from_documents(
            status if status is not None else evidence(),
            {"deviceId": DEVICE},
            {"deviceId": DEVICE, "recordings": records},
            [(item, object()) for item in records],
        )

    def test_valid_candidate_is_not_a_release_receipt(self):
        self.assertEqual(self.check(), (DEVICE, CYCLE))

    def test_fail_closed_on_missing_unqualified_duplicate_or_recovered(self):
        invalid = [
            (evidence(releaseCycleId=None), None),
            (evidence(releaseCycleId=CYCLE.upper()), None),
            (evidence(state="NORMAL"), None),
            (evidence(rtcCorrectionPending="false"), None),
            (None, [recording(recordingId=DEVICE)]),
            (None, [recording(), recording()]),
            (None, [recording(state="recovered")]),
            (None, [recording(durationMs=0)]),
            (None, [recording(sizeBytes=44)]),
            (None, [recording(sampleRate=8000)]),
            (None, [recording(bitDepth=8)]),
            (None, [recording(channels=2)]),
        ]
        for status, records in invalid:
            with self.subTest(status=status, records=records):
                with self.assertRaises(PreconditionBlocked):
                    self.check(status=status, recs=records)

    def test_exact_identity_is_revalidated_before_and_after_status(self):
        port = Port()
        with patch("m5daylog.release_cycle_candidate.inspect_device",
                   return_value={"deviceId": DEVICE}), \
             patch("m5daylog.release_cycle_candidate._load_manifest",
                   return_value={"deviceId": DEVICE}), \
             patch("m5daylog.release_cycle_candidate._preflight",
                   return_value=[(recording(), object())]):
            result = inspect_release_cycle_candidate(
                session=SESSION, port=port, status_reader=lambda _: evidence(),
                current_session_epoch=lambda: SESSION.session_epoch,
            )
        self.assertEqual(result.release_cycle_id, CYCLE)
        self.assertEqual(port.events, ["binding", "volume", "binding", "volume"])

    def test_epoch_change_during_get_status_is_rejected(self):
        port = Port()
        observed = ["synthetic-host-epoch"]
        def status(_):
            observed[0] = "new-observation"
            return evidence()
        with patch("m5daylog.release_cycle_candidate.inspect_device",
                   return_value={"deviceId": DEVICE}), \
             patch("m5daylog.release_cycle_candidate._load_manifest",
                   return_value={"deviceId": DEVICE}), \
             patch("m5daylog.release_cycle_candidate._preflight",
                   return_value=[(recording(), object())]):
            with self.assertRaises(PreconditionBlocked):
                inspect_release_cycle_candidate(
                    session=SESSION, port=port, status_reader=status,
                    current_session_epoch=lambda: observed[0],
                )

    def test_stale_epoch_denied_before_any_media_read(self):
        with patch("m5daylog.release_cycle_candidate.inspect_device") as reader:
            with self.assertRaises(PreconditionBlocked):
                inspect_release_cycle_candidate(
                    session=SESSION, port=Port(),
                    status_reader=lambda _: evidence(),
                    current_session_epoch=lambda: "stale",
                )
            reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
