"""#143 R5-FU source-only negative matrix. Synthetic IDs and no Device/CDC I/O."""

from __future__ import annotations

import unittest
from dataclasses import replace

from m5daylog.first_use_admission import (
    CycleOrigin, HostHistory, InitialBootObservation, InitialIntent,
    MaintenanceReceipt, PriorEnrollment, inspect_first_use_admission,
    parse_cycle_origin,
)
from m5daylog.release import PreconditionBlocked


DEVICE = "00000000-0000-4000-8000-000000000001"
OTHER_DEVICE = "00000000-0000-4000-8000-000000000002"
ATTEMPT = "00000000-0000-4000-8000-000000000003"
OTHER_ATTEMPT = "00000000-0000-4000-8000-000000000004"
CYCLE = "aaaaaaaa-0000-4000-8000-000000000005"


class FirstUseContractTests(unittest.TestCase):
    def setUp(self):
        self.status = {
            "state": "USB_SYNC", "rtcCorrectionPending": False,
            "releaseCycleId": CYCLE,
            "releaseCycleOrigin": "SUPERVISED_FIRST_USE",
        }
        self.history = HostHistory(
            PriorEnrollment.VERIFIED_NEVER_ENROLLED, False, False, True
        )
        self.intent = InitialIntent(
            DEVICE, "pre-parent", "pre-ownership-session", ATTEMPT,
            exact_human_permit_verified=True,
            device_physical_stage_verified=True,
            running_handler_verified=True,
            original_media_preserved=True,
        )
        self.maintenance = MaintenanceReceipt(
            DEVICE, "pre-parent", "pre-ownership-session", ATTEMPT,
            accepted_response_verified=True,
            exact_target_disappeared=True,
            device_shutdown_armed_verified=True,
        )
        self.boot = InitialBootObservation(
            DEVICE, "current-parent", "post-release-new-session",
            cross_session_custody_verified=True,
            first_boot_transition_verified=True,
            current_run_terminal_pcm_verified=True,
            raw_wav_sync_durable=True,
            rtc_validity_verified=True,
            exact_target_verified=True,
        )

    def assess(self, **changes):
        arguments = {
            "status": self.status, "history": self.history,
            "intent": self.intent, "maintenance": self.maintenance,
            "boot": self.boot,
        }
        arguments.update(changes)
        return inspect_first_use_admission(**arguments)

    def test_qualified_first_use_yields_only_planning_tuple(self):
        result = self.assess()
        self.assertEqual(
            (result.device_id, result.release_cycle_id,
             result.maintenance_attempt_id),
            (DEVICE, CYCLE, ATTEMPT),
        )
        # In particular: no "send", "enroll", "consume", or "authorize" method.
        self.assertFalse(hasattr(result, "release_storage"))
        self.assertFalse(hasattr(result, "reserve_locked"))

    def test_strict_origin_never_promotes_legacy_or_unknown_status(self):
        self.assertIs(
            parse_cycle_origin({"releaseCycleOrigin": "NORMAL_WAKE"}),
            CycleOrigin.NORMAL_WAKE,
        )
        invalid = [
            {}, {"releaseCycleOrigin": None},
            {"releaseCycleOrigin": "normal_wake"},
            {"releaseCycleOrigin": "SUPERVISED_FIRST_USE "},
            {"releaseCycleOrigin": []},
            {"releaseCycleOrigin": 1},
        ]
        for status in invalid:
            with self.subTest(status=status):
                with self.assertRaises(PreconditionBlocked):
                    parse_cycle_origin(status)
                with self.assertRaises(PreconditionBlocked):
                    self.assess(status=status)
        for origin in ("NORMAL_WAKE", "normal_wake", None):
            with self.subTest(origin=origin):
                with self.assertRaises(PreconditionBlocked):
                    self.assess(status={**self.status, "releaseCycleOrigin": origin})

    def test_existing_enrollment_corruption_and_store_absence_block(self):
        invalid = [
            HostHistory(PriorEnrollment.ALREADY_ENROLLED, True, True, False),
            HostHistory(PriorEnrollment.ALREADY_ENROLLED, False, False, True),
            HostHistory(PriorEnrollment.INDETERMINATE, False, False, True),
            HostHistory(PriorEnrollment.VERIFIED_NEVER_ENROLLED, False, False),
            HostHistory(PriorEnrollment.VERIFIED_NEVER_ENROLLED, True, False, True),
            HostHistory(PriorEnrollment.VERIFIED_NEVER_ENROLLED, False, True, True),
            HostHistory(PriorEnrollment.VERIFIED_NEVER_ENROLLED, True, True, True),
        ]
        for history in invalid:
            with self.subTest(history=history):
                with self.assertRaises(PreconditionBlocked):
                    self.assess(history=history)

    def test_consumed_stale_or_unpermitted_intent_never_restages(self):
        for state in ("CONSUMED", "FAILED", "INDETERMINATE", "", "FU_STAGED"):
            with self.subTest(state=state):
                with self.assertRaises(PreconditionBlocked):
                    self.assess(intent=replace(self.intent, state=state))
        for flag in (
            "exact_human_permit_verified", "device_physical_stage_verified",
            "running_handler_verified", "original_media_preserved",
        ):
            for value in (False, None, 1):
                with self.subTest(flag=flag, value=value):
                    with self.assertRaises(PreconditionBlocked):
                        self.assess(intent=replace(self.intent, **{flag: value}))

    def test_maintenance_and_attempt_must_match_exact_old_session(self):
        for mutated in (
            replace(self.maintenance, device_id=OTHER_DEVICE),
            replace(self.maintenance, maintenance_attempt_id=OTHER_ATTEMPT),
            replace(self.maintenance, physical_parent="wrong-parent"),
            replace(self.maintenance, maintenance_session="wrong-session"),
        ):
            with self.subTest(mutated=mutated):
                with self.assertRaises(PreconditionBlocked):
                    self.assess(maintenance=mutated)
        for flag in (
            "accepted_response_verified", "exact_target_disappeared",
            "device_shutdown_armed_verified",
        ):
            with self.subTest(flag=flag):
                with self.assertRaises(PreconditionBlocked):
                    self.assess(maintenance=replace(self.maintenance, **{flag: False}))
        with self.assertRaises(PreconditionBlocked):
            self.assess(intent=replace(self.intent, maintenance_attempt_id=OTHER_ATTEMPT))

    def test_new_boot_custody_and_real_wav_require_independent_proof(self):
        flags = (
            "cross_session_custody_verified", "first_boot_transition_verified",
            "current_run_terminal_pcm_verified", "raw_wav_sync_durable",
            "rtc_validity_verified", "exact_target_verified",
        )
        for flag in flags:
            for value in (False, None):
                with self.subTest(flag=flag, value=value):
                    with self.assertRaises(PreconditionBlocked):
                        self.assess(boot=replace(self.boot, **{flag: value}))
        for bad_boot in (
            replace(self.boot, device_id=OTHER_DEVICE),
            replace(self.boot, new_host_session="pre-ownership-session"),
            replace(self.boot, current_physical_parent=""),
            replace(self.boot, new_host_session="line\nbreak"),
        ):
            with self.subTest(bad_boot=bad_boot):
                with self.assertRaises(PreconditionBlocked):
                    self.assess(boot=bad_boot)

    def test_rtc_and_status_must_be_qualified(self):
        for bad in (
            {"state": "NORMAL"},
            {"state": "WAKE_RECOVERY_PENDING"},
            {"rtcCorrectionPending": True},
            {"rtcCorrectionPending": "false"},
            {"rtcCorrectionPending": None},
            {"releaseCycleId": None},
            {"releaseCycleId": CYCLE.upper()},
            {"releaseCycleId": OTHER_DEVICE + " "},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(PreconditionBlocked):
                    self.assess(status={**self.status, **bad})
        with self.assertRaises(PreconditionBlocked):
            self.assess(boot=replace(self.boot, rtc_validity_verified=False))

    def test_no_source_mutations_or_inferred_new_enrollment(self):
        previous_status = dict(self.status)
        previous_history = self.history
        previous_intent = self.intent
        self.assess()
        self.assertEqual(self.status, previous_status)
        self.assertEqual(self.history, previous_history)
        self.assertEqual(self.intent, previous_intent)
        # A new process with absent stores but without the independently
        # retained one-use receipt is not thereby a fresh host.
        with self.assertRaises(PreconditionBlocked):
            self.assess(history=replace(
                self.history, independently_verified_never_enrolled=False
            ))


if __name__ == "__main__":
    unittest.main()
