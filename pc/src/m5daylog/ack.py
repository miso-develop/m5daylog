"""Task #52: durable PC ACK outbox and bounded pre-release delivery.

This module does not own USB release, device ingestion, or retention. #89
must supply an admission-controlled write lease and a proven atomic final-file
commit adapter. Without those dependencies it cannot write Device storage.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .sync import default_contracts_dir, validate_contract_document

_ACK_PATH = Path("M5DAYLOG") / "acks" / "processed.json"
_OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS ack_outbox (
    recording_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    pending_since TEXT NOT NULL,
    last_delivered_at TEXT
)
"""
# Device receiver bounds (current firmware/processed_ack.c).
_ACK_MAX_IDS = 1024
_ACK_MAX_BYTES = 128 * 1024

# Preserve validated IDs from an existing final even if a prior PC's
# processing database is unavailable. This is transport debt, not proof that
# the Device has applied any ID or that local AI stages have succeeded.
_CARRYOVER_DDL = """
CREATE TABLE IF NOT EXISTS ack_carryover (
    recording_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    pending_since TEXT NOT NULL,
    last_delivered_at TEXT
)
"""


class AckSafetyError(RuntimeError):
    """ACK delivery prerequisites failed; never send a release-success signal."""


class AckAdmissionDenied(AckSafetyError):
    """#89 gate sealed: defer without opening Device storage."""


class AckHandleUnsafeError(AckSafetyError):
    """A Device I/O handle or lease failed to close; #89 must not release."""


class AtomicAckPort(Protocol):
    """#89-supplied, filesystem-qualified commit boundary.

    replace_atomic must preserve the old complete final if replacement fails,
    and never publish a partially written final. flush_final must establish
    required host-side durability before acknowledging delivery.
    """

    def replace_atomic(self, temporary: Path, final: Path) -> None: ...

    def flush_final(self, final: Path) -> None: ...


@dataclass(frozen=True)
class VerifiedSyncBarrier:
    """Provided by orchestration only after validated #51 sync returns."""

    device_id: str
    durable_sync_completed: bool
    conflicts: int


