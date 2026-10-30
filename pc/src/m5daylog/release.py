"""Fail-closed PC release orchestration (Decision D-031, Task #89).

A successful raw sync is never rolled back if release fails. Ordinary MSC/CDC
leases are permanently sealed for this USB session before host quiescence.
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterator, Protocol

from .sync import SyncSummary


class ReleaseError(Exception):
    """A known release failure; no protected metadata is included."""


class WrongTarget(ReleaseError):
    pass


class UnsupportedTarget(ReleaseError):
    pass


class AlreadyRemoved(ReleaseError):
    pass


class HostQuiesceError(ReleaseError):
    pass


class ReleaseTimeout(HostQuiesceError):
    """A bounded release barrier or disappearance deadline expired."""


class VolumeBusy(HostQuiesceError):
    pass


class VolumeLockError(HostQuiesceError):
    pass


class VolumeDismountError(HostQuiesceError):
    pass


class ReleaseRejected(ReleaseError):
    pass


class ReleaseIndeterminate(ReleaseError):
    """A release request might have been accepted; never auto-resend."""


class GateClosed(ReleaseError):
    pass


class PreconditionBlocked(ReleaseError):
    """#135 required pre-release mutation, observation or fence denied."""




class GateState(str, Enum):
    OPEN = "OPEN"
    SEALED_FOR_RELEASE = "SEALED_FOR_RELEASE"
    HOST_QUIESCED = "HOST_QUIESCED"
    RELEASE_ACCEPTED = "RELEASE_ACCEPTED"
    RELEASED = "RELEASED"


class UsbSessionIoGate:
    """Process-session admission gate; all Device I/O callers must take a lease.

    The gate is not an OS-wide lock. Callers outside this process are excluded
    only by the subsequent exact-target FSCTL volume lock.
    """

    def __init__(self) -> None:
        self._cv = threading.Condition(threading.RLock())
        self._ordinary = 0
        self._control = False
        self._drained = False
        self._state = GateState.OPEN

    @property
    def state(self) -> GateState:
        with self._cv:
            return self._state

    @property
    def outstanding_ordinary(self) -> int:
        with self._cv:
            return self._ordinary

    @contextmanager
    def ordinary(self, transport: str) -> Iterator[None]:
        if transport not in ("msc", "cdc"):
            raise ValueError("unknown transport")
        with self._cv:
            if self._state is not GateState.OPEN:
                raise GateClosed("ordinary Device I/O admission is sealed")
            self._ordinary += 1
        try:
            yield
        finally:
            with self._cv:
                self._ordinary -= 1
                self._cv.notify_all()

    def seal(self, *, timeout_s: float = 10.0,
             clock: Callable[[], float] = time.monotonic) -> None:
        if timeout_s < 0:
            raise ValueError("timeout must not be negative")
        with self._cv:
            if self._state is not GateState.OPEN:
                raise GateClosed("release already started")
            self._state = GateState.SEALED_FOR_RELEASE
            deadline = clock() + timeout_s
            while self._ordinary:
                remaining = deadline - clock()
                if remaining <= 0:
                    raise ReleaseTimeout("ordinary Device handles did not drain")
                self._cv.wait(remaining)
            self._drained = True

    @contextmanager
    def release_control(self) -> Iterator[None]:
        with self._cv:
            if (self._state is not GateState.SEALED_FOR_RELEASE
                    or not self._drained or self._control):
                raise GateClosed("dedicated release control not available")
            self._control = True
        try:
            yield
        finally:
            with self._cv:
                self._control = False

    def host_quiesced(self) -> None:
        with self._cv:
            if self._state is not GateState.SEALED_FOR_RELEASE or not self._control:
                raise GateClosed("host quiescence without a release lease")
            self._state = GateState.HOST_QUIESCED

    def accepted(self) -> None:
        with self._cv:
            if self._state is not GateState.HOST_QUIESCED or not self._control:
                raise GateClosed("release accepted outside quiesced phase")
            self._state = GateState.RELEASE_ACCEPTED

    def released(self) -> None:
        with self._cv:
            if self._state is not GateState.RELEASE_ACCEPTED:
                raise GateClosed("release not accepted")
            self._state = GateState.RELEASED


@dataclass(frozen=True)
class EjectTarget:
    expected_device_id: str
    mount_root: str
    volume_guid: str
    disk_number: int
    disk_instance_id: str
    eject_instance_id: str
    usb_physical_instance_id: str


@dataclass(frozen=True)
class AckFlushResult:
    """#52 integration seam: a payload error is tolerated only after close."""
    handles_closed: bool


@dataclass(frozen=True)
class ReleaseOutcome:
    status: str
    reason: str
    attempt_id: str | None = None
    target: EjectTarget | None = None

    @property
    def success(self) -> bool:
        return self.status == "SYNC_COMPLETE_RELEASED"


class LockedVolume(Protocol):
    """Context manager's enter/exit keep the exact volume lock alive."""
    def __enter__(self) -> "LockedVolume": ...
    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...


class HostReleasePort(Protocol):
    def resolve_target(self, mount_root: str, expected_device_id: str) -> EjectTarget: ...
    def revalidate_target(self, target: EjectTarget, cdc_port: str) -> None: ...
    def volume_available(self, target: EjectTarget) -> bool: ...
    def quiesce(self, target: EjectTarget) -> LockedVolume: ...
    def release_storage(self, cdc_port: str, attempt_id: str, target: EjectTarget) -> None: ...
    def volume_disappeared(self, target: EjectTarget) -> bool: ...


