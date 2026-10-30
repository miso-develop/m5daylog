"""#137: advisory SD free-space observation for an already-authorized USB session.

This module neither discovers a Device nor performs sync/release. The #135
coordinator must call it serially after #51 durable sync and before #89 seal.
No value from this module can authorize a CDC release or Device deletion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Protocol

from .release import EjectTarget, GateState, UsbSessionIoGate

BYTES_PER_SECOND = 16_000 * 2
DAY_SECONDS = 16 * 60 * 60
DAY_BYTES = BYTES_PER_SECOND * DAY_SECONDS
RESERVE_BYTES = 128 * 1024 * 1024
WARNING_THRESHOLD_BYTES = DAY_BYTES + RESERVE_BYTES
UINT64_MAX = (1 << 64) - 1
_VOLUME_ROOT = re.compile(r"^\\\\\?\\Volume\{[0-9a-fA-F-]{36}\}\\$")
_DRIVE_ROOT = re.compile(r"^[A-Za-z]:\\$")


class CapacityStatus(str, Enum):
    READY_FOR_NEXT_16H = "READY_FOR_NEXT_16H"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class VerifiedSession:
    """Injected authorization context, NOT proof from Device metadata alone.

    Caller must have approved this exact #89 MSC/CDC target against the user
    allowlist, validated #51 metadata, and minted a unique session epoch. The
    epoch must change on re-enumeration, reboot, resume or fresh admission.
    """

    target: EjectTarget = field(repr=False)
    cdc_port: str = field(repr=False)
    session_epoch: str = field(repr=False)


@dataclass(frozen=True)
class FreeSpace64:
    """The three unsigned 64-bit GetDiskFreeSpaceExW outputs, in API order."""

    available_to_caller: int
    total_bytes: int
    total_free_bytes: int


@dataclass(frozen=True)
class CapacityResult:
    """Ephemeral, untrusted-as-release-proof notification data.

    Only valid observations contain numbers. No target identifiers are exposed
    in repr or public_notice(). The separate release_blocked flag means the
    upstream coordinator must fail closed; this does NOT execute a release.
    """

    status: CapacityStatus
    reason: str
    release_blocked: bool
    usable_free_bytes: int | None = None
    physical_free_bytes: int | None = None
    estimated_seconds: int | None = None
    observed_at_utc: str | None = None
    _session_epoch: str = field(default="", repr=False, compare=False)

    def valid_for(self, *, session_epoch: str, session_open: bool) -> bool:
        """Only a live matching session may treat the reading as current."""
        return (
            self.status is not CapacityStatus.UNKNOWN
            and bool(self._session_epoch)
            and bool(session_epoch)
            and self._session_epoch == session_epoch
            and session_open
        )

    def public_notice(self, *, last_verified_session: bool = False) -> dict[str, object]:
        """Data for #135's fake/live consumer; no identifiers or sync claims."""
        return {
            "source": "Device SD (not PC disk)",
            "status": self.status.value,
            "action_required": self.status is not CapacityStatus.READY_FOR_NEXT_16H,
            "release_blocked_by_capacity_or_identity": self.release_blocked,
            "usable_free_bytes": self.usable_free_bytes,
            "estimated_minutes": (
                None if self.estimated_seconds is None else self.estimated_seconds // 60
            ),
            "observed_at_utc": self.observed_at_utc,
            "observation_kind": (
                "last-verified-session" if last_verified_session else "current-session"
            ),
            "guaranteed_16h": False,
            "unacked_raw_auto_reclaim": False,
            "reason": self.reason,
        }


class CapacityReader(Protocol):
    def query(self, exact_volume_guid_root: str) -> FreeSpace64: ...


class SessionValidator(Protocol):
    """Existing #89 validation, not a new identity/discovery implementation."""

    def revalidate_target(self, target: EjectTarget, cdc_port: str) -> None: ...
    def volume_available(self, target: EjectTarget) -> bool: ...


def _unknown(reason: str, epoch: str, *, block: bool) -> CapacityResult:
    return CapacityResult(
        CapacityStatus.UNKNOWN, reason, block, _session_epoch=epoch,
    )


def _binding_shape_valid(binding: VerifiedSession) -> bool:
    target = binding.target
    return (
        isinstance(target, EjectTarget)
        and bool(_DRIVE_ROOT.fullmatch(target.mount_root))
        and bool(_VOLUME_ROOT.fullmatch(target.volume_guid))
        and isinstance(target.disk_number, int)
        and not isinstance(target.disk_number, bool)
        and target.disk_number >= 0
        and all(isinstance(x, str) and bool(x) for x in (
            target.expected_device_id, target.disk_instance_id,
            target.eject_instance_id, target.usb_physical_instance_id,
            binding.cdc_port, binding.session_epoch,
        ))
    )


