"""Regression checks for non-echoing PC CLI output (Task #98).

All path markers are synthetic; no local user path is embedded in this file.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from m5daylog import cli, db


MARKER = "synthetic" + "-path-marker"


def run_cli(*args: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(list(args))
    return code, stdout.getvalue(), stderr.getvalue()


class NonEchoingCliTests(unittest.TestCase):
    def assert_no_path(self, stdout: str, stderr: str, *paths: Path | str) -> None:
        for stream in (stdout, stderr):
            self.assertNotIn(MARKER, stream)
            for path in paths:
                self.assertNotIn(str(path), stream)

    def test_init_explicit_db_success_preserves_schema_and_does_not_echo_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / MARKER / "state" / "m5daylog.db"
            unused_data_dir = root / MARKER / "unused"
            code, stdout, stderr = run_cli(
                "init", "--db", str(target), "--data-dir", str(unused_data_dir)
            )
            self.assertEqual(code, 0)
            self.assertTrue(target.is_file())
            self.assertFalse(unused_data_dir.exists())
            self.assertEqual(db.list_tables(target), sorted(db.expected_tables()))
            self.assertIn("initialized: ok", stdout)
            self.assertIn("tables:", stdout)
            for table in db.expected_tables():
                self.assertIn(table, stdout)
            self.assertEqual(stderr, "")
            self.assert_no_path(stdout, stderr, target, unused_data_dir)

    def test_init_data_dir_success_does_not_echo_resolved_database(self) -> None:
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / MARKER / "data"
            target = data_dir / "state" / "m5daylog.db"
            code, stdout, stderr = run_cli("init", "--data-dir", str(data_dir))
            self.assertEqual(code, 0)
            self.assertTrue(target.is_file())
            self.assertIn("initialized: ok", stdout)
            self.assertIn("tables:", stdout)
            self.assertEqual(stderr, "")
            self.assert_no_path(stdout, stderr, target, data_dir)

    def test_init_failure_does_not_echo_candidate_or_exception_text(self) -> None:
        candidate = Path("synthetic-root") / MARKER / "m5daylog.db"
        for failure in (OSError, sqlite3.OperationalError):
            with self.subTest(failure=failure.__name__):
                with patch.object(
                    cli, "init_db",
                    side_effect=failure(f"cannot open {candidate}; {MARKER}"),
                ):
                    code, stdout, stderr = run_cli("init", "--db", str(candidate))
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    stderr, f"error: init failed: {failure.__name__}\n"
                )
                self.assert_no_path(stdout, stderr, candidate)

    def test_init_missing_table_keeps_failure_exit_and_safe_status(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / MARKER / "m5daylog.db"
            with patch.object(cli, "list_tables", return_value=[]):
                code, stdout, stderr = run_cli("init", "--db", str(target))
            self.assertEqual(code, 1)
            self.assertIn("initialized: ok", stdout)
            self.assertIn("tables:", stdout)
            self.assertIn("error: missing tables:", stderr)
            self.assert_no_path(stdout, stderr, target)

    def test_device_success_and_failure_do_not_echo_source_paths(self) -> None:
        source = Path("synthetic-mount") / MARKER
        contracts = Path("synthetic-contracts") / MARKER
        synthetic_device = {
            "model": "SYNTHETIC-MODEL",
            "deviceId": "11111111-1111-4111-8111-111111111111",
        }
        with patch.object(cli, "inspect_device", return_value=synthetic_device):
            code, stdout, stderr = run_cli(
                "device", str(source), "--contracts-dir", str(contracts)
            )
        self.assertEqual(code, 0)
        self.assertIn("device: recognized=1", stdout)
        self.assertEqual(stderr, "")
        self.assert_no_path(stdout, stderr, source, contracts)

        with patch.object(
            cli, "inspect_device",
            side_effect=OSError(f"invalid source {source}: {MARKER}"),
        ):
            code, stdout, stderr = run_cli("device", str(source))
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "error: device validation failed: OSError\n")
        self.assert_no_path(stdout, stderr, source)

    def test_sync_counts_and_conflict_exit_do_not_echo_resolved_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / MARKER
            source = root / "device"
            library = root / "library"
            database = root / "state" / "m5daylog.db"
            for conflicts, expected_exit in ((0, 0), (1, 2)):
                with self.subTest(conflicts=conflicts):
                    with patch.object(
                        cli, "sync_device",
                        return_value=SimpleNamespace(
                            copied=2, reused=1, conflicts=conflicts
                        ),
                    ):
                        code, stdout, stderr = run_cli(
                            "sync", str(source),
                            "--library-root", str(library),
                            "--db", str(database),
                        )
                    self.assertEqual(code, expected_exit)
                    self.assertEqual(
                        stdout,
                        f"sync: copied=2 reused=1 conflicts={conflicts}\n",
                    )
                    self.assertEqual(stderr, "")
                    self.assert_no_path(
                        stdout, stderr, source, library, database
                    )

    def test_sync_failure_does_not_echo_exception_text_or_configured_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / MARKER
            source = root / "device"
            library = root / "library"
            data_dir = root / "data"
            database = data_dir / "state" / "m5daylog.db"
            with patch.object(
                cli, "sync_device",
                side_effect=OSError(f"failed on {database}: {MARKER}"),
            ):
                code, stdout, stderr = run_cli(
                    "sync", str(source),
                    "--library-root", str(library),
                    "--data-dir", str(data_dir),
                )
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "error: sync failed: OSError\n")
            self.assert_no_path(
                stdout, stderr, source, library, data_dir, database
            )


if __name__ == "__main__":
    unittest.main()
