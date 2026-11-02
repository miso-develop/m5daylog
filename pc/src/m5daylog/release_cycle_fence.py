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
from urllib.parse import quote

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


def _sqlite_recovery_sidecars_present(database: Path) -> bool:
    """Reject orphaned SQLite state before any writable enrollment connection.

    Connecting to a missing DB can recover/delete a leftover hot journal.
    Treat even dangling sidecar symlinks as ambiguous host state.
    """
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = database.with_name(database.name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            return True
    return False


def _tip(previous: str, sequence: int, device: str, cycle: str, attempt: str) -> str:
    payload = json.dumps([previous, sequence, device, cycle, attempt],
                         separators=(",", ":")).encode("ascii")
    return hashlib.sha256(payload).hexdigest()



def _generation_tip(previous: str, after_seq: int, new_device: str,
                    generation: str, grant: str) -> str:
    """Domain-separated append-only transition; never a reset to H0."""
    payload = json.dumps(
        ["m5daylog:r3:dev-generation:v2", previous, after_seq,
         new_device, generation, grant], separators=(",", ":"),
    ).encode("ascii")
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

        # A missing key can represent H0. A present key missing its value is
        # instead ambiguous partial state and must never authorize enrollment.
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY, 0,
                                 winreg.KEY_READ)
        except FileNotFoundError:
            return None
        except OSError:
            raise PreconditionBlocked("continuity anchor unreadable") from None
        try:
            with key:
                try:
                    value, kind = winreg.QueryValueEx(key, self.VALUE)
                except FileNotFoundError:
                    raise PreconditionBlocked("continuity anchor incomplete") from None
            if kind != winreg.REG_SZ or not isinstance(value, str):
                raise PreconditionBlocked("continuity anchor invalid")
            decoded = json.loads(value)
            if not isinstance(decoded, dict):
                raise PreconditionBlocked("continuity anchor invalid")
            return decoded
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
        # Keep the caller's original host paths for read-only Hpre. A
        # symlink/junction existing BEFORE construction must not disappear
        # merely because the active release journal resolves its target.
        self._database_source_path = Path(database).expanduser().absolute()
        self._lock_source_path = Path(lock_file).expanduser().absolute()
        self.database = self._database_source_path.resolve()
        self.lock_file = self._lock_source_path.resolve()
        root = Path(device_root).expanduser().resolve()
        for path in (self.database, self.lock_file):
            if path == root or root in path.parents:
                raise PreconditionBlocked("host fence cannot be on Device media")
        if self.database == self.lock_file:
            raise PreconditionBlocked("independent release lock required")
        self.anchor = anchor
        self._serialized_thread = threading.local()
        self._dev_cutover_database: Path | None = None
        # Retain the un-resolved binding path: Hpre must detect a DEV
        # symlink/junction even when resolve() points to an absent target.
        self._dev_cutover_source_path: Path | None = None
        self._dev_cutover_anchor: Anchor | None = None

    @classmethod
    def for_current_user(cls, *, device_root: Path) -> "CycleAttemptFence":
        """Fixed Windows per-user store/lock identity for both CLI and watcher.

        No opt-in, enrollment, recovery, or release action is performed here.
        """
        if os.name != "nt" or not os.environ.get("LOCALAPPDATA"):
            raise PreconditionBlocked("Windows release continuity unavailable")
        state = Path(os.environ["LOCALAPPDATA"]) / "M5Daylog" / "state"
        fence = cls(state / "release_cycle_r3.sqlite",
                    state / "release_cycle_r3.lock",
                    device_root, WindowsRegistryAnchor())
        # Canonical CLI/watch fences must observe DEV consumption even when
        # their caller does not instantiate DevInstallReleaseFence separately.
        from .dev_install_release_fence import DevInstallRegistryAnchor
        fence.bind_dev_cutover_witness(
            state / "dev_install_release_v1.sqlite", DevInstallRegistryAnchor(),
        )
        return fence

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

    def bind_dev_cutover_witness(self, database: Path, anchor: Anchor) -> None:
        """Bind independent DEV consumption for all cooperating release paths."""
        # absolute() does not follow a symlink; resolve() does. Preserve
        # both so the read-only Hpre can reject a dangling/redirected
        # *nominal* DEV path instead of trusting an absent resolved target.
        source = Path(database).expanduser().absolute()
        target = source.resolve()
        if (target == self.database or target == self.lock_file
                or not callable(getattr(anchor, "read", None))):
            raise PreconditionBlocked("DEV cutover witness invalid")
        if (self._dev_cutover_database is not None
                and self._dev_cutover_database != target):
            raise PreconditionBlocked("DEV cutover witness already bound")
        # Native Windows may express the same already-bound target with an
        # extended-length path on the second bind. Preserve the ORIGINAL
        # lexical path for Hpre even when an equivalent alias is rebound.
        if self._dev_cutover_source_path is None:
            self._dev_cutover_source_path = source
        self._dev_cutover_database = target
        self._dev_cutover_anchor = anchor

    def _check_dev_cutover_locked(self, state: dict[str, object]) -> None:
        """Block old V1 after DEV commit, including pre-R3-transaction failures.

        Required by reservation and pre-send proof, not an optional caller
        authorization callback. Never repairs or deletes a witness.
        """
        if self._dev_cutover_database is None:
            return  # Isolated legacy fixture; canonical factory always binds.
        database, anchor = self._dev_cutover_database, self._dev_cutover_anchor
        if anchor is None or _sqlite_recovery_sidecars_present(database):
            raise PreconditionBlocked("DEV cutover continuity uncertain")
        try:
            remote = anchor.read()
            exists = database.exists()
            if not exists and remote is None:
                if state["v"] != 1:
                    raise PreconditionBlocked("DEV cutover witness missing")
                return
            if not database.is_file() or remote is None:
                raise PreconditionBlocked("DEV cutover witness incomplete")
            with closing(sqlite3.connect(
                    "file:" + quote(database.as_posix(), safe="/:") + "?mode=ro",
                    uri=True, timeout=0.25)) as db:
                if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise PreconditionBlocked("DEV cutover ledger invalid")
                rows = db.execute(
                    "SELECT v, kind, state, receipt FROM dev_consumed"
                ).fetchall()
            if len(rows) != 1:
                raise PreconditionBlocked("DEV cutover ledger ambiguous")
            v, kind, consumed, receipt = rows[0]
            local = {"v": v, "kind": kind, "state": consumed, "receipt": receipt}
            if (type(v) is not int or v != 1
                    or kind not in ("DEV_FRESH_INSTALL_FIRST_RELEASE",
                                    "SAME_HOST_DEV_GENERATION_FIRST_RELEASE")
                    or consumed != "CONSUMED"
                    or type(receipt) is not str or not _HEX.fullmatch(receipt)
                    or local != remote):
                raise PreconditionBlocked("DEV cutover witness mismatch")
            if kind == "SAME_HOST_DEV_GENERATION_FIRST_RELEASE" and state["v"] != 2:
                raise PreconditionBlocked("DEV cutover pending; old generation blocked")
            if kind == "DEV_FRESH_INSTALL_FIRST_RELEASE" and state["v"] != 1:
                raise PreconditionBlocked("legacy first-use witness cannot enroll V2")
        except (sqlite3.Error, OSError, ValueError, TypeError):
            raise PreconditionBlocked("DEV cutover continuity uncertain") from None

    def _connection(self) -> sqlite3.Connection:
        # Opening a pre-existing ledger writable may replay or delete SQLite
        # recovery sidecars before the independent HKCU witness is compared.
        # Preserve uncertain state for a separately authorized investigation.
        if _sqlite_recovery_sidecars_present(self.database):
            raise PreconditionBlocked("release SQLite recovery uncertain")
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
        if not isinstance(state, dict):
            raise PreconditionBlocked("invalid release continuity state")
        version = state.get("v")
        expected = ({"v", "device", "seq", "tip"} if version == 1 else
                    {"v", "device", "seq", "tip", "active_device", "generation"}
                    if version == 2 else set())
        if set(state) != expected or type(version) is not int:
            raise PreconditionBlocked("invalid release continuity state")
        if (not isinstance(state["device"], str)
                or not _HEX.fullmatch(state["device"])
                or type(state["seq"]) is not int or state["seq"] < 0
                or not isinstance(state["tip"], str)
                or not _HEX.fullmatch(state["tip"])):
            raise PreconditionBlocked("invalid release continuity state")
        if version == 2 and (
                not isinstance(state["active_device"], str)
                or not _HEX.fullmatch(state["active_device"])
                or not isinstance(state["generation"], str)
                or not _HEX.fullmatch(state["generation"])
                or state["seq"] < 2):
            raise PreconditionBlocked("invalid DEV generation continuity state")
        return state

    def _verify(self, db: sqlite3.Connection, device: str) -> dict[str, object]:
        try:
            rows = db.execute("SELECT v, device, seq, tip FROM ledger_state").fetchall()
            if len(rows) != 1:
                raise PreconditionBlocked("release enrollment missing")
            v, stored, sequence, tip = rows[0]
            # A V2 witness is independently flushed in the same existing HKCU
            # anchor. Never downgrade V2 by accepting the legacy V1 shape.
            remote = self._validated(self.anchor.read())
            current = {"v": v, "device": stored, "seq": sequence, "tip": tip}
            if v != remote["v"]:
                raise PreconditionBlocked("host continuity version mismatch")
            if v == 2:
                current.update(active_device=remote["active_device"],
                               generation=remote["generation"])
            local = self._validated(current)
            if local != remote:
                raise PreconditionBlocked("host continuity mismatch")

            marker = None
            if v == 2:
                markers = db.execute(
                    "SELECT after_seq, new_device, generation, grant, tip "
                    "FROM generation_transition"
                ).fetchall()
                if len(markers) != 1:
                    raise PreconditionBlocked("DEV generation transition missing")
                after_seq, new_device, generation, grant, marker_tip = markers[0]
                if (type(after_seq) is not int or after_seq < 1
                        or after_seq >= sequence
                        or any(type(h) is not str or not _HEX.fullmatch(h)
                               for h in (new_device, generation, grant, marker_tip))
                        or remote["active_device"] != new_device
                        or remote["generation"] != generation
                        or new_device == stored):
                    raise PreconditionBlocked("DEV generation transition invalid")
                marker = (after_seq, new_device, generation, grant, marker_tip)

            active = stored
            previous = _hash("m5daylog:r3:enrolled:" + stored)
            observed = 0
            for seq, cycle, attempt, row_tip in db.execute(
                "SELECT seq, cycle, attempt, tip FROM consumed ORDER BY seq"
            ):
                observed += 1
                if (type(seq) is not int or seq != observed
                        or any(type(h) is not str or not _HEX.fullmatch(h)
                               for h in (cycle, attempt, row_tip))):
                    raise PreconditionBlocked("release chain invalid")
                if marker is not None and seq == marker[0] + 1:
                    previous = _generation_tip(previous, *marker[:4])
                    if previous != marker[4]:
                        raise PreconditionBlocked("DEV generation chain invalid")
                    active = marker[1]
                previous = _tip(previous, seq, active, cycle, attempt)
                if row_tip != previous:
                    raise PreconditionBlocked("release chain invalid")
            if (observed != local["seq"] or previous != local["tip"]
                    or active != device):
                raise PreconditionBlocked("release chain tip or active device invalid")
            return local
        except sqlite3.Error:
            raise PreconditionBlocked("release ledger unreadable") from None

    def read_enrolled_state_locked(self, device_id: str) -> dict[str, object]:
        """Read healthy dual-store continuity; no enrollment or repair."""
        if not getattr(self._serialized_thread, "held", False):
            raise PreconditionBlocked("release OS lock required")
        with closing(self._connection()) as db:
            return self._verify(db, _hash(_uuid(device_id)))

    def append_dev_generation_first_cycle_locked(
        self, *, previous_device_id: str, new_device_id: str,
        generation_id: str, grant_digest: str, prior_state_digest: str,
        release_cycle_id: str, attempt_id: str,
    ) -> None:
        """Single SDB-1.1 generation append AND first consumed cycle, unwired.

        Requires the independently verified *existing* V1 host lineage under
        the shared lock. A grant hash is correlation only, not an issued grant.
        SQLite transition+consumed tuple commit precedes HKCU flush/readback.
        After an uncertain partial write there is NO automatic recovery.
        """
        if not getattr(self._serialized_thread, "held", False):
            raise PreconditionBlocked("release OS lock required")
        old = _hash(_uuid(previous_device_id))
        new = _hash(_uuid(new_device_id))
        generation = _hash(_uuid(generation_id))
        cycle = _hash(_uuid(release_cycle_id))
        attempt = _hash(_uuid(attempt_id))
        if old == new:
            raise PreconditionBlocked("DEV generation must change device identity")
        if (type(grant_digest) is not str or not _HEX.fullmatch(grant_digest)
                or type(prior_state_digest) is not str
                or not _HEX.fullmatch(prior_state_digest)):
            raise PreconditionBlocked("DEV generation lineage invalid")
        try:
            with closing(self._connection()) as db:
                # Verifies exact prior hash-chain AND independently flushed
                # anchor before any BEGIN/CREATE/UPDATE/INSERT.
                prior = self._verify(db, old)
                if prior["v"] != 1 or prior["seq"] < 1:
                    raise PreconditionBlocked("DEV generation requires prior consumed lineage")
                digest = hashlib.sha256(json.dumps(
                    prior, sort_keys=True, separators=(",", ":")
                ).encode("ascii")).hexdigest()
                if digest != prior_state_digest:
                    raise PreconditionBlocked("DEV host lineage receipt mismatch")
                db.execute("BEGIN IMMEDIATE")
                try:
                    if self._verify(db, old) != prior:
                        raise PreconditionBlocked("DEV lineage changed during admission")
                    after_seq = prior["seq"]
                    marker_tip = _generation_tip(
                        prior["tip"], after_seq, new, generation, grant_digest,
                    )
                    seq = after_seq + 1
                    tip = _tip(marker_tip, seq, new, cycle, attempt)
                    db.execute(
                        "CREATE TABLE generation_transition ("
                        "after_seq INTEGER PRIMARY KEY, new_device TEXT NOT NULL, "
                        "generation TEXT UNIQUE NOT NULL, grant TEXT UNIQUE NOT NULL, "
                        "tip TEXT NOT NULL)"
                    )
                    db.execute(
                        "INSERT INTO generation_transition VALUES (?,?,?,?,?)",
                        (after_seq, new, generation, grant_digest, marker_tip),
                    )
                    db.execute("INSERT INTO consumed VALUES (?,?,?,?)",
                               (seq, cycle, attempt, tip))
                    db.execute(
                        "UPDATE ledger_state SET v=2, seq=?, tip=?", (seq, tip)
                    )
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
            updated = {"v": 2, "device": old, "seq": seq, "tip": tip,
                       "active_device": new, "generation": generation}
            self.anchor.write(updated)
            self.verify_dev_generation_locked(
                new_device_id, generation_id, grant_digest,
                release_cycle_id, attempt_id,
            )
        except (sqlite3.Error, OSError):
            raise PreconditionBlocked("DEV generation persistence uncertain") from None

    def verify_dev_generation_locked(
        self, new_device_id: str, generation_id: str, grant_digest: str,
        release_cycle_id: str, attempt_id: str,
    ) -> None:
        """Read back exact transition + first tuple in both stores before send."""
        if not getattr(self._serialized_thread, "held", False):
            raise PreconditionBlocked("release OS lock required")
        new = _hash(_uuid(new_device_id))
        generation = _hash(_uuid(generation_id))
        cycle = _hash(_uuid(release_cycle_id))
        attempt = _hash(_uuid(attempt_id))
        with closing(self._connection()) as db:
            state = self._verify(db, new)
            if state["v"] != 2 or state["generation"] != generation:
                raise PreconditionBlocked("DEV generation not enrolled")
            row = db.execute(
                "SELECT after_seq, grant FROM generation_transition"
            ).fetchone()
            if row is None or row[1] != grant_digest:
                raise PreconditionBlocked("DEV grant not correlated")
            first = db.execute(
                "SELECT cycle, attempt FROM consumed WHERE seq=?", (row[0] + 1,)
            ).fetchone()
            if first != (cycle, attempt):
                raise PreconditionBlocked("DEV first cycle not consumed")

    def enroll(self, device_id: str, *, supervised: bool = False) -> None:
        """Explicit supervised initialization only; NEVER call from monitor/CLI.

        A true flag is an internal boundary, not human approval. An authorized
        external maintenance workflow must verify provenance independently.
        """
        device = _hash(_uuid(device_id))
        if not supervised:
            raise PreconditionBlocked("supervised enrollment required")
        with self.serialized():
            # Apply the same fail-closed H0 check as first-cycle initialization.
            # Orphan SQLite journals must never be normalized by connect().
            self.check_unenrolled_locked()
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

    def check_unenrolled_locked(self) -> None:
        """Recheck the *actual* two-store absence under the global OS lock.

        Absence is necessary but NEVER proof of a previously unenrolled host.
        A separate, independently retained #119 evidence baseline is required.
        """
        if not getattr(self._serialized_thread, "held", False):
            raise PreconditionBlocked("release OS lock required")
        try:
            if (self.database.exists()
                    or _sqlite_recovery_sidecars_present(self.database)
                    or self.anchor.read() is not None):
                raise PreconditionBlocked("existing or incomplete host enrollment")
        except OSError:
            raise PreconditionBlocked("host enrollment state uncertain") from None

    def initialize_first_cycle_locked(self, device_id: str,
                                      release_cycle_id: str,
                                      attempt_id: str) -> None:
        """Source-only: first enrollment AND first consumed tuple in one DB commit.

        MUST be invoked only after a separately supplied, independently proven
        one-use supervised #119 intent has been durably consumed under the
        same serialized() lease. This primitive does not validate that intent,
        grant release authority, or make an unattended CDC sender available.

        SQLite commits first, HKCU anchor second. A crash/uncertainty after
        SQLite but before anchor leaves a mismatch and blocks future actions.
        Do not call legacy enroll() here: its empty ledger is not sufficient.
        """
        if not getattr(self._serialized_thread, "held", False):
            raise PreconditionBlocked("release OS lock required")
        device = _hash(_uuid(device_id))
        cycle = _hash(_uuid(release_cycle_id))
        attempt = _hash(_uuid(attempt_id))
        self.check_unenrolled_locked()
        prior_tip = _hash("m5daylog:r3:enrolled:" + device)
        tip = _tip(prior_tip, 1, device, cycle, attempt)
        state = {"v": 1, "device": device, "seq": 1, "tip": tip}
        try:
            self.database.parent.mkdir(parents=True, exist_ok=True)
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
                db.execute("INSERT INTO consumed VALUES (?,?,?,?)",
                           (1, cycle, attempt, tip))
                db.execute("INSERT INTO ledger_state VALUES (?,?,?,?)",
                           (1, device, 1, tip))
                db.execute("COMMIT")
            self.anchor.write(state)
            with closing(self._connection()) as db:
                if self._verify(db, device) != state:
                    raise PreconditionBlocked("first enrollment verification failed")
        except (sqlite3.Error, OSError):
            raise PreconditionBlocked("first enrollment persistence uncertain") from None

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
                    self._check_dev_cutover_locked(prior)
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
            updated = dict(prior, seq=seq, tip=tip)
            self.anchor.write(updated)
            with closing(self._connection()) as db:
                if self._verify(db, device) != updated:
                    raise PreconditionBlocked("continuity reread failed")
        except (sqlite3.Error, OSError):
            raise PreconditionBlocked("release attempt persistence uncertain") from None

    def verify_reserved_locked(self, device_id: str,
                               release_cycle_id: str, attempt_id: str) -> None:
        """Pre-send proof of the *exact* already-consumed cycle/attempt tuple.

        Only valid inside the same serialized() lease spanning RTC and CDC.
        This operation never inserts, repairs or re-arms the journal.
        """
        if not getattr(self._serialized_thread, "held", False):
            raise PreconditionBlocked("release OS lock required")
        device = _hash(_uuid(device_id))
        cycle = _hash(_uuid(release_cycle_id))
        attempt = _hash(_uuid(attempt_id))
        try:
            with closing(self._connection()) as db:
                state = self._verify(db, device)
                self._check_dev_cutover_locked(state)
                row = db.execute(
                    "SELECT attempt FROM consumed WHERE cycle=?", (cycle,)
                ).fetchone()
                if row != (attempt,):
                    raise PreconditionBlocked("release attempt correlation mismatch")
        except (sqlite3.Error, OSError):
            raise PreconditionBlocked("release attempt verification uncertain") from None

    def check_locked(self, device_id: str) -> None:
        """Verify enrolled host witnesses under serialized() without mutation."""
        if not getattr(self._serialized_thread, "held", False):
            raise PreconditionBlocked("release OS lock required")
        device = _hash(_uuid(device_id))
        try:
            with closing(self._connection()) as db:
                state = self._verify(db, device)
                self._check_dev_cutover_locked(state)
        except (sqlite3.Error, OSError):
            raise PreconditionBlocked("release host continuity uncertain") from None
