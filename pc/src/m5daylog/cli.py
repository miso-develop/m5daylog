"""Command-line entrypoint for the M5Daylog PoC."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .db import expected_tables, init_db, list_tables, resolve_db_path
from .vad import VadError, run_vad
from .sync import (
    ContractValidationError,
    IntegrityError,
    SyncValidationError,
    inspect_device,
    resolve_library_root,
    sync_device,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="m5daylog",
        description="M5Daylog PoC local-first audio lifelog CLI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    init_parser = subparsers.add_parser(
        "init",
        help="Initialize the local SQLite state database.",
        description="Create state/m5daylog.db with the scaffold schema (idempotent).",
    )
    init_parser.add_argument("--db", default=None, help="Explicit SQLite file path.")
    init_parser.add_argument(
        "--data-dir",
        default=None,
        help="Local data root; database is <data-dir>/state/m5daylog.db.",
    )

    device_parser = subparsers.add_parser(
        "device",
        help="Validate and identify a mounted M5Daylog Device.",
    )
    device_parser.add_argument("device_root", help="Mounted Device root.")
    device_parser.add_argument(
        "--contracts-dir",
        default=None,
        help="Canonical contracts directory (normally auto-detected).",
    )

    sync_parser = subparsers.add_parser(
        "sync",
        help="Incrementally copy manifest recordings into local raw storage.",
    )
    sync_parser.add_argument("device_root", help="Mounted Device root.")
    sync_parser.add_argument(
        "--library-root",
        default=None,
        help="Local M5Daylog document root (default: ~/Documents/M5Daylog).",
    )
    sync_parser.add_argument("--db", default=None, help="Explicit SQLite file path.")
    sync_parser.add_argument(
        "--data-dir",
        default=None,
        help="Local state root; database is <data-dir>/state/m5daylog.db.",
    )
    sync_parser.add_argument(
        "--contracts-dir",
        default=None,
        help="Canonical contracts directory (normally auto-detected).",
    )
    vad_parser = subparsers.add_parser(
        "vad", help="Run deterministic Silero VAD on one SYNCED recording."
    )
    vad_parser.add_argument("recording_id", help="Recording UUID.")
    vad_parser.add_argument("--library-root", default=None)
    vad_parser.add_argument("--db", default=None)
    vad_parser.add_argument("--data-dir", default=None)
    return parser


def cmd_vad(recording_id: str, library_root: str | None,
            db_arg: str | None, data_dir_arg: str | None) -> int:
    try:
        result = run_vad(
            recording_id,
            db_path=resolve_db_path(explicit_db=db_arg, data_dir=data_dir_arg),
            library_root=library_root,
        )
    except (VadError, OSError, sqlite3.Error, ImportError) as exc:
        print(f"error: vad failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"vad: status={result.status} ranges={result.ranges} sha256={result.sha256}")
    return 0


def cmd_init(db_arg: str | None, data_dir_arg: str | None) -> int:
    # Raw tracebacks can contain host-local paths; diagnostics must not echo them.
    try:
        db_path = resolve_db_path(explicit_db=db_arg, data_dir=data_dir_arg)
        init_db(db_path)
        tables = list_tables(db_path)
    except Exception as exc:
        print(f"error: init failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("initialized: ok")
    print(f"tables: {','.join(tables)}")
    missing = [name for name in expected_tables() if name not in tables]
    if missing:
        print(f"error: missing tables: {','.join(missing)}", file=sys.stderr)
        return 1
    return 0


def cmd_device(device_root: str, contracts_dir: str | None) -> int:
    try:
        device = inspect_device(device_root, contracts_dir=contracts_dir)
    except Exception as exc:
        print(f"error: device validation failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"device: recognized=1 model={device['model']} device_id={device['deviceId']}")
    return 0


def cmd_sync(
    device_root: str,
    library_root: str | None,
    db_arg: str | None,
    data_dir_arg: str | None,
    contracts_dir: str | None,
) -> int:
    try:
        db_path = resolve_db_path(explicit_db=db_arg, data_dir=data_dir_arg)
        library = resolve_library_root(library_root)
        summary = sync_device(
            device_root,
            library_root=library,
            db_path=db_path,
            contracts_dir=contracts_dir,
        )
    except Exception as exc:
        print(f"error: sync failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    print(
        "sync: "
        f"copied={summary.copied} "
        f"reused={summary.reused} "
        f"conflicts={summary.conflicts}"
    )
    return 2 if summary.conflicts else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args.db, args.data_dir)
    if args.command == "device":
        return cmd_device(args.device_root, args.contracts_dir)
    if args.command == "vad":
        return cmd_vad(args.recording_id, args.library_root, args.db, args.data_dir)
    if args.command == "sync":
        return cmd_sync(
            args.device_root,
            args.library_root,
            args.db,
            args.data_dir,
            args.contracts_dir,
        )
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
