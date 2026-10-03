"""Manifest-based incremental sync for the M5Daylog PoC.

The Device mount is treated as read-only. JSON validation is driven by the
canonical repository schemas in contracts/; this module does not redefine
their field set.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO

from .db import init_db

DEVICE_METADATA = Path("M5DAYLOG") / "device.json"
MANIFEST_METADATA = Path("M5DAYLOG") / "manifest.json"
RECORDINGS_ROOT = Path("M5DAYLOG") / "recordings"

_SCHEMA_META_KEYS = {"$schema", "title"}
_SCHEMA_VALIDATION_KEYS = {
    "$defs",
    "type",
    "required",
    "properties",
    "const",
    "format",
    "minLength",
    "$ref",
    "pattern",
    "minimum",
    "enum",
    "items",
}
_SCHEMA_KEYS = _SCHEMA_META_KEYS | _SCHEMA_VALIDATION_KEYS

# Match the RFC3339 validator used by jsonschema[format]==4.26.0 for the
# canonical Draft 2020-12 ``date-time`` format checker. jsonschema uppercases
# the candidate before validation, so lowercase ``t`` / ``z`` remain valid.
_RFC3339_DATETIME = re.compile(
    r"^(\d{4})-(0[1-9]|1[0-2])-(\d{2})T"
    r"(?:[01]\d|2[0-3]):(?:[0-5]\d):(?:[0-5]\d)"
    r"(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$",
    re.ASCII,
)


class ContractValidationError(ValueError):
    """Canonical schema validation failed without echoing protected input."""


class SyncValidationError(ValueError):
    """Cross-document or filesystem sync preflight validation failed."""


class IntegrityError(OSError):
    """Copied bytes do not match the manifest size/hash."""


@dataclass(frozen=True)
class SyncSummary:
    copied: int = 0
    reused: int = 0
    conflicts: int = 0


def default_contracts_dir() -> Path:
    configured = os.environ.get("M5DAYLOG_CONTRACTS_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[3] / "contracts"


def resolve_library_root(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    configured = os.environ.get("M5DAYLOG_LIBRARY_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Documents" / "M5Daylog"


def _contract_error(path: str, keyword: str) -> ContractValidationError:
    return ContractValidationError(f"contract validation failed at {path}: {keyword}")


def _assert_supported_schema(schema: Any, path: str = "$") -> None:
    if not isinstance(schema, dict):
        raise _contract_error(path, "schema")
    unknown = set(schema) - _SCHEMA_KEYS
    if unknown:
        raise _contract_error(path, "unsupported-schema-keyword")
    defs = schema.get("$defs", {})
    if not isinstance(defs, dict):
        raise _contract_error(path, "$defs")
    for name, child in defs.items():
        _assert_supported_schema(child, f"{path}.$defs.{name}")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise _contract_error(path, "properties")
    for name, child in properties.items():
        _assert_supported_schema(child, f"{path}.properties.{name}")
    if "items" in schema:
        _assert_supported_schema(schema["items"], f"{path}.items")


def _resolve_ref(root_schema: dict[str, Any], ref: str) -> dict[str, Any]:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise _contract_error("$", "$ref")
    current: Any = root_schema
    for raw in ref[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or key not in current:
            raise _contract_error("$", "$ref")
        current = current[key]
    if not isinstance(current, dict):
        raise _contract_error("$", "$ref")
    return current


def _is_integer(value: Any) -> bool:
    # JSON Schema integers are mathematical integers, so JSON ``1.0`` is an
    # integer while booleans are not. This mirrors jsonschema's type checker
    # for values produced by json.loads().
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


def _validate_format(value: str, format_name: str, path: str) -> None:
    if format_name == "uuid":
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError):
            raise _contract_error(path, "format") from None
        # jsonschema 4.26.0's Draft 2020-12 UUID checker accepts UUID strings
        # only when the RFC 4122 hyphens occur at these exact positions.
        if not all(value[position] == "-" for position in (8, 13, 18, 23)):
            raise _contract_error(path, "format")
        return
    if format_name == "date-time":
        match = _RFC3339_DATETIME.fullmatch(value.upper())
        if match is None:
            raise _contract_error(path, "format")
        year, month, day = map(int, match.groups())
        if year == 0 or not 1 <= day <= calendar.monthrange(year, month)[1]:
            raise _contract_error(path, "format")
        return
    raise _contract_error(path, "unsupported-format")


def _validate_value(
    value: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str,
) -> None:
    if "$ref" in schema:
        _validate_value(value, _resolve_ref(root_schema, schema["$ref"]), root_schema, path)

    expected_type = schema.get("type")
    if expected_type is not None:
        matches = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": _is_integer(value),
        }.get(expected_type)
        if matches is None:
            raise _contract_error(path, "unsupported-type")
        if not matches:
            raise _contract_error(path, "type")

    if "const" in schema and value != schema["const"]:
        raise _contract_error(path, "const")
    if "enum" in schema and value not in schema["enum"]:
        raise _contract_error(path, "enum")
    if "minLength" in schema and len(value) < schema["minLength"]:
        raise _contract_error(path, "minLength")
    if "minimum" in schema and value < schema["minimum"]:
        raise _contract_error(path, "minimum")
    if "pattern" in schema:
        if not isinstance(value, str) or re.search(schema["pattern"], value) is None:
            raise _contract_error(path, "pattern")
    if "format" in schema:
        if not isinstance(value, str):
            raise _contract_error(path, "format")
        _validate_format(value, schema["format"], path)

    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(name, str) for name in required):
            raise _contract_error(path, "required")
        for name in required:
            if name not in value:
                raise _contract_error(path, "required")
        properties = schema.get("properties", {})
        for name, child_schema in properties.items():
            if name in value:
                _validate_value(value[name], child_schema, root_schema, f"{path}.{name}")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _validate_value(item, schema["items"], root_schema, f"{path}[{index}]")


def validate_contract_document(document: Any, schema_path: str | Path) -> None:
    """Validate a document against the canonical schema, failing closed on unknown keywords."""
    path = Path(schema_path)
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            f"canonical schema unavailable: {type(exc).__name__}"
        ) from None
    _assert_supported_schema(schema)
    _validate_value(document, schema, schema, "$")


def _load_document(path: Path, schema_path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            f"metadata unavailable: {type(exc).__name__}"
        ) from None
    if not isinstance(document, dict):
        raise ContractValidationError("metadata must be a JSON object")
    validate_contract_document(document, schema_path)
    return document


def _contracts_dir(contracts_dir: str | Path | None) -> Path:
    return (
        Path(contracts_dir).expanduser()
        if contracts_dir is not None
        else default_contracts_dir()
    )


def inspect_device(
    mount_root: str | Path,
    *,
    contracts_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Read and validate device metadata without writing to the Device mount."""
    mount = Path(mount_root)
    contracts = _contracts_dir(contracts_dir)
    return _load_document(mount / DEVICE_METADATA, contracts / "device.schema.json")


