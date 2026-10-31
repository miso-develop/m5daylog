"""#135 R5 negative release gate: no host/Device mutation or physical IO."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from m5daylog import cli


_DEVICE = "00000000-0000-4000-8000-000000000001"
_CYCLE = "00000000-0000-4000-8000-000000000002"


class LegacyCliR5ExclusionTests(unittest.TestCase):
    def test_manual_release_blocks_before_any_legacy_io_even_with_apparent_status(self):
        # This gate is deliberately independent of a claimed status/receipt.
        # The true R5 issuer, Device attestation and common one-send lock are
        # not integrated. Not even a syntactically valid pair may enable IO.
        candidate_statuses = (
            ("legacy-cycle-only", {"releaseCycleId": _CYCLE}),
            ("missing-both", {}),
            ("one-sided-origin", {"releaseCycleOrigin": "NORMAL_WAKE"}),
            ("invalid-uuid", {
                "releaseCycleId": "not-a-uuid", "releaseCycleOrigin": "NORMAL_WAKE",
            }),
            ("invalid-origin", {
                "releaseCycleId": _CYCLE, "releaseCycleOrigin": "normal_wake",
            }),
            ("unknown-origin", {
                "releaseCycleId": _CYCLE, "releaseCycleOrigin": "UNKNOWN",
            }),
            ("supervised-without-host-intent", {
                "releaseCycleId": _CYCLE,
                "releaseCycleOrigin": "SUPERVISED_FIRST_USE",
            }),
            ("normal-without-consumed-journal", {
                "releaseCycleId": _CYCLE, "releaseCycleOrigin": "NORMAL_WAKE",
            }),
            ("matched-looking-id-is-not-proof", {
                "deviceId": _DEVICE,
                "releaseCycleId": _CYCLE,
                "releaseCycleOrigin": "NORMAL_WAKE",
                "rtcCorrectionPending": False,
            }),
        )
        for label, purported_status in candidate_statuses:
            with (
                self.subTest(case=label),
                patch.object(cli.sys, "platform", "win32"),
                patch.object(cli, "resolve_db_path") as resolve_db,
                patch.object(cli, "resolve_library_root") as resolve_library,
                patch.object(cli, "inspect_device") as inspect,
                patch.object(cli, "sync_device") as sync,
                patch("m5daylog.windows_release.NativeWin32DeviceApi") as native,
                patch("m5daylog.windows_release.WindowsHostReleasePort.set_time") as set_time,
                patch("m5daylog.windows_release.WindowsHostReleasePort.release_storage") as cdc_release,
                patch("m5daylog.release.SyncReleaseCoordinator.run") as sender,
            ):
                native.return_value.read_status.return_value = purported_status
                stderr, stdout = io.StringIO(), io.StringIO()
                with redirect_stderr(stderr), redirect_stdout(stdout):
                    code = cli.main([
                        "sync-release", "Z:\\", "--cdc-port", "COM9",
                        "--library-root", "local", "--db", "local.db",
                        "--contracts-dir", "contracts",
                    ])
                self.assertEqual(code, 3)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(
                    stderr.getvalue().strip(),
                    "release: status=RELEASE_BLOCKED "
                    "reason=R5_ADMISSION_NOT_QUALIFIED",
                )
                for mock in (resolve_db, resolve_library, inspect, sync,
                             native, set_time, cdc_release, sender):
                    mock.assert_not_called()

    def test_manual_release_does_not_allow_force_override(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            cli.main(["sync-release", "Z:\\", "--cdc-port", "COM9", "--force"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments: --force", stderr.getvalue())

    def test_manual_release_remains_windows_only(self):
        stderr = io.StringIO()
        with patch.object(cli.sys, "platform", "linux"), redirect_stderr(stderr):
            code = cli.main(["sync-release", "Z:\\", "--cdc-port", "COM9"])
        self.assertEqual(code, 1)
        self.assertEqual(stderr.getvalue().strip(),
                         "error: sync-release requires Windows")

    def test_read_only_explicit_sync_command_is_unchanged(self):
        stdout = io.StringIO()
        with (
            patch.object(cli, "resolve_db_path", return_value="db") as db,
            patch.object(cli, "resolve_library_root", return_value="library") as lib,
            patch.object(cli, "sync_device", return_value=SimpleNamespace(
                copied=1, reused=2, conflicts=0)) as sync,
            patch("m5daylog.release.SyncReleaseCoordinator.run") as sender,
            redirect_stdout(stdout),
        ):
            code = cli.main([
                "sync", "Z:\\", "--library-root", "raw", "--db", "dbpath",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(),
                         "sync: copied=1 reused=2 conflicts=0")
        db.assert_called_once_with(explicit_db="dbpath", data_dir=None)
        lib.assert_called_once_with("raw")
        sync.assert_called_once_with(
            "Z:\\", library_root="library", db_path="db", contracts_dir=None,
        )
        sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
