"""#135 fail-closed, source-only automatic-intake coordination.

This module intentionally has no CDC release sender. The existing manual
sync-release command has no shared durable release-attempt fence, so enabling
unattended RELEASE_STORAGE here would violate D-031. Arrival providers must
prove the exact USB_SYNC MSC/CDC session before this code copies any WAV.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Protocol, Sequence

from .capacity import CapacityResult, CapacityStatus, VerifiedSession, probe_capacity
from .release import EjectTarget, UsbSessionIoGate
from .sync import (
    SyncSummary, _contracts_dir, _load_manifest, _preflight, inspect_device,
    sync_device,
)


@dataclass(frozen=True)
class Arrival:
    """Provider-issued candidate. It is NOT authorization by itself."""

    binding: VerifiedSession = field(repr=False)
    usb_sync_confirmed: bool = False


@dataclass(frozen=True)
class IntakeResult:
    status: str
    reason: str
    copied: int = 0
    reused: int = 0
    capacity: CapacityResult | None = field(default=None, repr=False)

    def public_notice(self) -> dict[str, object]:
        """Allowlist-only output: never expose Device/host identifiers or audio."""
        capacity = self.capacity.public_notice(last_verified_session=True) if self.capacity else {
            "source": "Device SD (not PC disk)",
            "status": CapacityStatus.UNKNOWN.value,
            "action_required": True,
            "usable_free_bytes": None,
            "estimated_minutes": None,
            "guaranteed_16h": False,
            "unacked_raw_auto_reclaim": False,
            "observation_kind": "unverified",
        }
        return {
            "sync_release": self.status,
            "reason": self.reason,
            "copied": self.copied,
            "reused": self.reused,
            "capacity": capacity,
            "safe_to_leave_usb_connected": False,
            "device_shutdown_armed_proven": False,
        }


class ArrivalProvider(Protocol):
    """Observer is responsible for USB_SYNC proof, epoch, and unique binding."""

    def scan(self) -> Sequence[Arrival]: ...


class AdmissionPort(Protocol):
    def revalidate_target(self, target: EjectTarget, cdc_port: str) -> None: ...
    def volume_available(self, target: EjectTarget) -> bool: ...


class CapacityPort(Protocol):
    def query(self, exact_volume_guid_root: str): ...


@contextmanager
def exclusive_intake(local_lock_file: Path) -> Iterator[bool]:
    """One cooperating local intake process; lock file is NOT on Device media.

    This prevents simultaneous #51 writes, but does not establish a persistent
    CDC RELEASE_STORAGE fence. Release remains completely disabled.
    """
    local_lock_file.parent.mkdir(parents=True, exist_ok=True)
    with local_lock_file.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class IntakeCoordinator:
    """#51 raw intake and #137 capacity; never invokes release/seal/SET_TIME."""

    def __init__(
        self, *, port: AdmissionPort, capacity_reader: CapacityPort,
        trusted_device_ids: frozenset[str], local_lock_file: Path,
        library_root: Path, db_path: Path, contracts_dir: Path | None = None,
        preflight: Callable[[str], str] | None = None,
        sync: Callable[[str], SyncSummary] | None = None,
        port_factory: Callable[[], AdmissionPort] | None = None,
    ) -> None:
        self.port = port
        self._port_factory = port_factory or (lambda: port)
        self.capacity_reader = capacity_reader
        self.trusted_device_ids = trusted_device_ids
        self.local_lock_file = Path(local_lock_file)
        self.library_root = Path(library_root)
        self.db_path = Path(db_path)
        self.contracts_dir = contracts_dir
        self._preflight = preflight or self._canonical_preflight
        self._sync = sync or (
            lambda root: sync_device(root, library_root=self.library_root,
                                     db_path=self.db_path, contracts_dir=self.contracts_dir)
        )

    def _canonical_preflight(self, root: str) -> str:
        """Validate canonical schema, cross-document IDs and source containment."""
        device = inspect_device(root, contracts_dir=self.contracts_dir)
        manifest = _load_manifest(Path(root), _contracts_dir(self.contracts_dir))
        _preflight(Path(root), device, manifest)
        return device["deviceId"]

    def _authorized(self, arrival: Arrival) -> bool:
        if not isinstance(arrival, Arrival) or not arrival.usb_sync_confirmed:
            return False
        binding = arrival.binding
        if not isinstance(binding, VerifiedSession):
            return False
        t = binding.target
        if not isinstance(t, EjectTarget):
            return False
        # Require exact, already-verified Windows volume, and a fresh caller
        # supplied session epoch. Never interpret a drive label as an identity.
        import re
        return (
            t.expected_device_id in self.trusted_device_ids
            and bool(binding.session_epoch)
            and bool(binding.cdc_port)
            and re.fullmatch(r"[A-Za-z]:\\", t.mount_root) is not None
            and re.fullmatch(r"\\\\\?\\Volume\{[0-9a-fA-F-]{36}\}\\", t.volume_guid) is not None
            and isinstance(t.disk_number, int) and not isinstance(t.disk_number, bool)
            and t.disk_number >= 0
            and all(bool(x) for x in (
                t.disk_instance_id, t.eject_instance_id, t.usb_physical_instance_id
            ))
        )

    def intake(
        self, arrival: Arrival, *, current_session_epoch: Callable[[], str],
    ) -> IntakeResult:
        if not self._authorized(arrival):
            return IntakeResult("RELEASE_BLOCKED", "UNAUTHORIZED_OR_UNPROVEN_SESSION")
        binding = arrival.binding
        target = binding.target
        epoch = binding.session_epoch
        try:
            with exclusive_intake(self.local_lock_file) as acquired:
                if not acquired:
                    return IntakeResult("RELEASE_BLOCKED", "OTHER_INTAKE_ACTIVE")
                gate = UsbSessionIoGate()
                session_port = self._port_factory()
                with gate.ordinary("msc"):
                    if current_session_epoch() != epoch:
                        return IntakeResult("RELEASE_BLOCKED", "STALE_SESSION")
                    session_port.revalidate_target(target, binding.cdc_port)
                    if not session_port.volume_available(target):
                        return IntakeResult("RELEASE_BLOCKED", "TARGET_UNAVAILABLE")
                    if self._preflight(target.mount_root) != target.expected_device_id:
                        return IntakeResult("RELEASE_BLOCKED", "DEVICE_IDENTITY_MISMATCH")
                    # Check the exact same target again before starting any copy.
                    session_port.revalidate_target(target, binding.cdc_port)
                    if current_session_epoch() != epoch:
                        return IntakeResult("RELEASE_BLOCKED", "STALE_SESSION")
                with gate.ordinary("msc"):
                    summary = self._sync(target.mount_root)
                if not isinstance(summary, SyncSummary) or summary.conflicts:
                    return IntakeResult("SYNC_FAILED", "SYNC_CONFLICT")
                capacity = probe_capacity(
                    binding, gate=gate, validator=session_port,
                    reader=self.capacity_reader,
                    durable_sync_complete=lambda: True,
                    current_session_epoch=current_session_epoch,
                )
                if capacity.release_blocked:
                    return IntakeResult(
                        "RELEASE_BLOCKED", "POST_SYNC_IDENTITY_UNVERIFIED",
                        summary.copied, summary.reused, capacity,
                    )
                return IntakeResult(
                    "RELEASE_BLOCKED", "AUTO_RELEASE_NOT_QUALIFIED",
                    summary.copied, summary.reused, capacity,
                )
        except Exception:
            # This may follow a partially completed #51 sync. Never claim release
            # and never expose exception strings (they can contain local paths).
            return IntakeResult("SYNC_FAILED", "INTAKE_OR_VALIDATION_ERROR")


