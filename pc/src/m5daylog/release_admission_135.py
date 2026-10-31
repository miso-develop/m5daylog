"""#135/#147 source-only composition of the canonical #89 release coordinator.

No CLI/watcher/Device caller imports this adapter. It is NOT a release permit,
external proof issuer, or Human Gate result. Injected callbacks are NOT
authentication; a future authorized integration must independently qualify
their backing evidence before enabling any real Windows/USB sender.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from .capacity import VerifiedSession
from .dev_install_release_fence import DevInstallReceipt, DevInstallReleaseFence
from .first_use_admission import CycleOrigin
from .release import (
    EjectTarget, ObservationFn, PreconditionBlocked, ReleaseOutcome,
    SyncFn, SyncReleaseCoordinator, UsbSessionIoGate,
)
from .release_cycle_candidate import QualifiedCycleCandidate

ExternalDevProof = Callable[[DevInstallReceipt, VerifiedSession, EjectTarget], bool]
ExternalWakeProof = Callable[
    [DevInstallReceipt, Mapping[str, object], VerifiedSession, EjectTarget], bool
]
ExternalNormalProof = Callable[[QualifiedCycleCandidate, EjectTarget], bool]


@dataclass
class SourceOnlyReleaseSequence:
    """Two distinct first-use consumptions plus later R3 cycles, one #89 sender.

    The same R3 cooperating OS lock spans the entire canonical coordinator
    call, including RTC, volume lock/dismount, CDC and disappearance. All
    invocations in v0.1 are TEST-ONLY fake ports. No public entrypoint uses it.
    """

    fence: DevInstallReleaseFence
    coordinator: SyncReleaseCoordinator
    session: VerifiedSession
    sync: SyncFn
    current_epoch: Callable[[], str]
    cdc_mutations: Callable[[], None]
    before_seal_observation: ObservationFn

    def _target(self, target: EjectTarget) -> None:
        if (type(self.session) is not VerifiedSession
                or type(target) is not EjectTarget
                or target != self.session.target
                or not self.session.cdc_port
                or not self.session.session_epoch
                or self.current_epoch() != self.session.session_epoch):
            raise PreconditionBlocked("release target or session changed")

    def _check_inputs(self) -> None:
        if (not isinstance(self.fence, DevInstallReleaseFence)
                or not isinstance(self.coordinator, SyncReleaseCoordinator)
                or not callable(self.sync)
                or not callable(self.current_epoch)
                or not callable(self.cdc_mutations)
                or not callable(self.before_seal_observation)):
            raise PreconditionBlocked("release admission inputs unavailable")
        self._target(self.session.target)

    def _run(
        self, *,
        reserve: Callable[[str, EjectTarget], None],
        verify: Callable[[str, EjectTarget], None],
        fixed_attempt: str | None = None,
    ) -> ReleaseOutcome:
        self._check_inputs()

        def before_seal(target: EjectTarget, gate: UsbSessionIoGate) -> bool:
            self._target(target)
            if self.before_seal_observation(target, gate) is not True:
                return False
            self._target(target)
            return True

        with self.fence.cycles.serialized():
            # Never create a second sender or a lock scoped only to CDC.
            return self.coordinator.run(
                mount_root=self.session.target.mount_root,
                cdc_port=self.session.cdc_port,
                device_id=lambda: self.session.target.expected_device_id,
                sync=self.sync, gate=UsbSessionIoGate(),
                cdc_mutations=self.cdc_mutations,
                before_cdc_mutations=reserve,
                before_seal_observation=before_seal,
                before_release_send=verify,
                release_attempt_id=fixed_attempt,
            )

    def dev_first_release(
        self, *, receipt: DevInstallReceipt,
        verify_external: ExternalDevProof,
    ) -> ReleaseOutcome:
        """FI2-I2: consume the generation-bound FIRST permit before RTC/CDC."""
        self._check_inputs()
        if (type(receipt) is not DevInstallReceipt
                or receipt.device_id != self.session.target.expected_device_id
                or not callable(verify_external)):
            raise PreconditionBlocked("DEV initial-release evidence missing")

        def reserve(attempt: str, target: EjectTarget) -> None:
            self._target(target)
            if attempt != receipt.first_release_attempt_id:
                raise PreconditionBlocked("DEV first-release attempt mismatch")
            self.fence.consume_locked(
                receipt, verify_external=lambda r:
                verify_external(r, self.session, target) is True,
            )

        def verify(attempt: str, target: EjectTarget) -> None:
            self._target(target)
            if attempt != receipt.first_release_attempt_id:
                raise PreconditionBlocked("DEV first-release attempt mismatch")
            self.fence.verify_consumed_locked(receipt)

        return self._run(
            reserve=reserve, verify=verify,
            fixed_attempt=receipt.first_release_attempt_id,
        )

    def first_supervised_cycle(
        self, *, receipt: DevInstallReceipt,
        candidate: QualifiedCycleCandidate,
        status: Mapping[str, object],
        verify_first_wake: ExternalWakeProof,
    ) -> ReleaseOutcome:
        """FI2-I3: independently verified first WAKE, then distinct R3 tuple."""
        self._check_inputs()
        if (type(receipt) is not DevInstallReceipt
                or type(candidate) is not QualifiedCycleCandidate
                or candidate.release_cycle_origin is not CycleOrigin.SUPERVISED_FIRST_USE
                or candidate.binding != self.session
                or candidate.device_id != receipt.device_id
                or candidate.device_id != self.session.target.expected_device_id
                or not isinstance(status, Mapping)
                or status.get("releaseCycleId") != candidate.release_cycle_id
                or status.get("releaseCycleOrigin") != "SUPERVISED_FIRST_USE"
                or not callable(verify_first_wake)):
            raise PreconditionBlocked("first supervised cycle unqualified")

        def reserve(attempt: str, target: EjectTarget) -> None:
            self._target(target)
            self.fence.initialize_first_dev_cycle_locked(
                receipt, status=status, cycle_attempt_id=attempt,
                verify_first_wake=lambda r, s:
                verify_first_wake(r, s, self.session, target) is True,
            )

        def verify(attempt: str, target: EjectTarget) -> None:
            self._target(target)
            self.fence.verify_consumed_locked(receipt)
            self.fence.cycles.verify_reserved_locked(
                candidate.device_id, candidate.release_cycle_id, attempt,
            )

        return self._run(reserve=reserve, verify=verify)

    def normal_cycle(
        self, *, candidate: QualifiedCycleCandidate,
        verify_current_cycle: ExternalNormalProof,
    ) -> ReleaseOutcome:
        """R3 enrolled NORMAL_WAKE. DEV first-use evidence is not reusable."""
        self._check_inputs()
        if (type(candidate) is not QualifiedCycleCandidate
                or candidate.release_cycle_origin is not CycleOrigin.NORMAL_WAKE
                or candidate.binding != self.session
                or candidate.device_id != self.session.target.expected_device_id
                or not callable(verify_current_cycle)):
            raise PreconditionBlocked("normal release cycle unqualified")

        def reserve(attempt: str, target: EjectTarget) -> None:
            self._target(target)
            if verify_current_cycle(candidate, target) is not True:
                raise PreconditionBlocked("current cycle external proof missing")
            self.fence.cycles.reserve_locked(
                candidate.device_id, candidate.release_cycle_id, attempt,
            )

        def verify(attempt: str, target: EjectTarget) -> None:
            self._target(target)
            self.fence.cycles.verify_reserved_locked(
                candidate.device_id, candidate.release_cycle_id, attempt,
            )

        return self._run(reserve=reserve, verify=verify)
