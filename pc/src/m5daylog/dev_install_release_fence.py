"""#147 FI2-I2: independent, source-only DEV first-release consumption.

This is NOT a permit issuer, installed release sender, proof of H0, or
physical-host persistence certification. Only a separately verified private
DEV_FRESH_INSTALL receipt can be considered by a future authorized caller.
Neither the CLI nor watcher imports this module. It never accesses Device media.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import quote

from .release import PreconditionBlocked
from .release_cycle_fence import Anchor, CycleAttemptFence, WindowsRegistryAnchor

_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _uuid(value: str) -> str:
    if type(value) is not str:
        raise PreconditionBlocked("DEV receipt UUID invalid")
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise PreconditionBlocked("DEV receipt UUID invalid") from None
    if value != parsed:
        raise PreconditionBlocked("DEV receipt UUID noncanonical")
    return parsed


def _digest(value: str) -> str:
    if type(value) is not str or not _HEX.fullmatch(value):
        raise PreconditionBlocked("DEV receipt digest invalid")
    return value


@dataclass(frozen=True, repr=False)
class DevInstallReceipt:
    """Private receipt *descriptor*, NOT authority or a self-issued permit.

    Digests name independently acquired material; they do not validate it.
    A verifier for the actual independently approved private evidence is
    deliberately not implemented here. Synthetic verifiers are test-only.
    """
    device_id: str
    generation_id: str
    first_release_attempt_id: str
    physical_target_digest: str
    host_lineage_digest: str
    image_set_digest: str
    partition_layout_digest: str
    nvs_write_set_digest: str
    admission_inventory_digest: str
    external_permit_digest: str
    media_branch: str

    def fingerprint(self) -> str:
        if self.media_branch not in ("C", "R", "A"):
            raise PreconditionBlocked("DEV media branch not qualified")
        values = [
            _uuid(self.device_id), _uuid(self.generation_id),
            _uuid(self.first_release_attempt_id),
            _digest(self.physical_target_digest),
            _digest(self.host_lineage_digest),
            _digest(self.image_set_digest),
            _digest(self.partition_layout_digest),
            _digest(self.nvs_write_set_digest),
            _digest(self.admission_inventory_digest),
            _digest(self.external_permit_digest), self.media_branch,
        ]
        return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode("ascii")).hexdigest()


class DevInstallRegistryAnchor(WindowsRegistryAnchor):
    """Separate HKCU continuity witness, not the normal R3 cycle anchor."""
    KEY = r"Software\M5Daylog\DevInstallReleaseV1"
    VALUE = "ConsumedV1"


class DevInstallReleaseFence:
    """One new installation-generation attempt, burned before any CDC send.

    Shares the existing #143 R3 OS-wide coordinator lock, but uses a
    separate SQLite journal and independent HKCU anchor. A single-store
    rollback, partial commit or missing store blocks without auto-repair.
    Matched whole-host rollback remains outside the approved R3 Option A model.

    This primitive is intentionally UNWIRED. A truthful external validator,
    selected-APP and GPIO/RTC/PCM/manifest/ownership evidence, and canonical
    #89 sender coupling are not supplied by a Python receipt descriptor.
    """

    def __init__(self, *, cycles: CycleAttemptFence, database: Path,
                 device_root: Path, anchor: Anchor) -> None:
        self.cycles = cycles
        self.database = Path(database).expanduser().resolve()
        root = Path(device_root).expanduser().resolve()
        if (self.database == root or root in self.database.parents
                or self.database in (cycles.database, cycles.lock_file)
                or anchor is cycles.anchor):
            raise PreconditionBlocked("DEV ledger/anchor isolation invalid")
        self.anchor = anchor

    @classmethod
    def for_current_user(cls, *, device_root: Path) -> "DevInstallReleaseFence":
        cycles = CycleAttemptFence.for_current_user(device_root=device_root)
        return cls(cycles=cycles,
                   database=cycles.database.with_name("dev_install_release_v1.sqlite"),
                   device_root=device_root, anchor=DevInstallRegistryAnchor())

    def _require_lock(self) -> None:
        if not getattr(self.cycles._serialized_thread, "held", False):
            raise PreconditionBlocked("shared release OS lock required")

    @staticmethod
    def _state(receipt: DevInstallReceipt) -> dict[str, object]:
        return {"v": 1, "kind": "DEV_FRESH_INSTALL_FIRST_RELEASE",
                "state": "CONSUMED", "receipt": receipt.fingerprint()}

    def _read(self) -> dict[str, object]:
        if not self.database.is_file():
            raise PreconditionBlocked("DEV release consumption absent")
        if any(self.database.with_name(self.database.name + ext).exists()
               for ext in ("-journal", "-wal", "-shm")):
            raise PreconditionBlocked("DEV release SQLite recovery uncertain")
        try:
            with closing(sqlite3.connect(
                    "file:" + quote(self.database.as_posix(), safe="/:") + "?mode=ro",
                    uri=True,
                    timeout=0.25)) as db:
                if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise PreconditionBlocked("DEV release ledger corrupt")
                rows = db.execute("SELECT v, kind, state, receipt FROM dev_consumed").fetchall()
            if len(rows) != 1:
                raise PreconditionBlocked("DEV release ledger ambiguous")
            v, kind, state, receipt = rows[0]
            if (type(v) is not int or v != 1
                    or kind != "DEV_FRESH_INSTALL_FIRST_RELEASE"
                    or state != "CONSUMED"
                    or type(receipt) is not str or not _HEX.fullmatch(receipt)):
                raise PreconditionBlocked("DEV release ledger invalid")
            return {"v": v, "kind": kind, "state": state, "receipt": receipt}
        except (sqlite3.Error, OSError, ValueError):
            raise PreconditionBlocked("DEV release ledger unreadable") from None

    def verify_consumed_locked(self, receipt: DevInstallReceipt) -> None:
        """Observe the exact separately consumed #1 attempt, never re-arm it.

        This check can later correlate the first enrolled R3 cycle, but is
        not a standalone proof of Device first-use origin or a send permit.
        """
        self._require_lock()
        expected = self._state(receipt)
        local = self._read()
        try:
            remote = self.anchor.read()
        except (OSError, ValueError):
            raise PreconditionBlocked("DEV release anchor unreadable") from None
        if remote != expected or local != expected:
            raise PreconditionBlocked("DEV first release continuity mismatch")

    def consume_locked(self, receipt: DevInstallReceipt, *,
                       verify_external: Callable[[DevInstallReceipt], bool]) -> None:
        """Irreversibly consume #1 under an already-held #143 OS-wide lock.

        The external verifier MUST independently authenticate/approve the
        private H0 + exact-install/target/branch/generation/first-release
        record; no default verifier or public fixture can grant authority.
        Use before any possible RTC/CDC mutation, never as a retry primitive.
        """
        self._require_lock()
        if type(receipt) is not DevInstallReceipt or not callable(verify_external):
            raise PreconditionBlocked("DEV release external permit unavailable")
        expected = self._state(receipt)
        if verify_external(receipt) is not True:
            raise PreconditionBlocked("DEV release external permit unqualified")
        # Confirm actual R3 H0 two-store absence before the first consume.
        # This check alone is never evidence of never-enrolled history.
        self.cycles.check_unenrolled_locked()
        try:
            local_exists = self.database.exists()
            remote = self.anchor.read()
            if local_exists or remote is not None:
                raise PreconditionBlocked("DEV release generation already used or uncertain")
            self.database.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE dev_consumed (v INTEGER NOT NULL, "
                           "kind TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT NOT NULL)")
                db.execute("INSERT INTO dev_consumed VALUES (?,?,?,?)",
                           tuple(expected[k] for k in ("v", "kind", "state", "receipt")))
                db.execute("COMMIT")
            # A crash after SQLite and before HKCU flush blocks permanently.
            self.anchor.write(expected)
            self.verify_consumed_locked(receipt)
        except (sqlite3.Error, OSError, ValueError):
            raise PreconditionBlocked("DEV first release persistence uncertain") from None

    def initialize_first_dev_cycle_locked(
        self, receipt: DevInstallReceipt, *, status: Mapping[str, object],
        cycle_attempt_id: str,
        verify_first_wake: Callable[[DevInstallReceipt, Mapping[str, object]], bool],
    ) -> None:
        """Model #147 FI2-I3: distinct first-cycle consumption after #1.

        This is source-only and UNWIRED. The independent external verifier
        must establish first #105 accepted release/disappearance, Device
        SHUTDOWN_ARMED, physically authorized manual WAKE, terminal current
        PCM/manifest, RTC, exact USB parent, #51 SYNCED and H0 lineage.
        A fake status/proof callback must NEVER authorize a live CDC sender.
        """
        self._require_lock()
        self.verify_consumed_locked(receipt)
        # R3 enrollment must STILL be absent; DEV journal is independent.
        self.cycles.check_unenrolled_locked()
        if not isinstance(status, Mapping) or not callable(verify_first_wake):
            raise PreconditionBlocked("DEV first-WAKE evidence unavailable")
        cycle = _uuid(status.get("releaseCycleId"))
        attempt = _uuid(cycle_attempt_id)
        if (status.get("releaseCycleOrigin") != "SUPERVISED_FIRST_USE"
                or status.get("state") != "USB_SYNC"
                or type(status.get("rtcCorrectionPending")) is not bool
                or attempt == receipt.first_release_attempt_id):
            raise PreconditionBlocked("DEV first cycle status invalid")
        if verify_first_wake(receipt, status) is not True:
            raise PreconditionBlocked("DEV first-WAKE external proof invalid")
        self.cycles.initialize_first_cycle_locked(receipt.device_id, cycle, attempt)
        self.cycles.verify_reserved_locked(receipt.device_id, cycle, attempt)
