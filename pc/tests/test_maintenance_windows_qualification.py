"""#126: production pure-code, synthetic-only Windows qualification checks."""
from __future__ import annotations

from dataclasses import replace
import unittest

from m5daylog.maintenance_windows_qualification import (
    Assessment, AttemptIdentity, SnapshotEvent, SyntheticGuard, TargetIdentity,
    Verdict, assess_synthetic_snapshot,
)


A = "a" * 64
B = "b" * 64
ATTEMPT = "9b53a6e0-6ac5-4a29-a743-6f6875e8790c"


def target() -> TargetIdentity:
    return TargetIdentity(
        "synthetic-device", "synthetic-volume-guid", 3,
        "synthetic-disk-instance", "synthetic-removable-instance",
        "synthetic-physical-usb", "synthetic-cdc-instance",
    )


def events() -> tuple[SnapshotEvent, ...]:
    stages = (
        "preflight", "target_freeze", "io_seal", "volume_lock",
        "volume_dismount", "post_lock_witness",
    )
    return tuple(SnapshotEvent(
        step, target(), A, "synthetic-lock" if i >= 3 else "",
    ) for i, step in enumerate(stages))


def attempt() -> AttemptIdentity:
    return AttemptIdentity(119, ATTEMPT, A, B, "synthetic-host-volume")


def update_event(case: tuple[SnapshotEvent, ...], index: int, **changes
                 ) -> tuple[SnapshotEvent, ...]:
    selected = list(case)
    selected[index] = replace(selected[index], **changes)
    return tuple(selected)


class SnapshotTests(unittest.TestCase):
    def assert_blocked(self, case, reason):
        verdict = assess_synthetic_snapshot(case)
        self.assertEqual(verdict.verdict, Verdict.BLOCKED)
        self.assertEqual(verdict.reason, reason)
        self.assertFalse(verdict.release_authorized)
        self.assertEqual(verdict.native_qualification, "NOT_RUN")

    def test_ideal_trace_never_authorizes_native_release(self):
        result = assess_synthetic_snapshot(events())
        self.assertEqual(result.verdict, Verdict.SOURCE_TEST_ONLY)
        self.assertEqual(result.native_qualification, "NOT_RUN")
        self.assertFalse(result.release_authorized)

    def test_missing_or_wrong_order_cannot_pass(self):
        self.assert_blocked(events()[:-1], "STEP_SEQUENCE_UNPROVEN")
        self.assert_blocked(events()[::-1], "STEP_SEQUENCE_UNPROVEN")

    def test_untrusted_wrong_type_event_blocks_without_exception(self):
        wrong = list(events())
        wrong[3] = "forged-lock"
        self.assert_blocked(tuple(wrong), "STEP_SEQUENCE_UNPROVEN")

    def test_target_drift_at_post_lock_stage(self):
        bad = replace(target(), cdc_instance_id="different-cdc")
        self.assert_blocked(update_event(events(), 5, target=bad),
                            "TARGET_IDENTITY_DRIFT")

    def test_volume_swap_or_disk_swap_fail(self):
        for field, value in (
            ("volume_guid", "different-volume"),
            ("disk_number", 8),
            ("usb_physical_instance_id", "other-usb"),
            ("disk_instance_id", "other-disk"),
        ):
            with self.subTest(field=field):
                self.assert_blocked(
                    update_event(events(), 4, target=replace(target(), **{field: value})),
                    "TARGET_IDENTITY_DRIFT",
                )

    def test_missing_target_qualification(self):
        self.assert_blocked(update_event(events(), 0,
                            target=replace(target(), volume_guid="")),
                            "TARGET_UNQUALIFIED")
        self.assert_blocked(update_event(events(), 0,
                            target=replace(target(), disk_number=True)),
                            "TARGET_UNQUALIFIED")

    def test_prelock_and_late_media_modification(self):
        for index in (1, 2, 3, 4, 5):
            with self.subTest(index=index):
                self.assert_blocked(
                    update_event(events(), index, media_digest=B),
                    "MEDIA_SNAPSHOT_DRIFT",
                )

    def test_reparse_or_namespace_swap(self):
        for index in range(6):
            with self.subTest(index=index):
                self.assert_blocked(
                    update_event(events(), index, namespace_changed=True),
                    "NAMESPACE_OR_MEDIA_DRIFT",
                )

    def test_untyped_lock_and_namespace_flags_block(self):
        self.assert_blocked(update_event(events(), 3, lock_identity=37),
                            "RETAINED_LOCK_UNPROVEN")
        self.assert_blocked(update_event(events(), 4, lock_identity=37),
                            "RETAINED_LOCK_LOST")
        self.assert_blocked(update_event(events(), 5, namespace_changed=None),
                            "NAMESPACE_OR_MEDIA_DRIFT")

    def test_lock_id_must_remain_bound_through_witness(self):
        self.assert_blocked(update_event(events(), 3, lock_identity=""),
                            "RETAINED_LOCK_UNPROVEN")
        self.assert_blocked(update_event(events(), 4, lock_identity="other-lock"),
                            "RETAINED_LOCK_LOST")

    def test_open_leases_and_pending_writes_after_seal(self):
        for index in (2, 3, 4, 5):
            with self.subTest(index=index):
                self.assert_blocked(
                    update_event(events(), index, open_ordinary_leases=1),
                    "ORDINARY_LEASE_REMAINING",
                )
                self.assert_blocked(
                    update_event(events(), index, pending_host_writes=1),
                    "HOST_WRITE_REMAINING",
                )

    def test_invalid_evidence_values_fail_closed(self):
        self.assert_blocked(update_event(events(), 0, media_digest="unverified"),
                            "INVENTORY_DIGEST_UNQUALIFIED")
        self.assert_blocked(update_event(events(), 3, open_ordinary_leases=-1),
                            "LEASE_STATE_UNKNOWN")


