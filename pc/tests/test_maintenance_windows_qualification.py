"""#126: production pure-code, synthetic-only Windows qualification checks."""
from __future__ import annotations

from dataclasses import replace
import sys
import threading
import unittest
from unittest.mock import patch

from m5daylog import maintenance_windows_qualification as qualification

from m5daylog.maintenance_windows_qualification import (
    Assessment, AttemptIdentity, SnapshotEvent, SyntheticGuard, TargetIdentity,
    Verdict, assess_synthetic_snapshot,
)


A = "a" * 64
B = "b" * 64
ATTEMPT = "9b53a6e0-6ac5-4a29-a743-6f6875e8790c"


class ForgedDigest:
    """Malicious event datum: not a digest, but claims equality to every value."""

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False


class AlwaysEqualStr(str):
    """Malicious string subtype overriding the identity comparison."""

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False


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

    def test_trace_tuple_subclasses_are_untrusted(self):
        class MaliciousTuple(tuple):
            def __iter__(self):
                return iter(events())

        self.assert_blocked(MaliciousTuple(events()), "STEP_SEQUENCE_UNPROVEN")

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

    def test_late_disk_number_numeric_equivalence_cannot_bypass_type_check(self):
        for index in range(1, 6):
            with self.subTest(index=index, value=3.0):
                self.assert_blocked(
                    update_event(events(), index,
                                 target=replace(target(), disk_number=3.0)),
                    "TARGET_UNQUALIFIED",
                )
        # Python considers True == 1; use a valid disk-1 trace as the baseline.
        disk_one = replace(target(), disk_number=1)
        baseline = tuple(replace(e, target=disk_one) for e in events())
        self.assertEqual(assess_synthetic_snapshot(baseline).verdict,
                         Verdict.SOURCE_TEST_ONLY)
        for index in range(1, 6):
            with self.subTest(index=index, value=True):
                self.assert_blocked(
                    update_event(baseline, index,
                                 target=replace(disk_one, disk_number=True)),
                    "TARGET_UNQUALIFIED",
                )

    def test_later_stage_digest_cannot_be_counterfeited_by_equality(self):
        for index in range(1, 6):
            for malformed in (ForgedDigest(), AlwaysEqualStr(B), "not-a-digest"):
                with self.subTest(index=index, malformed=type(malformed).__name__):
                    self.assert_blocked(
                        update_event(events(), index, media_digest=malformed),
                        "INVENTORY_DIGEST_UNQUALIFIED",
                    )
        self.assert_blocked(
            update_event(events(), 0, media_digest=AlwaysEqualStr(A)),
            "INVENTORY_DIGEST_UNQUALIFIED",
        )

    def test_custom_string_equality_cannot_mask_target_identity_drift(self):
        identity_fields = (
            "device_id", "volume_guid", "disk_instance_id",
            "removable_instance_id", "usb_physical_instance_id", "cdc_instance_id",
        )
        for index in range(1, 6):
            for field in identity_fields:
                with self.subTest(index=index, field=field):
                    changed = replace(target(), **{field: AlwaysEqualStr("other-target")})
                    self.assert_blocked(
                        update_event(events(), index, target=changed),
                        "TARGET_UNQUALIFIED",
                    )

    def test_custom_string_steps_and_lock_are_unqualified(self):
        self.assert_blocked(
            update_event(events(), 5, step=AlwaysEqualStr("post_lock_witness")),
            "STEP_SEQUENCE_UNPROVEN",
        )
        self.assert_blocked(
            update_event(events(), 5, lock_identity=AlwaysEqualStr("other-lock")),
            "RETAINED_LOCK_LOST",
        )

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

    def test_noninteger_issue_numeric_equivalence_cannot_consume_guard(self):
        for issue in (119.0, 119 + 0j):
            with self.subTest(issue=issue):
                guard = SyntheticGuard()
                invalid = replace(attempt(), issue=issue)
                result = guard.consume(invalid, create_exclusive=True,
                                       file_flushed=True, directory_durable=True)
                self.assertEqual(result.verdict, Verdict.BLOCKED)
                self.assertEqual(result.reason, "ATTEMPT_UNQUALIFIED")
                self.assertFalse(guard.consumed)
                self.assertEqual(guard.transmissions, 0)
                self.assertEqual(guard.simulated_send(invalid).verdict,
                                 Verdict.BLOCKED)

    def test_untyped_issue_alias_cannot_send_after_valid_consumption(self):
        guard = SyntheticGuard()
        valid = attempt()
        guard.consume(valid, create_exclusive=True, file_flushed=True,
                      directory_durable=True)
        for issue in (119.0, 119 + 0j):
            with self.subTest(issue=issue):
                result = guard.simulated_send(replace(valid, issue=issue))
                self.assertEqual(result.verdict, Verdict.BLOCKED)
                self.assertEqual(result.reason, "UNAUTHORIZED_ATTEMPT")
                self.assertEqual(guard.transmissions, 0)
        self.assertEqual(guard.simulated_send(valid).verdict,
                         Verdict.SOURCE_TEST_ONLY)
        self.assertEqual(guard.transmissions, 1)

    def test_custom_string_equality_cannot_consume_or_send_attempt(self):
        for field, value in (
            ("host_volume_identity", AlwaysEqualStr("other-host-volume")),
            ("attempt_id", AlwaysEqualStr(ATTEMPT)),
            ("original_clear_sha256", AlwaysEqualStr(A)),
            ("receipt_sha256", AlwaysEqualStr(B)),
        ):
            with self.subTest(field=field):
                valid = attempt()
                forged = replace(valid, **{field: value})
                invalid_guard = SyntheticGuard()
                verdict = invalid_guard.consume(
                    forged, create_exclusive=True, file_flushed=True,
                    directory_durable=True,
                )
                self.assertEqual(verdict.reason, "ATTEMPT_UNQUALIFIED")
                self.assertFalse(verdict.release_authorized)
                self.assertEqual(verdict.native_qualification, "NOT_RUN")
                self.assertFalse(invalid_guard.consumed)
                self.assertEqual(invalid_guard.transmissions, 0)

                valid_guard = SyntheticGuard()
                valid_guard.consume(valid, create_exclusive=True,
                                    file_flushed=True, directory_durable=True)
                verdict = valid_guard.simulated_send(forged)
                self.assertEqual(verdict.reason, "UNAUTHORIZED_ATTEMPT")
                self.assertFalse(verdict.release_authorized)
                self.assertEqual(verdict.native_qualification, "NOT_RUN")
                self.assertEqual(valid_guard.transmissions, 0)
                self.assertEqual(valid_guard.simulated_send(valid).verdict,
                                 Verdict.SOURCE_TEST_ONLY)
                self.assertEqual(valid_guard.transmissions, 1)

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

    def test_consumed_attempt_uses_detached_canonical_identity_snapshot(self):
        # @dataclass(frozen=True) is not a security boundary: object.__setattr__
        # can rewrite caller-owned evidence even after guard consumption.
        mutations = (
            ("attempt_id", "e5f8d87c-8d36-49ad-9c6a-8ae782b284e9"),
            ("original_clear_sha256", "c" * 64),
            ("receipt_sha256", "d" * 64),
            ("host_volume_identity", "different-host-volume"),
        )
        for field, new_value in mutations:
            with self.subTest(field=field):
                caller_owned = attempt()
                original = replace(caller_owned)
                guard = SyntheticGuard()
                claimed = guard.consume(
                    caller_owned, create_exclusive=True, file_flushed=True,
                    directory_durable=True,
                )
                self.assertEqual(claimed.verdict, Verdict.SOURCE_TEST_ONLY)
                self.assertTrue(guard.consumed)
                self.assertEqual(guard.transmissions, 0)

                object.__setattr__(caller_owned, field, new_value)
                forged_copy = replace(caller_owned)
                self.assertIsNot(forged_copy, caller_owned)
                self.assertTrue(caller_owned.qualified())
                self.assertTrue(forged_copy.qualified())
                for changed_request in (caller_owned, forged_copy):
                    rejected = guard.simulated_send(changed_request)
                    self.assertEqual(rejected.verdict, Verdict.BLOCKED)
                    self.assertEqual(rejected.reason, "UNAUTHORIZED_ATTEMPT")
                    self.assertFalse(rejected.release_authorized)
                    self.assertEqual(rejected.native_qualification, "NOT_RUN")
                    self.assertEqual(guard.transmissions, 0)

                # The unchanged canonical claim is still valid exactly once
                # in the uninterrupted synthetic process, not after restart.
                allowed = guard.simulated_send(original)
                self.assertEqual(allowed.verdict, Verdict.SOURCE_TEST_ONLY)
                self.assertFalse(allowed.release_authorized)
                self.assertEqual(guard.transmissions, 1)
                self.assertEqual(guard.simulated_send(original).reason,
                                 "ATTEMPT_ALREADY_SENT")
                self.assertEqual(
                    guard.simulate_restart().simulated_send(original).verdict,
                    Verdict.BLOCKED,
                )

    def test_guard_write_failure_with_mutated_claim_remains_terminal(self):
        caller_owned = attempt()
        guard = SyntheticGuard()
        self.assertEqual(
            guard.consume(caller_owned, create_exclusive=True,
                          file_flushed=True, directory_durable=False).reason,
            "DIRECTORY_DURABILITY_UNPROVEN",
        )
        object.__setattr__(caller_owned, "host_volume_identity", "other-host")
        object.__setattr__(caller_owned, "original_clear_sha256", "c" * 64)
        self.assertTrue(guard.consumed)
        self.assertEqual(guard.consume(attempt(), create_exclusive=True,
                                       file_flushed=True,
                                       directory_durable=True).reason,
                         "ATTEMPT_ALREADY_CONSUMED")
        self.assertEqual(guard.simulated_send(replace(caller_owned)).verdict,
                         Verdict.BLOCKED)
        self.assertEqual(
            guard.simulate_restart().simulated_send(attempt()).verdict,
            Verdict.BLOCKED,
        )
        self.assertEqual(guard.transmissions, 0)

    def test_restart_after_consumption_before_send_is_terminal(self):
        # A crash at this boundary cannot prove whether a CDC send escaped.
        # The restart model must never recover the in-process send privilege.
        guard = SyntheticGuard()
        fields = dict(create_exclusive=True, file_flushed=True,
                      directory_durable=True)
        result = guard.consume(attempt(), **fields)
        self.assertEqual(result.verdict, Verdict.SOURCE_TEST_ONLY)
        self.assertEqual(guard.transmissions, 0)

        restarted = guard.simulate_restart()
        self.assertTrue(restarted.consumed)
        self.assertEqual(restarted.transmissions, 0)
        blocked = restarted.simulated_send(attempt())
        self.assertEqual(blocked.verdict, Verdict.BLOCKED)
        self.assertEqual(blocked.reason, "UNAUTHORIZED_ATTEMPT")
        self.assertFalse(blocked.release_authorized)
        self.assertEqual(blocked.native_qualification, "NOT_RUN")
        self.assertEqual(restarted.transmissions, 0)
        self.assertEqual(restarted.consume(attempt(), **fields).reason,
                         "ATTEMPT_ALREADY_CONSUMED")
        self.assertEqual(restarted.simulate_restart().simulated_send(
            attempt()).verdict, Verdict.BLOCKED)

        # An uninterrupted in-process attempt may still send once (synthetic).
        self.assertEqual(guard.simulated_send(attempt()).verdict,
                         Verdict.SOURCE_TEST_ONLY)
        self.assertEqual(guard.transmissions, 1)

    def test_recovered_failed_guard_write_never_sends(self):
        fields = dict(create_exclusive=True, file_flushed=True,
                      directory_durable=True)
        for failed in fields:
            with self.subTest(failed=failed):
                guard = SyntheticGuard()
                failure = dict(fields)
                failure[failed] = False
                self.assertEqual(guard.consume(attempt(), **failure).verdict,
                                 Verdict.BLOCKED)
                recovered = guard.simulate_restart()
                self.assertTrue(recovered.consumed)
                self.assertEqual(recovered.simulated_send(attempt()).verdict,
                                 Verdict.BLOCKED)
                self.assertEqual(recovered.transmissions, 0)

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
        # The recovered send capability is absent even for a known sent attempt.
        self.assertEqual(restarted.simulated_send(attempt()).reason,
                         "UNAUTHORIZED_ATTEMPT")
        self.assertEqual(restarted.transmissions, 1)