def _usable_free(value: FreeSpace64) -> int:
    if not isinstance(value, FreeSpace64):
        raise ValueError("invalid free-space response")
    a, total, free = value.available_to_caller, value.total_bytes, value.total_free_bytes
    if any(not isinstance(n, int) or isinstance(n, bool) or n < 0 or n > UINT64_MAX
           for n in (a, total, free)):
        raise ValueError("invalid 64-bit counters")
    # TotalBytes may represent an enforced per-user quota, while total_free is
    # volume-wide. Do NOT require total_free <= total in quota environments.
    if total == 0 or a > total or a > free:
        raise ValueError("impossible free-space counters")
    return min(a, free)


def classify_free_space(value: FreeSpace64, *, epoch: str,
                        observed_at_utc: str) -> CapacityResult:
    """Pure byte arithmetic. Caller must authenticate session and I/O phase."""
    free = _usable_free(value)
    seconds = max(0, free - RESERVE_BYTES) // BYTES_PER_SECOND
    status = (CapacityStatus.READY_FOR_NEXT_16H if free >= WARNING_THRESHOLD_BYTES
              else CapacityStatus.ACTION_REQUIRED)
    return CapacityResult(
        status=status,
        reason="CAPACITY_ESTIMATE_ONLY" if status is CapacityStatus.READY_FOR_NEXT_16H
               else "INSUFFICIENT_ESTIMATED_NEXT_16H",
        release_blocked=False,
        usable_free_bytes=free,
        physical_free_bytes=value.total_free_bytes,
        estimated_seconds=seconds,
        observed_at_utc=observed_at_utc,
        _session_epoch=epoch,
    )


def probe_capacity(
    binding: VerifiedSession, *, gate: UsbSessionIoGate,
    validator: SessionValidator, reader: CapacityReader,
    durable_sync_complete: Callable[[], bool],
    current_session_epoch: Callable[[], str],
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> CapacityResult:
    """Read once using #89's ordinary MSC admission, before its I/O seal.

    durable_sync_complete must represent #51's verified final WAV + SQLite
    barrier, not an in-memory copy finished indicator. Identity checks run
    before and after the native call. Identity uncertainty blocks release;
    a capacity-only Win32 error is advisory UNKNOWN. No CDC call is made.

    Concurrent release orchestration is the #135 owner's responsibility: it
    must serialize this call against gate.seal at the coordinator level. The
    process-local gate prevents new post-seal admission and drains leases.
    """
    if not isinstance(binding, VerifiedSession) or not _binding_shape_valid(binding):
        return _unknown("INVALID_AUTHORIZED_TARGET", "", block=True)
    epoch = binding.session_epoch

    try:
        with gate.ordinary("msc"):
            if gate.state is not GateState.OPEN or current_session_epoch() != epoch:
                return _unknown("STALE_OR_SEALED_SESSION", epoch, block=True)
            if not durable_sync_complete():
                return _unknown("SYNC_NOT_VERIFIED", epoch, block=True)
            try:
                validator.revalidate_target(binding.target, binding.cdc_port)
                if not validator.volume_available(binding.target):
                    raise ValueError("target unavailable")
            except Exception:
                return _unknown("IDENTITY_UNVERIFIED", epoch, block=True)
            if gate.state is not GateState.OPEN or current_session_epoch() != epoch:
                return _unknown("STALE_OR_SEALED_SESSION", epoch, block=True)

            # Query the frozen Volume GUID root, not a mutable drive-letter alias.
            try:
                raw = reader.query(binding.target.volume_guid)
                usable = _usable_free(raw)
            except Exception:
                raw, usable = None, None

            # A post-call identity failure is release-blocking even if
            # the free-space API itself failed.
            try:
                validator.revalidate_target(binding.target, binding.cdc_port)
                if not validator.volume_available(binding.target):
                    raise ValueError("target unavailable")
            except Exception:
                return _unknown("IDENTITY_CHANGED", epoch, block=True)
            if gate.state is not GateState.OPEN or current_session_epoch() != epoch:
                return _unknown("STALE_OR_SEALED_SESSION", epoch, block=True)
            if raw is None or usable is None:
                return _unknown("CAPACITY_API_UNAVAILABLE", epoch, block=False)
            observed = clock()
            if not isinstance(observed, datetime) or observed.tzinfo is None:
                return _unknown("CAPACITY_TIMESTAMP_UNAVAILABLE", epoch, block=False)
            return classify_free_space(
                raw, epoch=epoch,
                observed_at_utc=observed.astimezone(timezone.utc).isoformat(),
            )
    except Exception:
        # Gate admission and session failures are not harmless API failures.
        return _unknown("CAPACITY_ADMISSION_BLOCKED", epoch, block=True)
