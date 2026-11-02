"""#147 SDB-1.2.1: nonmutating R3 old-host Hpre, source-only.

This module cannot issue P0/P1, enroll an H0 host, arm a Device, or send CDC.
A healthy ledger is a NECESSARY candidate, not independent evidence of the
operator's previousDeviceId, real production profile, or approval. The future
owner issuer must corroborate those facts separately before P0.

The existing read_enrolled_state_locked() is NOT usable here: its connection
opens SQLite writable and changes journal_mode. Read-only Hpre deliberately
has a separate implementation while reusing the canonical R3 verifier.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import quote

from .release import PreconditionBlocked
from .release_cycle_fence import (
    CycleAttemptFence, _hash, _sqlite_recovery_sidecars_present, _uuid,
)


class HpreStatus(str, Enum):
    HEALTHY_V1_MATCHING_SEQ_GE_1 = "HEALTHY_V1_MATCHING_SEQ_GE_1"
    HEALTHY_V1_SEQ_0 = "HEALTHY_V1_SEQ_0"
    ABSENT_OR_UNPROVEN_H0 = "ABSENT_OR_UNPROVEN_H0"
    V2_OR_UNSUPPORTED = "V2_OR_UNSUPPORTED"
    SPLIT_OR_ROLLED_BACK = "SPLIT_OR_ROLLED_BACK"
    UNREADABLE_OR_UNKNOWN = "UNREADABLE_OR_UNKNOWN"


@dataclass(frozen=True)
class HpreResult:
    status: HpreStatus
    # Present ONLY for a healthy V1 consumed lineage, to bind a future P0.
    # Never a release grant or an authentication of the caller's old Device ID.
    lineage_digest: str | None = None
    sequence: int | None = None
    tip: str | None = None


def _unchanged_signature(path: Path) -> tuple[int, int, int]:
    s = path.stat()
    return s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _unspent_dev_witness_locked(fence: CycleAttemptFence) -> bool:
    """Only an unused, canonical-bound DEV one-shot can precede P0.

    The later SDB-1.1 DEV writer rejects any existing DEV SQLite or HKCU
    witness, even a healthy DEV_FRESH_INSTALL_FIRST_RELEASE record with an R3
    V1 ledger. A mere valid R3/DEV pair is therefore NOT a P0 candidate.
    Read-only presence checks under the shared lock must not open SQLite,
    recover journals, enroll, reset, or rewrite either continuity store.
    """
    database = fence._dev_cutover_database
    anchor = fence._dev_cutover_anchor
    if database is None or anchor is None:
        return False  # No positive path via a direct, unbound fixture fence.
    if (_sqlite_recovery_sidecars_present(database)
            or database.exists() or database.is_symlink()):
        return False
    return anchor.read() is None


def inspect_hpre_locked(
    fence: CycleAttemptFence, *, expected_previous_device_id: str,
) -> HpreResult:
    """Inspect existing R3 V1 DB + independent anchor under the R3 OS lock.

    Uses mode=ro and SELECT/read-only PRAGMAs, never writable _connection().
    Sidecars/hot journals are *not* recovered, not even for classification.
    No Device or Windows volume access. The separate DEV cutover witness is
    also checked so a previously spent generation cannot look like a new V1.
    This function assumes the caller separately established the original
    previousDeviceId and host/profile provenance; it does NOT grant P0.
    """
    unknown = HpreResult(HpreStatus.UNREADABLE_OR_UNKNOWN)
    if type(fence) is not CycleAttemptFence:
        # An injected object must not masquerade as a production fence.
        return unknown
    if not getattr(fence._serialized_thread, "held", False):
        return unknown
    try:
        old = _hash(_uuid(expected_previous_device_id))
        database = fence.database
        # Do not manufacture a lock file or directory for this observation.
        if (not fence.lock_file.is_file() or
                (os.name == "nt" and fence.lock_file.stat().st_size < 1)):
            return unknown
        if (_sqlite_recovery_sidecars_present(database)
                or not _unspent_dev_witness_locked(fence)):
            return unknown
        db_present = database.exists() or database.is_symlink()
        anchor = fence.anchor.read()
        if not db_present and anchor is None:
            return HpreResult(HpreStatus.ABSENT_OR_UNPROVEN_H0)
        if not db_present or anchor is None:
            return HpreResult(HpreStatus.SPLIT_OR_ROLLED_BACK)
        if database.is_symlink() or not database.is_file():
            return unknown

        signature = _unchanged_signature(database)
        # WAL/unknown SQLite header can require sidecar materialization even
        # with a readonly URI. Reject it before invoking SQLite at all.
        with database.open("rb") as stream:
            header = stream.read(100)
        if (len(header) != 100 or not header.startswith(b"SQLite format 3\x00")
                or header[18:20] != b"\x01\x01"):
            return unknown

        uri = "file:" + quote(database.as_posix(), safe="/:") + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as db:
            db.execute("PRAGMA query_only=ON")  # connection-local only
            if db.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                return unknown
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                return HpreResult(HpreStatus.SPLIT_OR_ROLLED_BACK)
            rows = db.execute("SELECT v FROM ledger_state").fetchall()
            if len(rows) != 1:
                return HpreResult(HpreStatus.SPLIT_OR_ROLLED_BACK)
            if type(rows[0][0]) is not int or rows[0][0] != 1:
                return HpreResult(HpreStatus.V2_OR_UNSUPPORTED)
            try:
                # Full V1 chain / tip / previous Device hash / HKCU match.
                state = fence._verify(db, old)
                # Preserve the canonical R3/DEV cutover consistency check,
                # but it is not sufficient: it can allow an already spent
                # DEV_FRESH_INSTALL witness alongside a healthy old R3 V1.
                fence._check_dev_cutover_locked(state)
            except PreconditionBlocked:
                return HpreResult(HpreStatus.SPLIT_OR_ROLLED_BACK)

        if (_sqlite_recovery_sidecars_present(database)
                or not _unspent_dev_witness_locked(fence)
                or _unchanged_signature(database) != signature
                or fence.anchor.read() != state):
            return unknown
        if state["seq"] == 0:
            return HpreResult(HpreStatus.HEALTHY_V1_SEQ_0)
        if state["seq"] < 1:
            return HpreResult(HpreStatus.SPLIT_OR_ROLLED_BACK)
        digest = hashlib.sha256(json.dumps(
            state, sort_keys=True, separators=(",", ":")
        ).encode("ascii")).hexdigest()
        return HpreResult(
            HpreStatus.HEALTHY_V1_MATCHING_SEQ_GE_1,
            lineage_digest=digest,
            sequence=state["seq"],
            tip=state["tip"],
        )
    except (PreconditionBlocked, sqlite3.Error, OSError, ValueError, TypeError,
            UnicodeError):
        return unknown


def inspect_current_user_hpre(*, expected_previous_device_id: str) -> HpreResult:
    """Production-path host-only *candidate* read using canonical R3 paths.

    Does not discover or authenticate expected_previous_device_id: the future
    independent owner record verifier must supply and corroborate it. Does not
    access Device, volume, CDC, RTC, or issue any owner permit. An absent or
    zero-byte lock file is UNKNOWN rather than implicitly created/repaired.
    """
    unknown = HpreResult(HpreStatus.UNREADABLE_OR_UNKNOWN)
    if os.name != "nt" or not os.environ.get("LOCALAPPDATA"):
        return unknown
    state_dir = Path(os.environ["LOCALAPPDATA"]) / "M5Daylog" / "state"
    lock = state_dir / "release_cycle_r3.lock"
    try:
        if not lock.is_file() or lock.stat().st_size < 1:
            return unknown
        # Device path is a never-accessed sentinel, not an actual media root.
        # The fixed per-user R3 factory still selects real DB/HKCU/lock paths.
        fence = CycleAttemptFence.for_current_user(
            device_root=state_dir / "__hpre_no_device_access__",
        )
        if fence.lock_file != lock.resolve():
            return unknown
        with fence.serialized():
            return inspect_hpre_locked(
                fence, expected_previous_device_id=expected_previous_device_id,
            )
    except (PreconditionBlocked, OSError, ValueError):
        return unknown
