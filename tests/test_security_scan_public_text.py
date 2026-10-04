from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "security_scan.py"
SPEC = importlib.util.spec_from_file_location("security_scan_public_text", MODULE_PATH)
assert SPEC and SPEC.loader
security_scan = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = security_scan
SPEC.loader.exec_module(security_scan)


def synthetic_windows_home(marker: str = "synthetic-user-94") -> str:
    return "C:" + "\\" + "Users" + "\\" + marker + "\\" + "workspace" + "\\" + "notes.txt"


def synthetic_private_repo(marker: str = "synthetic-project-94") -> str:
    return marker + "-" + "private"


class PublicTextValidationTests(unittest.TestCase):
    def run_main(
        self,
        argv: list[str],
        *,
        stdin_text: str = "",
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = security_scan.main(argv, stdin=io.StringIO(stdin_text))
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_candidate_not_echoed(self, candidate: str, stdout: str, stderr: str) -> None:
        self.assertNotIn(candidate, stdout)
        self.assertNotIn(candidate, stderr)

    def test_unsafe_human_gate_instruction_from_stdin_is_rejected_without_echo(self) -> None:
        marker = "synthetic-user-94"
        candidate = "Inspect " + synthetic_windows_home(marker) + " before continuing."
        argv = ["--public-text-stdin", "--label", "human-gate/instruction"]
        self.assertNotIn(candidate, argv)

        code, stdout, stderr = self.run_main(argv, stdin_text=candidate)

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertIn("human-gate/instruction:1", stderr)
        self.assertIn("[machine-path-windows]", stderr)
        self.assertIn("FAILED: 1 finding(s)", stderr)
        self.assertNotIn(marker, stderr)
        self.assert_candidate_not_echoed(candidate, stdout, stderr)

    def test_safe_placeholder_human_gate_instruction_from_stdin_passes(self) -> None:
        candidate = "Inspect <repo-root>/artifacts and store evidence under <evidence-dir>."
        argv = ["--public-text-stdin", "--label", "human-gate/instruction"]
        self.assertNotIn(candidate, argv)

        code, stdout, stderr = self.run_main(argv, stdin_text=candidate)

        self.assertEqual(0, code)
        self.assertIn("OK", stdout)
        self.assertEqual("", stderr)
        self.assert_candidate_not_echoed(candidate, stdout, stderr)

    def test_unsafe_durable_evidence_file_is_rejected_without_echo(self) -> None:
        marker = "synthetic-project-94"
        candidate = json.dumps(
            {
                "issue": "#94",
                "revision": "<revision>",
                "observation": "source=" + synthetic_private_repo(marker),
                "disposition": "PASS",
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            candidate_path = Path(temp_dir) / "candidate.txt"
            candidate_path.write_text(candidate, encoding="utf-8")
            argv = [
                "--public-text-file",
                str(candidate_path),
                "--label",
                "human-gate/evidence",
            ]
            self.assertNotIn(candidate, argv)

            code, stdout, stderr = self.run_main(argv)

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertIn("human-gate/evidence:1", stderr)
        self.assertIn("[private-repository-identifier]", stderr)
        self.assertNotIn(marker, stderr)
        self.assert_candidate_not_echoed(candidate, stdout, stderr)

    def test_structured_sanitized_durable_evidence_file_passes(self) -> None:
        candidate = json.dumps(
            {
                "issue": "#94",
                "pr": "#<public-pr>",
                "revision": "<revision>",
                "acceptance": "HG-01",
                "command": {"exit_code": 0, "result": "PASS"},
                "observation": "Required physical state was observed.",
                "disposition": "PASS",
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            candidate_path = Path(temp_dir) / "candidate.txt"
            candidate_path.write_text(candidate, encoding="utf-8")
            argv = [
                "--public-text-file",
                str(candidate_path),
                "--label",
                "human-gate/evidence",
            ]
            self.assertNotIn(candidate, argv)

            code, stdout, stderr = self.run_main(argv)

        self.assertEqual(0, code)
        self.assertIn("OK", stdout)
        self.assertEqual("", stderr)
        self.assert_candidate_not_echoed(candidate, stdout, stderr)

    def test_scanner_error_fails_closed_with_distinct_exit_code_without_echo(self) -> None:
        marker = "synthetic-user-94"
        candidate = "Inspect " + synthetic_windows_home(marker)
        argv = ["--public-text-stdin", "--label", "human-gate/instruction"]
        with mock.patch.object(
            security_scan,
            "scan_text",
            side_effect=security_scan.ScanError("synthetic scanner failure"),
        ):
            code, stdout, stderr = self.run_main(argv, stdin_text=candidate)

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertIn("[security:scan] ERROR:", stderr)
        self.assertNotIn(marker, stderr)
        self.assert_candidate_not_echoed(candidate, stdout, stderr)

    def test_unreadable_public_text_file_fails_closed_without_echoing_file_path(self) -> None:
        marker = "synthetic-path-marker-94"
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / marker / "missing.txt"
            argv = [
                "--public-text-file",
                str(missing),
                "--label",
                "human-gate/evidence",
            ]
            code, stdout, stderr = self.run_main(argv)

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertIn("[security:scan] ERROR:", stderr)
        self.assertNotIn(marker, stderr)
        self.assertNotIn(str(missing), stderr)

    def test_invalid_public_text_label_fails_closed_without_echoing_label(self) -> None:
        marker = "synthetic-label-value-94"
        unsafe_label = "human gate " + marker
        argv = ["--public-text-stdin", "--label", unsafe_label]

        code, stdout, stderr = self.run_main(argv, stdin_text="safe candidate")

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertIn("[security:scan] ERROR:", stderr)
        self.assertNotIn(marker, stderr)
        self.assertNotIn(unsafe_label, stderr)

    def test_default_mode_still_uses_tracked_repository_scan(self) -> None:
        root = Path("synthetic-root")
        with mock.patch.object(
            security_scan,
            "scan_repository",
            return_value=([], []),
        ) as scan_repository:
            code, stdout, stderr = self.run_main(["--root", str(root)])

        self.assertEqual(0, code)
        self.assertIn("OK", stdout)
        self.assertEqual("", stderr)
        scan_repository.assert_called_once_with(root.resolve())


if __name__ == "__main__":
    unittest.main()
