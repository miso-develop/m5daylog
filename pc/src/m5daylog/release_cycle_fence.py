"""#143 R3 host-side consumed-cycle ledger (source-only; no release sender).

A confirmed Device-origin releaseCycleId is necessary but not sufficient for
release. Caller must separately prove current-boot Device qualification,
manifest binding, exact MSC/CDC identity, RTC and #89 ordering. This module
cannot grant those proofs and is intentionally not wired to the CLI or watcher.

The independent HKCU continuity anchor detects one-sided local rollback, not
matched whole-host rollback, malicious same-user code, or hostile USB devices.
Only a separately approved supervised workflow may enroll/recover a host.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator, Protocol

from .release import PreconditionBlocked


_ZERO = hashlib.sha256(b"m5daylog:r3:uninitialized").hexdigest()
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _uuid(value: str) -> str:
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise PreconditionBlocked("unqualified release cycle identity") from None
    if canonical != value:
        raise PreconditionBlocked("noncanonical release cycle identity")
    return canonical


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _tip(previous: str, sequence: int, device: str, cycle: str, attempt: str) -> str:
    payload = json.dumps([previous, sequence, device, cycle, attempt],
                         separators=(",", ":")).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


class Anchor(Protocol):
    def read(self) -> dict[str, object] | None: ...
    def write(self, state: dict[str, object]) -> None: ...


class WindowsRegistryAnchor:
    """Per-user independent HKCU witness; not a trusted monotonic counter.

    RegFlushKey is a required Windows durability operation, but physical
    power-fail qualification of the exact host remains a separate Human Gate.
    Never emulate this anchor using SQLite or a file in the DB backup tree.
    """

    KEY = r"Software\M5Daylog\ReleaseFenceR3"
    VALUE = "ContinuityV1"

    def read(self) -> dict[str, object] | None:
        if os.name != "nt":
            raise PreconditionBlocked("Windows continuity anchor unavailable")
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY, 0,
                                winreg.KEY_READ) as key:
                value, kind = winreg.QueryValueEx(key, self.VALUE)
            if kind != winreg.REG_SZ or not isinstance(value, str):
                raise PreconditionBlocked("continuity anchor invalid")
            decoded = json.loads(value)
            if not isinstance(decoded, dict):
                raise PreconditionBlocked("continuity anchor invalid")
            return decoded
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            raise PreconditionBlocked("continuity anchor unreadable") from None

    def write(self, state: dict[str, object]) -> None:
        if os.name != "nt":
            raise PreconditionBlocked("Windows continuity anchor unavailable")
        import winreg

        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.KEY, 0,
                                    winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
                winreg.SetValueEx(key, self.VALUE, 0, winreg.REG_SZ,
                                  json.dumps(state, sort_keys=True, separators=(",", ":")))
                winreg.FlushKey(key)
        except OSError:
            raise PreconditionBlocked("continuity anchor flush uncertain") from None


@contextmanager
def _host_lock(path: Path) -> Iterator[None]:
    """One cooperating OS-level lock across processes, not a Device-media lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"0")
                stream.flush()
                os.fsync(stream.fileno())
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise PreconditionBlocked("release coordinator busy") from None
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise PreconditionBlocked("release coordinator busy") from None
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class CycleAttemptFence:
    """Strict off-Device dual-witness ledger for one known enrolled Device.

    SQLite commit is deliberately first. If the process crashes before the
    independent anchor flush, the stores disagree forever until separately
    supervised recovery: neither watcher nor normal CLI may reconcile them.

    This source-only primitive is NOT wired to RELEASE_STORAGE and must not
    be used as proof of production Windows power-loss durability.
    """

    def __init__(self, database: Path, lock_file: Path, device_root: Path,
                 anchor: Anchor) -> None:
        self.database = Path(database).expanduser().resolve()
        self.lock_file = Path(lock_file).expanduser().resolve()
        root = Path(device_root).expanduser().resolve()
        for path in (self.database, self.lock_file):
            if path == root or root in path.parents:
                raise PreconditionBlocked("host fence cannot be on Device media")
        if self.database == self.lock_file:
            raise PreconditionBlocked("independent release lock required")
        self.anchor = anchor
        self._serialized_thread = threading.local()

    @classmethod
    def for_current_user(cls, *, device_root: Path) -> "CycleAttemptFence":
        """Fixed Windows per-user store/lock identity for both CLI and watcher.

        No opt-in, enrollment, recovery, or release action is performed here.
        """
        if os.name != "nt" or not os.environ.get("LOCALAPPDATA"):
            raise PreconditionBlocked("Windows release continuity unavailable")
        state = Path(os.environ["LOCALAPPDATA"]) / "M5Daylog" / "state"
        return cls(state / "release_cycle_r3.sqlite",
                   state / "release_cycle_r3.lock",
                   device_root, WindowsRegistryAnchor())

    @contextmanager
    def serialized(self) -> Iterator[None]:
        """Hold for the full future CLI/watcher canonical coordinator call."""
        if getattr(self._serialized_thread, "held", False):
            raise PreconditionBlocked("nested release admission is unsupported")
        try:
            with _host_lock(self.lock_file):
                self._serialized_thread.held = True
                try:
                    yield
                finally:
                    self._serialized_thread.held = False
        except OSError:
            raise PreconditionBlocked("release host lock unavailable") from None

    def _connection(self) -> sqlite3.Connection:
        if not self.database.is_file():
            raise PreconditionBlocked("enrolled release ledger missing")
        db = sqlite3.connect(self.database, timeout=0.25, isolation_level=None)
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            db.close()
            raise PreconditionBlocked("release ledger integrity failure")
        return db

    @staticmethod
    def _validated(state: object) -> dict[str, object]:
        if not isinstance(state, dict) or set(state) != {"v", "device", "seq", "tip"}:
            raise PreconditionBlocked("invalid release continuity state")
        if (type(state["v"]) is not int or state["v"] != 1
                or not isinstance(state["device"], str)
                or not _HEX.fullmatch(state["device"])
                or type(state["seq"]) is not int or state["seq"] < 0
                or not isinstance(state["tip"], str)
                or not _HEX.fullmatch(state["tip"])):
            raise PreconditionBlocked("invalid release continuity state")
        return state

    def _verify(self, db: sqlite3.Connection, device: str) -> dict[str, object]:
        try:
            rows = db.execute("SELECT v, device, seq, tip FROM ledger_state").fetchall()
            if len(rows) != 1:
                raise PreconditionBlocked("release enrollment missing")
            v, stored, sequence, tip = rows[0]
            local = self._validated({"v": v, "device": stored, "seq": sequence, "tip": tip})
            remote = self._validated(self.anchor.read())
            if local != remote or local["device"] != device:
                raise PreconditionBlocked("host continuity mismatch")
            previous = _hash("m5daylog:r3:enrolled:" + device)
            observed = 0
            for seq, cycle, attempt, row_tip in db.execute(
                "SELECT seq, cycle, attempt, tip FROM consumed ORDER BY seq"
            ):
                observed += 1
                if (type(seq) is not int or seq != observed
                        or not isinstance(cycle, str) or not _HEX.fullmatch(cycle)
                        or not isinstance(attempt, str) or not _HEX.fullmatch(attempt)
                        or not isinstance(row_tip, str) or not _HEX.fullmatch(row_tip)):
                    raise PreconditionBlocked("release chain invalid")
                previous = _tip(previous, seq, device, cycle, attempt)
                if row_tip != previous:
                    raise PreconditionBlocked("release chain invalid")
            if observed != local["seq"] or previous != local["tip"]:
                raise PreconditionBlocked("release chain tip invalid")
            return local
        except sqlite3.Error:
            raise PreconditionBlocked("release ledger unreadable") from None

    def enroll(self, device_id: str, *, supervised: bool = False) -> None:
        """Explicit supervised initialization only; NEVER call from monitor/CLI.

        A true flag is an internal boundary, not human approval. An authorized
        external maintenance workflow must verify provenance independently.
        """
        device = _hash(_uuid(device_id))
        if not supervised:
            raise PreconditionBlocked("supervised enrollment required")
        with self.serialized():
            if self.database.exists() or self.anchor.read() is not None:
                raise PreconditionBlocked("enrollment or recovery already exists")
            self.database.parent.mkdir(parents=True, exist_ok=True)
            initial = {"v": 1, "device": device, "seq": 0,
                       "tip": _hash("m5daylog:r3:enrolled:" + device)}
            try:
                with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
                    db.execute("PRAGMA journal_mode=DELETE")
                    db.execute("PRAGMA synchronous=FULL")
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("CREATE TABLE ledger_state "
                               "(v INTEGER NOT NULL, device TEXT NOT NULL, "
                               "seq INTEGER NOT NULL, tip TEXT NOT NULL)")
                    db.execute("CREATE TABLE consumed "
                               "(seq INTEGER PRIMARY KEY, cycle TEXT UNIQUE NOT NULL, "
                               "attempt TEXT NOT NULL, tip TEXT NOT NULL)")
                    db.execute("INSERT INTO ledger_state VALUES (?,?,?,?)",
                               tuple(initial[k] for k in ("v", "device", "seq", "tip")))
                    db.execute("COMMIT")
                self.anchor.write(initial)
                with closing(self._connection()) as db:
                    self._verify(db, device)
            except (sqlite3.Error, OSError):
                raise PreconditionBlocked("release enrollment uncertain") from None

    def reserve_locked(self, device_id: str, release_cycle_id: str,
                       attempt_id: str) -> None:
        """Called ONLY while serialized() remains held through CDC outcome.

        The returned reservation is irrevocable, even if no bytes were sent.
        A caller must validate Device-origin cycle and exact target separately.
        """
        if not getattr(self._serialized_thread, "held", False):
            raise PreconditionBlocked("release OS lock required")
        device = _hash(_uuid(device_id))
        cycle = _hash(_uuid(release_cycle_id))
        attempt = _hash(_uuid(attempt_id))
        try:
            with closing(self._connection()) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    prior = self._verify(db, device)
                    if db.execute("SELECT 1 FROM consumed WHERE cycle=?", (cycle,)).fetchone():
                        raise PreconditionBlocked("release cycle already consumed")
                    seq = prior["seq"] + 1
                    tip = _tip(prior["tip"], seq, device, cycle, attempt)
                    db.execute("INSERT INTO consumed VALUES (?,?,?,?)",
                               (seq, cycle, attempt, tip))
                    db.execute("UPDATE ledger_state SET seq=?, tip=?", (seq, tip))
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
            updated = {"v": 1, "device": device, "seq": seq, "tip": tip}
            self.anchor.write(updated)
            with closing(self._connection()) as db:
                if self._verify(db, device) != updated:
                    raise PreconditionBlocked("continuity reread failed")
        except (sqlite3.Error, OSError):
            raise PreconditionBlocked("release attempt persistence uncertain") from None

    def check_locked(self, device_id: str) -> None:
        """Verify enrolled host witnesses under serialized() without mutation."""
        if not getattr(self._serialized_thread, "held", False):
            raise PreconditionBlocked("release OS lock required")
        device = _hash(_uuid(device_id))
        try:
            with closing(self._connection()) as db:
                self._verify(db, device)
        except (sqlite3.Error, OSError):
            raise PreconditionBlocked("release host continuity uncertain") from None
