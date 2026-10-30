"""#135 conservative, durable, cross-process RELEASE_STORAGE attempt admission.

This is deliberately DEVICE-scoped, not an invented USB session key: CDC v1
does not expose a durable session generation. It permits at most one attempt
for each known Device across all process restarts. It is NOT a usable daily
release policy and is never enabled by the unattended monitor. Do not clear
this ledger as an automatic recovery or after an uncertain CDC response.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from pathlib import Path
from contextlib import closing

from .release import EjectTarget, PreconditionBlocked


class DeviceAttemptFence:
    """Durable pre-send fence on a host-owned path, outside all Device volumes.

    A successfully committed row means a RELEASE_STORAGE *may* already have
    been sent. Only explicit external review of a proven new Device session
    can later justify any change to this deliberately one-shot policy.
    """

    def __init__(self, database: Path, *, device_root: Path) -> None:
        self.database = Path(database).expanduser().resolve()
        self.device_root = Path(device_root).expanduser().resolve()
        try:
            self.database.relative_to(self.device_root)
        except ValueError:
            pass
        else:
            raise PreconditionBlocked("attempt fence cannot live on Device media")
        if self.database == self.device_root or self.database.name == "":
            raise PreconditionBlocked("invalid host journal location")

    def reserve(self, attempt_id: str, target: EjectTarget) -> None:
        if not isinstance(target, EjectTarget):
            raise PreconditionBlocked("target not qualified")
        try:
            attempt = str(uuid.UUID(attempt_id))
            device = str(uuid.UUID(target.expected_device_id))
        except (TypeError, ValueError, AttributeError):
            raise PreconditionBlocked("invalid release identity") from None
        if attempt != attempt_id or device != target.expected_device_id:
            raise PreconditionBlocked("noncanonical release identity")
        device_key = hashlib.sha256(device.encode("ascii")).hexdigest()
        attempt_key = hashlib.sha256(attempt.encode("ascii")).hexdigest()
        try:
            self.database.parent.mkdir(parents=True, exist_ok=True)
            # sqlite3 EXCLUSIVE transaction/UNIQUE constraint arbitrates
            # independent Python processes; a lock file alone cannot persist
            # an attempted send through process termination.
            # sqlite3.Connection.__exit__ commits but does NOT close. The
            # explicit closing guard is required on Windows to release the
            # handle before another process or test deletes the DB.
            with closing(sqlite3.connect(self.database, timeout=1.0,
                                         isolation_level=None)) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS release_attempts ("
                    "device_key TEXT PRIMARY KEY, attempt_key TEXT NOT NULL)"
                )
                db.execute("BEGIN IMMEDIATE")
                try:
                    db.execute(
                        "INSERT INTO release_attempts(device_key, attempt_key) "
                        "VALUES (?, ?)", (device_key, attempt_key)
                    )
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
        except (sqlite3.Error, OSError):
            # Duplicate, unreadable/corrupt journal, lock conflict or disk
            # failure: treat as possibly attempted. Never silently reset.
            raise PreconditionBlocked("release attempt not durably reserved") from None