def _load_manifest(mount: Path, contracts: Path) -> dict[str, Any]:
    return _load_document(
        mount / MANIFEST_METADATA,
        contracts / "manifest.schema.json",
    )


def _resolved_source(mount: Path, filename: str) -> Path:
    if "\\" in filename:
        raise SyncValidationError("manifest filename containment violation")
    posix = PurePosixPath(filename)
    windows = PureWindowsPath(filename)
    raw_parts = filename.split("/")
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or len(posix.parts) < 2
        or posix.parts[0] != "recordings"
        or any(part in ("", ".", "..") for part in raw_parts)
    ):
        raise SyncValidationError("manifest filename containment violation")

    m5root = (mount / "M5DAYLOG").resolve(strict=True)
    recordings_root = (mount / RECORDINGS_ROOT).resolve(strict=True)
    try:
        recordings_root.relative_to(m5root)
    except ValueError:
        raise SyncValidationError("recordings root containment violation") from None

    source = mount.joinpath("M5DAYLOG", *posix.parts)
    try:
        resolved = source.resolve(strict=True)
        resolved.relative_to(recordings_root)
    except (OSError, ValueError):
        raise SyncValidationError("manifest filename containment violation") from None
    if not resolved.is_file():
        raise SyncValidationError("manifest source is not a regular file")
    return resolved


def _preflight(
    mount: Path,
    device: dict[str, Any],
    manifest: dict[str, Any],
) -> list[tuple[dict[str, Any], Path]]:
    if device["deviceId"] != manifest["deviceId"]:
        raise SyncValidationError("device identity mismatch")

    seen: set[str] = set()
    prepared: list[tuple[dict[str, Any], Path]] = []
    for recording in manifest["recordings"]:
        recording_id = recording["recordingId"]
        if recording_id in seen:
            raise SyncValidationError("duplicate recording identity")
        seen.add(recording_id)
        prepared.append((recording, _resolved_source(mount, recording["filename"])))
    return prepared


