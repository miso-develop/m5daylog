"""#121: opt-in, post-clear maintenance release; never a normal sync fallback.

The CLI is deliberately preflight-only. The execution coordinator is a
mockable, *non-CLI* Human Gate seam; no native execution/approval adapter is
provided by this task. In particular, a JSON receipt cannot attest firmware.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from .release import (
    EjectTarget, GateState, HostReleasePort, UsbSessionIoGate,
)
from .sync import inspect_device

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_EXPECTED_DELETIONS = {
    "wav": 76, "manifests": 2, "events": 1, "recording_directories": 10,
}
_ISSUE = 119


class MaintenanceBlocked(Exception):
    """Fail-closed diagnostic; no protected filesystem data in message."""


@dataclass(frozen=True)
class Preflight:
    receipt_sha256: str
    device_id: str
    verified_backups: int
    verified_protected: int


@dataclass(frozen=True)
class GatePermit:
    """Trusted Human Gate adapter output; NEVER deserialize from a receipt/CLI.

    The external adapter must independently attest the RUNNING D-031 handler,
    current target, and a distinct operator authorization for this attempt.
    This class alone is NOT proof of those facts.
    """
    issue: int
    receipt_sha256: str
    target: EjectTarget
    attempt_id: str
    running_handler_qualified: bool


@dataclass(frozen=True)
class MaintenanceOutcome:
    status: str
    reason: str

    @property
    def success(self) -> bool:
        return self.status == "MAINTENANCE_RELEASE_ONLY"


def _hex(value: object) -> bool:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _safe_relative(raw: object) -> Path:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise MaintenanceBlocked("INVALID_EVIDENCE_PATH")
    path = PurePosixPath(raw)
    if (path.is_absolute() or len(path.parts) == 0 or
            any(p in ("", ".", "..") for p in raw.split("/")) or
            any(":" in p for p in path.parts)):
        raise MaintenanceBlocked("INVALID_EVIDENCE_PATH")
    return Path(*path.parts)


def _regular(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError:
        raise MaintenanceBlocked("EVIDENCE_FILE_MISSING") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise MaintenanceBlocked("EVIDENCE_FILE_UNSAFE")
    return info


def _hash_file(path: Path) -> tuple[str, int]:
    before = _regular(path)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        after = _regular(path)
    except OSError:
        raise MaintenanceBlocked("EVIDENCE_READ_FAILED") from None
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino):
        raise MaintenanceBlocked("EVIDENCE_CHANGED_DURING_READ")
    return digest.hexdigest(), after.st_size


def _check_index(root: Path, records: object, count: int) -> set[str]:
    if not isinstance(records, list) or len(records) != count:
        raise MaintenanceBlocked("EVIDENCE_COUNT_MISMATCH")
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            raise MaintenanceBlocked("INVALID_EVIDENCE_RECORD")
        rel = _safe_relative(record["path"])
        name = rel.as_posix()
        if name in seen or not _hex(record["sha256"]):
            raise MaintenanceBlocked("INVALID_EVIDENCE_RECORD")
        size = record["size"]
        if type(size) is not int or size < 0:
            raise MaintenanceBlocked("INVALID_EVIDENCE_RECORD")
        candidate = root / rel
        # Do not traverse junctions/symlinks in intermediate path segments.
        node = root
        for part in rel.parts[:-1]:
            node = node / part
            try:
                mode = node.lstat().st_mode
            except OSError:
                raise MaintenanceBlocked("EVIDENCE_DIRECTORY_MISSING") from None
            if not stat.S_ISDIR(mode):
                raise MaintenanceBlocked("EVIDENCE_DIRECTORY_UNSAFE")
        actual_hash, actual_size = _hash_file(candidate)
        if actual_size != size or not hmac.compare_digest(actual_hash, record["sha256"]):
            raise MaintenanceBlocked("EVIDENCE_HASH_MISMATCH")
        seen.add(name)
    return seen


def _inventory(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    dirs: set[str] = set()
    def walk(folder: Path) -> None:
        try:
            entries = list(folder.iterdir())
        except OSError:
            raise MaintenanceBlocked("MEDIA_INVENTORY_FAILED") from None
        for path in entries:
            try:
                mode = path.lstat().st_mode
                name = path.relative_to(root).as_posix()
            except (OSError, ValueError):
                raise MaintenanceBlocked("MEDIA_INVENTORY_FAILED") from None
            if stat.S_ISDIR(mode):
                dirs.add(name)
                walk(path)
            elif stat.S_ISREG(mode):
                files.add(name)
            else:
                raise MaintenanceBlocked("MEDIA_UNSAFE_ENTRY")
    walk(root)
    return files, dirs


def verify_post_clear(
    *, mount_root: str | Path, backup_root: str | Path,
    receipt_path: str | Path, pinned_receipt_sha256: str,
    contracts_dir: str | Path | None = None,
) -> Preflight:
    """Host read-only check. A SHA pin must come from independently retained approval.

    Does NOT qualify the running firmware, USB owner, or a physical RELEASE.
    """
    if not _hex(pinned_receipt_sha256):
        raise MaintenanceBlocked("RECEIPT_PIN_REQUIRED")
    mount = Path(mount_root)
    backup = Path(backup_root)
    receipt_file = Path(receipt_path)
    try:
        if not mount.is_dir() or not backup.is_dir():
            raise MaintenanceBlocked("BASELINE_ROOT_MISSING")
        if mount.resolve() == backup.resolve():
            raise MaintenanceBlocked("BACKUP_NOT_ISOLATED")
        raw_hash, raw_size = _hash_file(receipt_file)
        if raw_size > 1024 * 1024 or not hmac.compare_digest(
            raw_hash, pinned_receipt_sha256
        ):
            raise MaintenanceBlocked("RECEIPT_PIN_MISMATCH")
        document = json.loads(receipt_file.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or set(document) != {
            "schema_version", "issue", "device_id", "clear_guard_consumed",
            "deleted", "backups", "protected",
        }:
            raise MaintenanceBlocked("RECEIPT_INVALID")
        if (type(document["schema_version"]) is not int or
                document["schema_version"] != 1 or
                type(document["issue"]) is not int or document["issue"] != _ISSUE or
                document["clear_guard_consumed"] is not True or
                document["deleted"] != _EXPECTED_DELETIONS):
            raise MaintenanceBlocked("CLEAR_NOT_PROVEN")
        expected_id = document["device_id"]
        if not isinstance(expected_id, str) or not expected_id:
            raise MaintenanceBlocked("DEVICE_ID_INVALID")
        device = inspect_device(mount, contracts_dir=contracts_dir)
        if device["deviceId"] != expected_id:
            raise MaintenanceBlocked("DEVICE_ID_CHANGED")
        _check_index(backup, document["backups"], 117)
        protected_files = _check_index(mount, document["protected"], 38)
        if "M5DAYLOG/device.json" not in protected_files:
            raise MaintenanceBlocked("PROTECTED_DEVICE_ID_MISSING")
        files, dirs = _inventory(mount)
        if files != protected_files:
            raise MaintenanceBlocked("MEDIA_CONTENT_CHANGED")
        if "M5DAYLOG/recordings" not in dirs:
            raise MaintenanceBlocked("RECORDINGS_DIRECTORY_MISSING")
        if any(p.startswith("M5DAYLOG/recordings/") for p in files | dirs):
            raise MaintenanceBlocked("NEW_CAPTURE_OR_RECOVERY")
        if ("M5DAYLOG/manifest.json" in files or
                "M5DAYLOG/events.jsonl" in files):
            raise MaintenanceBlocked("HISTORY_REAPPEARED")
        expected_dirs = {"M5DAYLOG", "M5DAYLOG/recordings",
                         "M5DAYLOG/acks", "M5DAYLOG/quarantine"}
        for name in protected_files:
            p = PurePosixPath(name)
            expected_dirs.update(str(parent) for parent in p.parents if str(parent) != ".")
        if not dirs.issubset(expected_dirs):
            raise MaintenanceBlocked("UNEXPECTED_MEDIA_DIRECTORY")
        return Preflight(raw_hash, expected_id, 117, 38)
    except MaintenanceBlocked:
        raise
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        raise MaintenanceBlocked("PREFLIGHT_UNVERIFIED") from None


class OneShotGuard:
    """Local *host-only* append-before-transmit record, never on Device storage.

    An external Human Gate must qualify Windows durability/volume/one-shot
    semantics before authorizing native calls. Existing path always blocks.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def consume(self, permit: GatePermit) -> None:
        content = json.dumps({
            "issue": permit.issue, "receipt_sha256": permit.receipt_sha256,
            "attempt_id": permit.attempt_id, "consumed": True,
        }, sort_keys=True).encode("ascii") + b"\n"
        fd = None
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise MaintenanceBlocked("ATTEMPT_ALREADY_CONSUMED") from None
        except OSError:
            # Treat any uncertain create/write/fsync as consumed. Never unlink.
            raise MaintenanceBlocked("GUARD_DURABILITY_UNVERIFIED") from None
        finally:
            if fd is not None:
                os.close(fd)