SyncFn = Callable[[], SyncSummary]
DeviceIdFn = Callable[[], str]
AckFn = Callable[[], AckFlushResult]
MutationFn = Callable[[], None]
ObservationFn = Callable[[EjectTarget, UsbSessionIoGate], bool]
PreSendFn = Callable[[str, EjectTarget], None]


class SyncReleaseCoordinator:
    def __init__(
        self, port: HostReleasePort, *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        timeout_s: float = 20.0,
        poll_interval_s: float = 0.2,
    ) -> None:
        if timeout_s <= 0 or poll_interval_s <= 0:
            raise ValueError("polling timing must be positive")
        self.port = port
        self.clock = clock
        self.sleep = sleep
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s

    def run(
        self, *, mount_root: str, cdc_port: str,
        device_id: DeviceIdFn, sync: SyncFn,
        gate: UsbSessionIoGate,
        ack_flush: AckFn | None = None,
        cdc_mutations: MutationFn | None = None,
        before_seal_observation: ObservationFn | None = None,
        before_release_send: PreSendFn | None = None,
    ) -> ReleaseOutcome:
        # No release-side I/O before #51 returns with verified finals and DB commits.
        try:
            with gate.ordinary("msc"):
                expected_id = device_id()
                summary = sync()
            if summary.conflicts:
                return ReleaseOutcome("SYNC_INTERRUPTED", "SYNC_CONFLICT")
        except Exception:
            return ReleaseOutcome("SYNC_INTERRUPTED", "SYNC_ERROR")

        attempt_id: str | None = None
        target: EjectTarget | None = None
        try:
            # #52: a bounded, already-eligible ACK snapshot; never wait for AI.
            if ack_flush is not None:
                with gate.ordinary("msc"):
                    ack_result = ack_flush()
                    if not isinstance(ack_result, AckFlushResult) or not ack_result.handles_closed:
                        raise HostQuiesceError("ACK handles not confirmed closed")

            # Resolve the exact MSC + CDC physical parent BEFORE the first
            # ordinary mutation. A successful sync does not authorize a
            # mutable COM alias or a newly enumerated volume.
            with gate.ordinary("msc"):
                target = self.port.resolve_target(mount_root, expected_id)
                self.port.revalidate_target(target, cdc_port)
                if not self.port.volume_available(target):
                    raise AlreadyRemoved("exact volume absent before release")

            # Required RTC/other pre-release mutations must complete while
            # admission is open, on the already frozen exact CDC endpoint.
            if cdc_mutations is not None:
                with gate.ordinary("cdc"):
                    cdc_mutations()

            # #137: optional source-only, session-bound capacity observation.
            # Its callback owns ordinary MSC admission and must return False
            # when identity or epoch changes (capacity-only API failure warns).
            if before_seal_observation is not None:
                if before_seal_observation(target, gate) is not True:
                    raise PreconditionBlocked("pre-seal observation unqualified")

            # Recheck after CDC mutation and capacity observation, before
            # the irreversible I/O seal. No ordinary I/O follows this barrier.
            with gate.ordinary("msc"):
                self.port.revalidate_target(target, cdc_port)
                if not self.port.volume_available(target):
                    raise AlreadyRemoved("exact volume absent before seal")

            gate.seal(timeout_s=self.timeout_s, clock=self.clock)
            attempt_id = str(uuid.uuid4())
            with gate.release_control():
                # Lock stays held until response and disappearance observation.
                with self.port.quiesce(target):
                    gate.host_quiesced()
                    # A caller-provided durable attempt fence MUST be persisted
                    # before the first possible CDC release byte. This hook
                    # never authorizes retry; the caller must validate a real
                    # fresh Device session before supplying it.
                    if before_release_send is not None:
                        before_release_send(attempt_id, target)
                    self.port.release_storage(cdc_port, attempt_id, target)
                    gate.accepted()
                    deadline = self.clock() + self.timeout_s
                    while True:
                        if self.port.volume_disappeared(target):
                            gate.released()
                            return ReleaseOutcome(
                                "SYNC_COMPLETE_RELEASED", "RELEASED", attempt_id, target
                            )
                        if self.clock() >= deadline:
                            raise ReleaseTimeout("volume removal deadline exceeded")
                        self.sleep(min(self.poll_interval_s, max(0, deadline - self.clock())))
        except Exception as exc:
            known = {
                WrongTarget: "WRONG_TARGET",
                UnsupportedTarget: "UNSUPPORTED_TARGET",
                AlreadyRemoved: "ALREADY_REMOVED_UNPROVEN",
                ReleaseRejected: "RELEASE_REJECTED",
                ReleaseIndeterminate: "INDETERMINATE",
                VolumeBusy: "BUSY",
                VolumeLockError: "LOCK_FAILED",
                VolumeDismountError: "DISMOUNT_FAILED",
                ReleaseTimeout: "TIMEOUT",
                GateClosed: "GATE_CLOSED",
                PreconditionBlocked: "PRECONDITION_BLOCKED",
            }
            reason = next(
                (code for typ, code in known.items() if isinstance(exc, typ)),
                "RELEASE_ERROR",
            )
            return ReleaseOutcome("SYNC_COMPLETE_RELEASE_FAILED", reason, attempt_id, target)