def _target_path(library_root: Path, recording: dict[str, Any]) -> Path:
    value = recording["startedAt"]
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        started = datetime.fromisoformat(normalized)
    except ValueError:
        raise SyncValidationError("invalid recording timestamp") from None
    if started.tzinfo is None:
        raise SyncValidationError("invalid recording timestamp")
    date = started.date()
    return (
        library_root
        / f"{date.year:04d}"
        / f"{date.month:02d}"
        / f"{date.day:02d}"
        / "raw"
        / f"{recording['recordingId']}.wav"
    )


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _file_matches(path: Path, expected_size: int, expected_sha256: str) -> bool:
    try:
        size, actual_sha = _hash_file(path)
    except OSError:
        return False
    return size == expected_size and actual_sha == expected_sha256


def copy_verified_stream(
    reader: BinaryIO,
    partial_path: str | Path,
    *,
    expected_size: int,
    expected_sha256: str,
    chunk_size: int = 1024 * 1024,
) -> None:
    """Copy from byte zero into a truncating partial and verify before return."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    partial = Path(partial_path)
    partial.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    with partial.open("wb") as output:
        while True:
            chunk = reader.read(chunk_size)
            if not chunk:
                break
            output.write(chunk)
            total += len(chunk)
            digest.update(chunk)
        output.flush()
        os.fsync(output.fileno())
    if total != expected_size or digest.hexdigest() != expected_sha256:
        raise IntegrityError("copied recording failed integrity verification")


def publish_no_clobber(partial_path: str | Path, final_path: str | Path) -> None:
    """Publish a verified partial without replacing an existing final file."""
    partial = Path(partial_path)
    final = Path(final_path)
    if os.name == "nt":
        os.rename(partial, final)
        return
    os.link(partial, final)
    try:
        partial.unlink()
    except BaseException:
        try:
            final.unlink()
        finally:
            raise


def _upsert_device(
    connection: sqlite3.Connection,
    device: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO devices (
            device_id, model, first_seen_at, last_seen_at,
            firmware_version, last_manifest_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            model=excluded.model,
            last_seen_at=excluded.last_seen_at,
            firmware_version=excluded.firmware_version,
            last_manifest_at=excluded.last_manifest_at
        """,
        (
            device["deviceId"],
            device["model"],
            now,
            now,
            device["firmwareVersion"],
            manifest["updatedAt"],
        ),
    )
    connection.commit()


def _existing_recording(connection: sqlite3.Connection, recording_id: str):
    return connection.execute(
        """
        SELECT device_id, source_sha256, size_bytes, local_path, sync_status
        FROM recordings WHERE recording_id=?
        """,
        (recording_id,),
    ).fetchone()


def _write_recording(
    connection: sqlite3.Connection,
    device_id: str,
    recording: dict[str, Any],
    final_path: Path,
    status: str,
) -> None:
    connection.execute(
        """
        INSERT INTO recordings (
            recording_id, device_id, started_at, duration_ms, source_filename,
            source_sha256, size_bytes, local_path, sync_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(recording_id) DO UPDATE SET
            device_id=excluded.device_id,
            started_at=excluded.started_at,
            duration_ms=excluded.duration_ms,
            source_filename=excluded.source_filename,
            source_sha256=excluded.source_sha256,
            size_bytes=excluded.size_bytes,
            local_path=excluded.local_path,
            sync_status=excluded.sync_status
        """,
        (
            recording["recordingId"],
            device_id,
            recording["startedAt"],
            recording["durationMs"],
            recording["filename"],
            recording["sha256"],
            recording["sizeBytes"],
            str(final_path),
            status,
        ),
    )
    connection.commit()


def _mark_conflict(
    connection: sqlite3.Connection,
    device_id: str,
    recording: dict[str, Any],
    final_path: Path,
    existing: Any,
) -> None:
    if existing is None:
        _write_recording(connection, device_id, recording, final_path, "CONFLICT")
        return
    connection.execute(
        "UPDATE recordings SET sync_status='CONFLICT' WHERE recording_id=?",
        (recording["recordingId"],),
    )
    connection.commit()


def _device_root(mount: Path) -> Path:
    try:
        return mount.resolve(strict=True)
    except OSError:
        raise SyncValidationError("unable to resolve Device mount") from None


def _reject_paths_inside_device(device_root: Path, *paths: Path) -> None:
    """Reject paths whose current resolved target is on the Device mount.

    ``Path.resolve(strict=False)`` follows existing symlink/reparse components
    while allowing not-yet-created suffixes. This catches a local library root
    that is safe itself but contains a nested junction/symlink into the Device,
    as well as a pre-existing partial/final symlink that targets Device media.
    """
    for path in paths:
        try:
            candidate = path.resolve(strict=False)
        except OSError:
            raise SyncValidationError("unable to resolve sync target paths") from None
        try:
            candidate.relative_to(device_root)
        except ValueError:
            continue
        raise SyncValidationError("local sync target must be outside Device mount")