@dataclass(frozen=True)
class AckFlushResult:
    status: str  # NO_OP, DELIVERED, or DEFERRED
    attempted: int = 0
    reason: str | None = None


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_pending(db_path: Path, device_id: str) -> tuple[str, ...]:
    """Snapshot at most one Device-sized, fair ACK batch without awaiting AI.

    No outbox row is removed after publication: a host write is not Device
    ingestion proof. Ineligible rows remain durable but cannot be sent until
    the four stages and the source hash are again valid. Previously published
    IDs retained in ack_carryover are retried independently of this PC's AI.
    """
    if not Path(db_path).is_file():
        raise AckSafetyError("local state database is missing")
    connection = sqlite3.connect(str(db_path), timeout=5)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(_OUTBOX_DDL)
        connection.execute(_CARRYOVER_DDL)
        connection.execute(
            """
            INSERT OR IGNORE INTO ack_outbox
                (recording_id, device_id, source_sha256, pending_since)
            SELECT recording_id, device_id, source_sha256, ?
            FROM recordings
            WHERE device_id=? AND sync_status='SYNCED'
              AND vad_status='SUCCEEDED' AND stt_status='SUCCEEDED'
              AND diarization_status='SUCCEEDED' AND day_status='SUCCEEDED'
              AND source_sha256 IS NOT NULL
            """,
            (_timestamp(), device_id),
        )
        result = connection.execute(
            """
            SELECT recording_id FROM (
                SELECT a.recording_id, a.pending_since, a.last_delivered_at
                FROM ack_outbox a JOIN recordings r
                  ON r.recording_id=a.recording_id
                WHERE a.device_id=? AND r.device_id=a.device_id
                  AND r.sync_status='SYNCED'
                  AND r.source_sha256=a.source_sha256
                  AND r.vad_status='SUCCEEDED'
                  AND r.stt_status='SUCCEEDED'
                  AND r.diarization_status='SUCCEEDED'
                  AND r.day_status='SUCCEEDED'
                UNION ALL
                SELECT c.recording_id, c.pending_since, c.last_delivered_at
                FROM ack_carryover c WHERE c.device_id=?
            )
            GROUP BY recording_id
            ORDER BY MIN(CASE WHEN last_delivered_at IS NULL THEN 0 ELSE 1 END),
                     MIN(COALESCE(last_delivered_at, '')),
                     MIN(pending_since), recording_id
            LIMIT ?
            """,
            (device_id, device_id, _ACK_MAX_IDS),
        ).fetchall()
        connection.commit()
        return tuple(row[0] for row in result)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _remember_prior_ids(
    db_path: Path, device_id: str, existing_ids: list[str]
) -> tuple[str, ...]:
    """Durably preserve untracked IDs before any replacement of the old final.

    IDs already in ack_outbox stay bound to their original source hash/stages;
    invalidated rows are not converted into unqualified carryover entries.
    """
    connection = sqlite3.connect(str(db_path), timeout=5)
    inserted: list[str] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(_CARRYOVER_DDL)
        for item in set(existing_ids):
            if connection.execute(
                "SELECT 1 FROM ack_outbox WHERE recording_id=?", (item,)
            ).fetchone():
                continue
            cursor = connection.execute(
                "INSERT OR IGNORE INTO ack_carryover "
                "(recording_id,device_id,pending_since) VALUES(?,?,?)",
                (item, device_id, _timestamp()),
            )
            if cursor.rowcount:
                inserted.append(item)
        connection.commit()
        return tuple(sorted(inserted))
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _mark_delivered(db_path: Path, ids: tuple[str, ...], device_id: str) -> None:
    """Keep both durable queues; a write is not Device-side ingestion proof."""
    connection = sqlite3.connect(str(db_path), timeout=5)
    try:
        connection.execute("BEGIN IMMEDIATE")
        delivered_at = _timestamp()
        connection.executemany(
            "UPDATE ack_outbox SET last_delivered_at=? "
            "WHERE device_id=? AND recording_id=?",
            ((delivered_at, device_id, item) for item in ids),
        )
        connection.executemany(
            "UPDATE ack_carryover SET last_delivered_at=? "
            "WHERE device_id=? AND recording_id=?",
            ((delivered_at, device_id, item) for item in ids),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _validate_device_identity(mount: Path, expected_id: str, contracts: Path) -> None:
    root = mount / "M5DAYLOG"
    try:
        device = json.loads((root / "device.json").read_text(encoding="utf-8"))
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        validate_contract_document(device, contracts / "device.schema.json")
        validate_contract_document(manifest, contracts / "manifest.schema.json")
    except (OSError, ValueError, TypeError):
        raise AckSafetyError("Device metadata validation failed") from None
    if device["deviceId"] != expected_id or manifest["deviceId"] != expected_id:
        raise AckSafetyError("Device identity changed")


def _ack_directory(mount: Path) -> Path:
    """Refuse reparse/symlink redirection from a mounted Device ACK path."""
    base = mount.resolve(strict=True)
    root = base / "M5DAYLOG"
    target = root / "acks"
    if root.is_symlink() or target.is_symlink():
        raise AckSafetyError("ACK directory redirection")
    if not root.is_dir() or not target.is_dir():
        raise AckSafetyError("ACK directory is missing")
    if target.resolve(strict=True) != target:
        raise AckSafetyError("ACK directory containment violation")
    return target


def _commit_file(final: Path, data: bytes, port: AtomicAckPort) -> None:
    temporary = final.with_name("processed.json.tmp")
    # The old final is never removed, even if a previous temporary is stale.
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()  # lexical entry, never follows an alias
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        stream = os.fdopen(descriptor, "wb")
    except Exception:
        try:
            os.close(descriptor)
        except Exception:
            raise AckHandleUnsafeError(
                "ACK temporary descriptor close unconfirmed"
            ) from None
        raise
    write_error = None
    try:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    except Exception as exc:
        write_error = exc
    finally:
        try:
            stream.close()
        except Exception:
            raise AckHandleUnsafeError("ACK temporary handle close failed") from None
    if write_error is not None:
        raise write_error
    port.replace_atomic(temporary, final)
    port.flush_final(final)


def flush_pre_release_acks(
    db_path: str | Path,
    mount_root: str | Path,
    barrier: VerifiedSyncBarrier,
    *,
    write_lease: AbstractContextManager[object],
    atomic_port: AtomicAckPort,
    contracts_dir: str | Path | None = None,
) -> AckFlushResult:
    """Snapshot once, then optionally deliver without waiting for AI stages.

    The #89 coordinator owns lease admission/cutoff, lease teardown, required
    CDC operations, and release. Call only after the #51 sync success barrier.
    Failed writes leave local outbox pending; unsafe close errors propagate.
    """
    if not barrier.durable_sync_completed or barrier.conflicts != 0:
        raise AckSafetyError("verified sync barrier is not satisfied")
    if not barrier.device_id:
        raise AckSafetyError("validated Device ID is missing")
    db = Path(db_path)
    ids = _snapshot_pending(db, barrier.device_id)
    if not ids:
        return AckFlushResult("NO_OP")

    mount = Path(mount_root)
    contracts = Path(contracts_dir) if contracts_dir is not None else default_contracts_dir()
    payload = {
        "schemaVersion": 1,
        "deviceId": barrier.device_id,
        "processedAt": _timestamp(),
        "recordingIds": list(ids),
    }
    try:
        validate_contract_document(payload, contracts / "processed-ack.schema.json")
        data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    except (OSError, ValueError, TypeError):
        return AckFlushResult("DEFERRED", len(ids), "payload-validation-failed")

    # Admission/teardown are separate from payload errors. A sealed gate
    # rejects without acquiring any Device handle; failed lease teardown
    # must block #89 even if the ACK payload failure is otherwise retryable.
    try:
        write_lease.__enter__()
    except AckAdmissionDenied:
        return AckFlushResult("DEFERRED", len(ids), "release-gate-sealed")
    except Exception:
        raise AckHandleUnsafeError("ACK lease admission outcome uncertain") from None
    write_error = None
    try:
        try:
            _validate_device_identity(mount, barrier.device_id, contracts)
            ack_dir = _ack_directory(mount)
            final = ack_dir / "processed.json"
            if final.is_symlink():
                raise AckSafetyError("ACK final is redirected")
            if final.exists():
                existing = json.loads(final.read_text(encoding="utf-8"))
                validate_contract_document(existing, contracts / "processed-ack.schema.json")
                if existing["deviceId"] != barrier.device_id:
                    raise AckSafetyError("existing ACK Device identity mismatch")
                # Schema v1 has no ID cap: a legacy final may be too large
                # for the Device receiver. Preserve *all* its ID debt locally
                # before replacing it with one bounded, replayable batch.
                # Commit any previously published, locally untracked IDs to
                # SQLite *before* replacing the only authoritative copy.
                new_prior_ids = _remember_prior_ids(
                    db, barrier.device_id, existing["recordingIds"]
                )
            else:
                new_prior_ids = ()
            # Give newly recovered prior-final IDs priority this attempt.
            # Every omitted outbox/carryover ID stays durable and is retried
            # fairly by least-recently-delivered time on subsequent sessions.
            selected = tuple(dict.fromkeys((*new_prior_ids, *ids)))[:_ACK_MAX_IDS]
            payload["recordingIds"] = sorted(selected)
            validate_contract_document(payload, contracts / "processed-ack.schema.json")
            data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            if len(data) > _ACK_MAX_BYTES:
                raise AckSafetyError("ACK exceeds Device byte limit")
            _commit_file(final, data, atomic_port)
        except Exception as exc:
            write_error = exc
    finally:
        try:
            write_lease.__exit__(None, None, None)
        except Exception:
            raise AckHandleUnsafeError("ACK lease teardown failed") from None
    if isinstance(write_error, AckHandleUnsafeError):
        raise write_error
    if write_error is not None:
        return AckFlushResult("DEFERRED", len(ids), type(write_error).__name__)

    try:
        _mark_delivered(db, selected, barrier.device_id)
    except sqlite3.Error:
        return AckFlushResult("DEFERRED", len(ids), "local-delivery-record-failed")
    return AckFlushResult("DELIVERED", len(selected))
