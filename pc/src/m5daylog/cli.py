"""Command-line entrypoint for the M5Daylog PoC scaffold.

Scope (#43): ``--help`` / ``--version`` and ``init`` (SQLite bootstrap).
Device sync, VAD, STT, diarization, and day.md generation are explicitly
out of scope and arrive in later Tasks.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .db import expected_tables, init_db, list_tables, resolve_db_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="m5daylog",
        description="M5Daylog PoC Python CLI scaffold (local-first audio lifelog).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    init_parser = subparsers.add_parser(
        "init",
        help="Initialize the local SQLite state database.",
        description="Create state/m5daylog.db with the scaffold schema (idempotent).",
    )
    init_parser.add_argument(
        "--db",
        default=None,
        help="Explicit SQLite file path (default: state/m5daylog.db under CWD or data dir).",
    )
    init_parser.add_argument(
        "--data-dir",
        default=None,
        help="Local data root; database is created at <data-dir>/state/m5daylog.db.",
    )
    return parser


def cmd_init(db_arg: str | None, data_dir_arg: str | None) -> int:
    try:
        db_path = resolve_db_path(explicit_db=db_arg, data_dir=data_dir_arg)
        init_db(db_path)
        tables = list_tables(db_path)
    except OSError as exc:
        print(f"error: init failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"initialized: {db_path}")
    print(f"tables: {','.join(tables)}")
    missing = [name for name in expected_tables() if name not in tables]
    if missing:
        print(f"error: missing tables: {','.join(missing)}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args.db, args.data_dir)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