def _reject_device_local_targets(
    mount: Path,
    library_root: Path,
    db_path: Path,
) -> Path:
    """Prevent caller-supplied local output paths from writing into Device media."""
    device_root = _device_root(mount)
    _reject_paths_inside_device(device_root, library_root, db_path)
    return device_root


def _reject_derived_recording_targets(
    device_root: Path,
    library_root: Path,
    prepared: list[tuple[dict[str, Any], Path]],
) -> None:
    """Preflight every final/partial path before any local sync state is written."""
    for recording, _source in prepared:
        final_path = _target_path(library_root, recording)
        partial_path = Path(str(final_path) + ".partial")
        _reject_paths_inside_device(device_root, final_path, partial_path)


def sync_device(
    mount_root: str | Path,
    *,
    library_root: str | Path,
    db_path: str | Path,
    contracts_dir: str | Path | None = None,
) -> SyncSummary:
    """Synchronize all validated manifest recordings from a read-only Device."""
    mount = Path(mount_root)
    library = resolve_library_root(library_root)
    contracts = _contracts_dir(contracts_dir)

    device = inspect_device(mount, contracts_dir=contracts)
    manifest = _load_manifest(mount, contracts)
    prepared = _preflight(mount, device, manifest)

    db_target = Path(db_path).expanduser()
    device_root = _reject_device_local_targets(mount, library, db_target)
    _reject_derived_recording_targets(device_root, library, prepared)

    db_file = init_db(db_target)
    connection = sqlite3.connect(str(db_file))
    copied = reused = conflicts = 0
    try:
        _upsert_device(connection, device, manifest)
        for recording, source in prepared:
            final_path = _target_path(library, recording)
            partial_path = Path(str(final_path) + ".partial")
            _reject_paths_inside_device(device_root, final_path, partial_path)

            expected_size = recording["sizeBytes"]
            expected_sha = recording["sha256"]
            existing = _existing_recording(connection, recording["recordingId"])

            if existing is not None and (
                existing[0] != device["deviceId"]
                or existing[1] != expected_sha
                or existing[2] != expected_size
            ):
                _mark_conflict(connection, device["deviceId"], recording, final_path, existing)
                conflicts += 1
                continue

            if final_path.exists():
                if _file_matches(final_path, expected_size, expected_sha):
                    _write_recording(
                        connection, device["deviceId"], recording, final_path, "SYNCED"
                    )
                    if partial_path.exists():
                        partial_path.unlink()
                    reused += 1
                else:
                    _mark_conflict(
                        connection, device["deviceId"], recording, final_path, existing
                    )
                    conflicts += 1
                continue

            final_path.parent.mkdir(parents=True, exist_ok=True)
            # Re-resolve after directory creation so an existing nested reparse
            # point cannot be followed by the subsequent partial-file open.
            _reject_paths_inside_device(device_root, final_path, partial_path)
            with source.open("rb") as reader:
                copy_verified_stream(
                    reader,
                    partial_path,
                    expected_size=expected_size,
                    expected_sha256=expected_sha,
                )

            # Re-check immediately before publication as well. This protects the
            # no-clobber step from a pre-existing or newly surfaced final reparse
            # target without changing the normal stale-partial recovery path.
            _reject_paths_inside_device(device_root, final_path, partial_path)
            try:
                publish_no_clobber(partial_path, final_path)
            except FileExistsError:
                _reject_paths_inside_device(device_root, final_path, partial_path)
                if _file_matches(final_path, expected_size, expected_sha):
                    if partial_path.exists():
                        partial_path.unlink()
                    _write_recording(
                        connection, device["deviceId"], recording, final_path, "SYNCED"
                    )
                    reused += 1
                else:
                    if partial_path.exists():
                        partial_path.unlink()
                    _mark_conflict(
                        connection, device["deviceId"], recording, final_path, existing
                    )
                    conflicts += 1
                continue

            if not _file_matches(final_path, expected_size, expected_sha):
                raise IntegrityError("published recording failed integrity verification")
            _write_recording(
                connection, device["deviceId"], recording, final_path, "SYNCED"
            )
            copied += 1
    finally:
        connection.close()

    return SyncSummary(copied=copied, reused=reused, conflicts=conflicts)
