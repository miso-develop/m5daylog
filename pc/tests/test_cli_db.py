"""Unit tests for the M5Daylog PoC CLI scaffold.

Uses synthetic identifiers/paths only. No real audio, transcript,
speaker, credential, or device data is used.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from m5daylog import cli, db


class CliHelpTests(unittest.TestCase):
    def test_help_lists_init_command(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as ctx:
                cli.build_parser().parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        # --help exits before main(); verify main() path separately.
        self.assertIn("init", stdout.getvalue())

    def test_main_without_args_prints_help_and_returns_zero(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main([])
        self.assertEqual(code, 0)
        self.assertIn("init", stdout.getvalue())

    def test_main_help_flag_exits_zero(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)


class InitCommandTests(unittest.TestCase):
    def test_init_creates_expected_tables(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "state" / "m5daylog.db"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli.main(["init", "--db", str(target)])
            self.assertEqual(code, 0)
            self.assertTrue(target.exists())
            self.assertEqual(db.list_tables(target), sorted(db.expected_tables()))
            self.assertIn("initialized:", stdout.getvalue())

    def test_init_is_idempotent_and_preserves_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "m5daylog.db"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["init", "--db", str(target)]), 0)
            connection = sqlite3.connect(str(target))
            try:
                connection.execute(
                    "INSERT INTO devices (device_id, model) VALUES (?, ?)",
                    ("synthetic-device-001", "synthetic-model"),
                )
                connection.commit()
            finally:
                connection.close()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["init", "--db", str(target)]), 0)
            connection = sqlite3.connect(str(target))
            try:
                rows = connection.execute("SELECT device_id FROM devices").fetchall()
            finally:
                connection.close()
            self.assertEqual(rows, [("synthetic-device-001",)])

    def test_init_with_data_dir_layout(self) -> None:
        with TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli.main(["init", "--data-dir", tmp])
            expected = Path(tmp) / "state" / "m5daylog.db"
            self.assertEqual(code, 0)
            self.assertTrue(expected.exists())


class DbModuleTests(unittest.TestCase):
    def test_resolve_prefers_explicit_db(self) -> None:
        resolved = db.resolve_db_path(explicit_db="custom.db", data_dir="data-dir")
        self.assertEqual(resolved, Path("custom.db"))

    def test_resolve_data_dir_layout(self) -> None:
        resolved = db.resolve_db_path(data_dir="synthetic-root")
        self.assertEqual(resolved, Path("synthetic-root") / "state" / "m5daylog.db")

    def test_init_db_sets_schema_version(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "m5daylog.db"
            db.init_db(target)
            connection = sqlite3.connect(str(target))
            try:
                version = connection.execute(
                    "SELECT value FROM schema_info WHERE key='schema_version'"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(version, (str(db.SCHEMA_VERSION),))


if __name__ == "__main__":
    unittest.main()