class GuardConcurrencyTests(unittest.TestCase):
    """Event-gated races, not GIL/scheduler luck."""

    def _contend(self, guard, first_call, second_call, second_method):
        first_at_key = threading.Event()
        second_at_key = threading.Event()
        release_first = threading.Event()
        second_in_method = threading.Event()
        second_finished = threading.Event()
        calls_lock = threading.Lock()
        calls = [0]
        results: dict[str, object] = {}
        errors: list[str] = []
        real_key = qualification._qualified_attempt_key

        def paused_key(candidate):
            with calls_lock:
                calls[0] += 1
                number = calls[0]
            if number == 1:
                first_at_key.set()
                if not release_first.wait(timeout=5):
                    raise TimeoutError("first key remained paused")
            else:
                second_at_key.set()
            return real_key(candidate)

        def on_call(frame, event, arg):
            if (event == "call" and frame.f_code.co_name == second_method
                    and frame.f_code.co_filename == qualification.__file__):
                second_in_method.set()
            return None

        def first():
            try:
                results["first"] = first_call()
            except BaseException as error:
                errors.append("first:" + repr(error))

        def second():
            sys.settrace(on_call)
            try:
                results["second"] = second_call()
            except BaseException as error:
                errors.append("second:" + repr(error))
            finally:
                sys.settrace(None)
                second_finished.set()

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=second)
        with patch.object(qualification, "_qualified_attempt_key", side_effect=paused_key):
            try:
                t1.start()
                self.assertTrue(first_at_key.wait(3), "first call did not enter key capture")
                t2.start()
                self.assertTrue(second_in_method.wait(3), "second call not entered")
                self.assertFalse(second_at_key.wait(0.2),
                                 "second identity check escaped guarded critical section")
                self.assertFalse(second_finished.is_set(),
                                 "second transition completed while first was pending")
            finally:
                release_first.set()
                if t1.ident is not None:
                    t1.join(timeout=4)
                if t2.ident is not None:
                    t2.join(timeout=4)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        self.assertEqual(errors, [])
        return results

    def test_concurrent_claim_only_one_synthetic_success(self):
        guard = SyntheticGuard()
        inputs = dict(create_exclusive=True, file_flushed=True, directory_durable=True)
        results = self._contend(
            guard,
            lambda: guard.consume(attempt(), **inputs),
            lambda: guard.consume(attempt(), **inputs), "consume",
        )
        self.assertEqual(results["first"].verdict, Verdict.SOURCE_TEST_ONLY)
        self.assertEqual(results["second"].reason, "ATTEMPT_ALREADY_CONSUMED")
        self.assertTrue(guard.consumed)
        self.assertEqual(guard.transmissions, 0)
        for r in results.values():
            self.assertIsInstance(r, Assessment)
            self.assertFalse(r.release_authorized)
            self.assertEqual(r.native_qualification, "NOT_RUN")

    def test_concurrent_send_never_increments_twice(self):
        guard = SyntheticGuard()
        guard.consume(attempt(), create_exclusive=True, file_flushed=True,
                      directory_durable=True)
        results = self._contend(
            guard, lambda: guard.simulated_send(attempt()),
            lambda: guard.simulated_send(attempt()), "simulated_send",
        )
        self.assertEqual(results["first"].verdict, Verdict.SOURCE_TEST_ONLY)
        self.assertEqual(results["second"].reason, "ATTEMPT_ALREADY_SENT")
        self.assertEqual(guard.transmissions, 1)
        self.assertEqual(guard.simulated_send(attempt()).verdict, Verdict.BLOCKED)
        self.assertEqual(guard.simulate_restart().simulated_send(attempt()).verdict,
                         Verdict.BLOCKED)

    def test_uncertain_failed_guard_claim_blocks_parallel_send(self):
        guard = SyntheticGuard()
        results = self._contend(
            guard,
            lambda: guard.consume(attempt(), create_exclusive=True,
                                  file_flushed=False, directory_durable=True),
            lambda: guard.simulated_send(attempt()), "simulated_send",
        )
        self.assertEqual(results["first"].reason, "FILE_DURABILITY_UNPROVEN")
        self.assertEqual(results["second"].verdict, Verdict.BLOCKED)
        self.assertEqual(results["second"].reason, "UNAUTHORIZED_ATTEMPT")
        self.assertTrue(guard.consumed)
        self.assertEqual(guard.transmissions, 0)
        self.assertEqual(guard.simulate_restart().simulated_send(attempt()).verdict,
                         Verdict.BLOCKED)

    def test_restart_during_uncertain_guard_write_never_recovers_permission(self):
        guard = SyntheticGuard()
        results = self._contend(
            guard,
            lambda: guard.consume(attempt(), create_exclusive=True,
                                  file_flushed=False, directory_durable=True),
            guard.simulate_restart, "simulate_restart",
        )
        recovered = results["second"]
        self.assertTrue(recovered.consumed)
        self.assertEqual(recovered.transmissions, 0)
        self.assertEqual(recovered.simulated_send(attempt()).verdict, Verdict.BLOCKED)
        self.assertEqual(recovered.consume(attempt(), create_exclusive=True,
                                           file_flushed=True,
                                           directory_durable=True).reason,
                         "ATTEMPT_ALREADY_CONSUMED")


if __name__ == "__main__":
    unittest.main()