class GuardTests(unittest.TestCase):
    def test_simulated_valid_guard_never_authorizes_real_release(self):
        guard = SyntheticGuard()
        result = guard.consume(
            attempt(), create_exclusive=True, file_flushed=True,
            directory_durable=True,
        )
        self.assertEqual(result.verdict, Verdict.SOURCE_TEST_ONLY)
        self.assertEqual(result.native_qualification, "NOT_RUN")
        self.assertFalse(result.release_authorized)
        self.assertEqual(guard.transmissions, 0)

    def test_unqualified_attempt_cannot_be_consumed(self):
        guard = SyntheticGuard()
        invalid = replace(attempt(), original_clear_sha256="changed")
        result = guard.consume(
            invalid, create_exclusive=True, file_flushed=True,
            directory_durable=True,
        )
        self.assertEqual(result.reason, "ATTEMPT_UNQUALIFIED")
        self.assertFalse(guard.consumed)
        self.assertEqual(guard.transmissions, 0)

    def test_every_guard_write_failure_consumes_and_prevents_send(self):
        for which in ("create_exclusive", "file_flushed", "directory_durable"):
            with self.subTest(which=which):
                guard = SyntheticGuard()
                fields = dict(create_exclusive=True, file_flushed=True,
                              directory_durable=True)
                fields[which] = False
                result = guard.consume(attempt(), **fields)
                self.assertEqual(result.verdict, Verdict.BLOCKED)
                self.assertTrue(guard.consumed)
                self.assertEqual(guard.transmissions, 0)
                self.assertEqual(guard.simulated_send(attempt()).verdict,
                                 Verdict.BLOCKED)
                restarted = guard.simulate_restart()
                self.assertEqual(restarted.consume(attempt(), **fields).reason,
                                 "ATTEMPT_ALREADY_CONSUMED")
                self.assertEqual(restarted.transmissions, 0)

    def test_guard_flags_require_real_boolean_values(self):
        for key, expected in (
            ("create_exclusive", "EXCLUSIVE_CREATE_UNPROVEN"),
            ("file_flushed", "FILE_DURABILITY_UNPROVEN"),
            ("directory_durable", "DIRECTORY_DURABILITY_UNPROVEN"),
        ):
            with self.subTest(key=key):
                guard = SyntheticGuard()
                fields = dict(create_exclusive=True, file_flushed=True,
                              directory_durable=True)
                fields[key] = "trusted"
                result = guard.consume(attempt(), **fields)
                self.assertEqual(result.reason, expected)
                self.assertEqual(guard.transmissions, 0)
                self.assertEqual(guard.simulated_send(attempt()).verdict,
                                 Verdict.BLOCKED)

    def test_replay_with_different_attempt_path_identity(self):
        guard = SyntheticGuard()
        fields = dict(create_exclusive=True, file_flushed=True,
                      directory_durable=True)
        guard.consume(attempt(), **fields)
        alternate = replace(attempt(), host_volume_identity="different-host-volume")
        self.assertEqual(guard.consume(alternate, **fields).reason,
                         "ATTEMPT_ALREADY_CONSUMED")
        self.assertEqual(guard.simulated_send(alternate).reason,
                         "UNAUTHORIZED_ATTEMPT")

    def test_simulated_send_at_most_once_even_after_restart(self):
        guard = SyntheticGuard()
        self.assertEqual(guard.simulated_send(attempt()).verdict, Verdict.BLOCKED)
        guard.consume(attempt(), create_exclusive=True, file_flushed=True,
                      directory_durable=True)
        self.assertEqual(guard.simulated_send(attempt()).verdict,
                         Verdict.SOURCE_TEST_ONLY)
        self.assertEqual(guard.simulated_send(attempt()).reason,
                         "ATTEMPT_ALREADY_SENT")
        restarted = guard.simulate_restart()
        self.assertEqual(restarted.simulated_send(attempt()).reason,
                         "ATTEMPT_ALREADY_SENT")
        self.assertEqual(restarted.transmissions, 1)


if __name__ == "__main__":
    unittest.main()
