"""#126: non-executing qualification model for Windows maintenance release.

This module deliberately has no Win32, filesystem, serial or release adapter.
An event trace supplied by an untrusted caller can establish *only* synthetic
ordering invariants; it can never attest a real locked volume or durable guard.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import uuid


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class Verdict(str, Enum):
    BLOCKED = "BLOCKED"
    SOURCE_TEST_ONLY = "SOURCE_TEST_ONLY"


@dataclass(frozen=True)
class Assessment:
    verdict: Verdict
    reason: str
    native_qualification: str = "NOT_RUN"
    release_authorized: bool = False


@dataclass(frozen=True)
class TargetIdentity:
    device_id: str
    volume_guid: str
    disk_number: int
    disk_instance_id: str
    removable_instance_id: str
    usb_physical_instance_id: str
    cdc_instance_id: str

    def qualified(self) -> bool:
        # Presence is necessary but never sufficient for real-world identity.
        return (
            all(isinstance(v, str) and bool(v.strip()) for v in (
                self.device_id, self.volume_guid, self.disk_instance_id,
                self.removable_instance_id, self.usb_physical_instance_id,
                self.cdc_instance_id,
            ))
            and type(self.disk_number) is int and self.disk_number >= 0
        )


@dataclass(frozen=True)
class SnapshotEvent:
    step: str
    target: TargetIdentity
    media_digest: str
    lock_identity: str = ""
    open_ordinary_leases: int = 0
    pending_host_writes: int = 0
    namespace_changed: bool = False


_STEPS = ("preflight", "target_freeze", "io_seal", "volume_lock",
          "volume_dismount", "post_lock_witness")


def assess_synthetic_snapshot(events: tuple[SnapshotEvent, ...]) -> Assessment:
    """Test a *fake event sequence*, never produce a release permit.

    Win32 volume locking is not a proof of a post-dismount filesystem
    inventory. The future trusted Windows witness must be separately defined
    and independently qualified; a matching digest in this test is not one.
    """
    if not isinstance(events, tuple) or len(events) != len(_STEPS):
        return Assessment(Verdict.BLOCKED, "STEP_SEQUENCE_UNPROVEN")
    if not all(isinstance(e, SnapshotEvent) for e in events):
        return Assessment(Verdict.BLOCKED, "STEP_SEQUENCE_UNPROVEN")
    if tuple(e.step for e in events) != _STEPS:
        return Assessment(Verdict.BLOCKED, "STEP_SEQUENCE_UNPROVEN")
    first = events[0]
    if not isinstance(first, SnapshotEvent) or not isinstance(first.target, TargetIdentity) or not first.target.qualified():
        return Assessment(Verdict.BLOCKED, "TARGET_UNQUALIFIED")
    if not isinstance(first.media_digest, str) or not _SHA256.fullmatch(first.media_digest):
        return Assessment(Verdict.BLOCKED, "INVENTORY_DIGEST_UNQUALIFIED")
    lock_identity = events[3].lock_identity
    if not isinstance(lock_identity, str) or not lock_identity.strip():
        return Assessment(Verdict.BLOCKED, "RETAINED_LOCK_UNPROVEN")
    for idx, event in enumerate(events):
        # Dataclass equality alone accepts numerically equal but mistyped IDs
        # (e.g. 3 == 3.0 or 1 == True); qualify every trace stage first.
        if not isinstance(event.target, TargetIdentity) or not event.target.qualified():
            return Assessment(Verdict.BLOCKED, "TARGET_UNQUALIFIED")
        if event.target != first.target:
            return Assessment(Verdict.BLOCKED, "TARGET_IDENTITY_DRIFT")
        if type(event.namespace_changed) is not bool or event.namespace_changed:
            return Assessment(Verdict.BLOCKED, "NAMESPACE_OR_MEDIA_DRIFT")
        if event.media_digest != first.media_digest:
            return Assessment(Verdict.BLOCKED, "MEDIA_SNAPSHOT_DRIFT")
        if type(event.open_ordinary_leases) is not int or event.open_ordinary_leases < 0:
            return Assessment(Verdict.BLOCKED, "LEASE_STATE_UNKNOWN")
        if type(event.pending_host_writes) is not int or event.pending_host_writes < 0:
            return Assessment(Verdict.BLOCKED, "WRITE_STATE_UNKNOWN")
        if idx >= 2 and event.open_ordinary_leases:
            return Assessment(Verdict.BLOCKED, "ORDINARY_LEASE_REMAINING")
        if idx >= 2 and event.pending_host_writes:
            return Assessment(Verdict.BLOCKED, "HOST_WRITE_REMAINING")
        if idx >= 3 and (not isinstance(event.lock_identity, str)
                         or event.lock_identity != lock_identity):
            return Assessment(Verdict.BLOCKED, "RETAINED_LOCK_LOST")
    return Assessment(Verdict.SOURCE_TEST_ONLY, "SYNTHETIC_ORDERING_ONLY")


@dataclass(frozen=True)
class AttemptIdentity:
    issue: int
    attempt_id: str
    original_clear_sha256: str
    receipt_sha256: str
    host_volume_identity: str

    def qualified(self) -> bool:
        if (type(self.issue) is not int or self.issue != 119
                or not isinstance(self.host_volume_identity, str)
                or not self.host_volume_identity.strip()):
            return False
        try:
            parsed = uuid.UUID(self.attempt_id)
        except (ValueError, AttributeError, TypeError):
            return False
        return (str(parsed) == self.attempt_id
                and isinstance(self.original_clear_sha256, str)
                and bool(_SHA256.fullmatch(self.original_clear_sha256))
                and isinstance(self.receipt_sha256, str)
                and bool(_SHA256.fullmatch(self.receipt_sha256)))


class SyntheticGuard:
    """In-memory fault model only: no host journal, I/O or CDC transport."""

    def __init__(self) -> None:
        self._claimed: AttemptIdentity | None = None
        self._consumed = False
        self._transmissions = 0
        self._ready = False

    @property
    def transmissions(self) -> int:
        return self._transmissions

    @property
    def consumed(self) -> bool:
        return self._consumed

    def consume(self, attempt: AttemptIdentity, *, create_exclusive: bool,
                file_flushed: bool, directory_durable: bool) -> Assessment:
        if self._consumed:
            return Assessment(Verdict.BLOCKED, "ATTEMPT_ALREADY_CONSUMED")
        if not isinstance(attempt, AttemptIdentity) or not attempt.qualified():
            return Assessment(Verdict.BLOCKED, "ATTEMPT_UNQUALIFIED")
        # Reserve before any potentially ambiguous create/write operation.
        self._claimed = attempt
        self._consumed = True
        if type(create_exclusive) is not bool or not create_exclusive:
            return Assessment(Verdict.BLOCKED, "EXCLUSIVE_CREATE_UNPROVEN")
        if type(file_flushed) is not bool or not file_flushed:
            return Assessment(Verdict.BLOCKED, "FILE_DURABILITY_UNPROVEN")
        if type(directory_durable) is not bool or not directory_durable:
            return Assessment(Verdict.BLOCKED, "DIRECTORY_DURABILITY_UNPROVEN")
        # This is a simulated claim, not Windows power-loss assurance.
        self._ready = True
        return Assessment(Verdict.SOURCE_TEST_ONLY, "SYNTHETIC_GUARD_ONLY")

    def simulated_send(self, attempt: AttemptIdentity) -> Assessment:
        if not self._ready or not self._consumed or attempt != self._claimed:
            return Assessment(Verdict.BLOCKED, "UNAUTHORIZED_ATTEMPT")
        if self._transmissions:
            return Assessment(Verdict.BLOCKED, "ATTEMPT_ALREADY_SENT")
        # A source test counter is not a CDC transmission.
        self._transmissions += 1
        return Assessment(Verdict.SOURCE_TEST_ONLY, "SYNTHETIC_SEND_ONLY")

    def simulate_restart(self) -> "SyntheticGuard":
        """Conservatively preserve consumed state, including uncertain writes.

        This does *not* prove an actual Windows process/power restart persists
        the marker; native host journal recovery remains NOT_RUN.
        """
        copy = SyntheticGuard()
        copy._claimed = self._claimed
        copy._consumed = self._consumed
        copy._transmissions = self._transmissions
        copy._ready = self._ready
        return copy