class PollingMonitor:
    """Two consecutive stable observations; events never authorize CDC release.

    The provider must keep arrival epochs stable within one observed session
    and invalidate them after resume/re-enumeration. A restart may safely re-run
    #51, which remains no-clobber/idempotent; it never retries CDC.
    """

    def __init__(self, provider: ArrivalProvider, coordinator: IntakeCoordinator,
                 epoch: Callable[[], str], notify: Callable[[dict[str, object]], None],
                 *, stable_scans: int = 2) -> None:
        if stable_scans < 2:
            raise ValueError("stable_scans must be >=2")
        self.provider = provider
        self.coordinator = coordinator
        self.epoch = epoch
        self.notify = notify
        self.stable_scans = stable_scans
        self._seen: dict[tuple[str, str, str], int] = {}
        self._handled: set[tuple[str, str, str]] = set()

    @staticmethod
    def _key(a: Arrival) -> tuple[str, str, str]:
        b = a.binding
        return (b.target.volume_guid, b.target.usb_physical_instance_id,
                b.session_epoch)

    def poll_once(self) -> None:
        # Ambiguous duplicate claims never trigger intake.
        observed: dict[tuple[str, str, str], Arrival] = {}
        invalid: set[tuple[str, str, str]] = set()
        try:
            candidates = self.provider.scan()
        except Exception:
            self._seen.clear()
            self._handled.clear()
            self.notify(IntakeResult("RELEASE_BLOCKED", "ARRIVAL_OBSERVATION_ERROR").public_notice())
            return
        for candidate in candidates:
            if not isinstance(candidate, Arrival) or not isinstance(candidate.binding, VerifiedSession):
                continue
            key = self._key(candidate)
            if key in observed:
                invalid.add(key)
            observed[key] = candidate
        for key in set(self._seen) - set(observed):
            self._seen.pop(key, None)
            self._handled.discard(key)
        for key in invalid:
            self._seen.pop(key, None)
            observed.pop(key, None)
        for key, candidate in observed.items():
            if not self.coordinator._authorized(candidate):
                self._seen.pop(key, None)
                continue
            self._seen[key] = min(self.stable_scans, self._seen.get(key, 0) + 1)
            if self._seen[key] == self.stable_scans and key not in self._handled:
                self._handled.add(key)
                self.notify(IntakeResult("SYNCING", "VERIFIED_ARRIVAL").public_notice())
                outcome = self.coordinator.intake(candidate, current_session_epoch=self.epoch)
                self.notify(outcome.public_notice())

    def run_forever(self, *, interval_s: float = 2.0,
                    stop: Callable[[], bool] = lambda: False,
                    sleep: Callable[[float], None] = time.sleep) -> None:
        if interval_s <= 0:
            raise ValueError("interval must be positive")
        self.notify(IntakeResult("WAITING_FOR_DEVICE", "MONITOR_ACTIVE").public_notice())
        while not stop():
            self.poll_once()
            if not stop():
                sleep(interval_s)