class MaintenanceReleaseCoordinator:
    """Human-Gate-only injection seam; not reachable from public CLI."""

    def __init__(self, port: HostReleasePort, *, timeout_s: float = 20,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.port = port
        self.timeout_s = timeout_s
        self.clock = clock
        self.sleep = sleep

    def run(self, *, mount_root: str, cdc_port: str, gate: UsbSessionIoGate,
            fresh_preflight: Callable[[], Preflight],
            trusted_human_gate_qualifier: Callable[[Preflight, EjectTarget], GatePermit],
            guard: OneShotGuard) -> MaintenanceOutcome:
        """Caller supplies separately authorized runtime/attempt proof.

        Not a substitute for independent Windows physical Human Gate.
        All injected host reads/approvals run before normal I/O admission seal.
        """
        try:
            with gate.ordinary("msc"):
                initial = fresh_preflight()
                target = self.port.resolve_target(mount_root, initial.device_id)
                self.port.revalidate_target(target, cdc_port)
                if not self.port.volume_available(target):
                    raise MaintenanceBlocked("VOLUME_NOT_AVAILABLE")
                final = fresh_preflight()
                if final != initial:
                    raise MaintenanceBlocked("PREFLIGHT_DRIFT")
                self.port.revalidate_target(target, cdc_port)
                permit = trusted_human_gate_qualifier(final, target)
                if (not isinstance(permit, GatePermit) or permit.issue != _ISSUE or
                        permit.receipt_sha256 != final.receipt_sha256 or
                        permit.target != target or not permit.running_handler_qualified):
                    raise MaintenanceBlocked("BLOCKED_PROVENANCE_OR_APPROVAL")
                if (not isinstance(permit.attempt_id, str) or
                        str(uuid.UUID(permit.attempt_id)) != permit.attempt_id):
                    raise MaintenanceBlocked("INVALID_ATTEMPT_ID")
            gate.seal(timeout_s=self.timeout_s, clock=self.clock)
            with gate.release_control():
                with self.port.quiesce(target):
                    gate.host_quiesced()
                    # Consume before the first byte can be transmitted, even
                    # if a later error means the request was never sent.
                    guard.consume(permit)
                    self.port.release_storage(cdc_port, permit.attempt_id, target)
                    gate.accepted()
                    deadline = self.clock() + self.timeout_s
                    while True:
                        if self.port.volume_disappeared(target):
                            gate.released()
                            return MaintenanceOutcome(
                                "MAINTENANCE_RELEASE_ONLY", "RELEASED")
                        if self.clock() >= deadline:
                            raise MaintenanceBlocked("POST_ACCEPT_TIMEOUT")
                        self.sleep(min(0.2, max(0, deadline - self.clock())))
        except Exception as error:
            if isinstance(error, MaintenanceBlocked):
                reason = str(error)
            else:
                reason = "INDETERMINATE_OR_HOST_FAILURE"
            return MaintenanceOutcome("BLOCKED", reason)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m m5daylog.maintenance_release",
        description="READ-ONLY post-clear maintenance preflight. Never sends RELEASE_STORAGE.",
    )
    parser.add_argument("--device-root", required=True)
    parser.add_argument("--backup-root", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--receipt-sha256", required=True)
    parser.add_argument("--cdc-port", required=True)
    parser.add_argument("--contracts-dir")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print("maintenance: BLOCKED reason=WINDOWS_REQUIRED")
        return 3
    try:
        from .windows_release import NativeWin32DeviceApi, WindowsHostReleasePort
        proof = verify_post_clear(
            mount_root=args.device_root, backup_root=args.backup_root,
            receipt_path=args.receipt, pinned_receipt_sha256=args.receipt_sha256,
            contracts_dir=args.contracts_dir)
        port = WindowsHostReleasePort(NativeWin32DeviceApi(),
                                      contracts_dir=args.contracts_dir)
        target = port.resolve_target(args.device_root, proof.device_id)
        port.revalidate_target(target, args.cdc_port)
        if not port.volume_available(target):
            raise MaintenanceBlocked("VOLUME_NOT_AVAILABLE")
        print("maintenance: PREFLIGHT_ONLY reason=RUNNING_FIRMWARE_UNATTESTED "
              "release_sent=0 volume_mutated=0")
        return 0
    except Exception as error:
        reason = str(error) if isinstance(error, MaintenanceBlocked) else "PREFLIGHT_UNVERIFIED"
        print(f"maintenance: BLOCKED reason={reason} release_sent=0")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
