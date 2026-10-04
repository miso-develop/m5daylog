from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "security_scan.py"
SPEC = importlib.util.spec_from_file_location("security_scan_review_regressions", MODULE_PATH)
assert SPEC and SPEC.loader
security_scan = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = security_scan
SPEC.loader.exec_module(security_scan)


def synthetic_private_repo(marker: str = "synthetic-project-42") -> str:
    return marker + "-" + "private"


class SecurityScanReviewRegressionTests(unittest.TestCase):
    def assert_repository_url_detected_without_echo(self, suffix: str) -> None:
        marker = "synthetic-project-42"
        candidate = (
            "https://github.com/"
            + "public-owner/"
            + synthetic_private_repo(marker)
            + suffix
        )
        findings = security_scan.scan_text("candidate-public-text", candidate)
        matching = [
            finding
            for finding in findings
            if finding.rule == "private-repository-identifier"
        ]
        self.assertEqual(1, len(matching), findings)
        rendered = security_scan.render_finding(matching[0])
        self.assertNotIn(marker, matching[0].message)
        self.assertNotIn(marker, rendered)
        self.assertNotIn(candidate, rendered)

    def assert_repository_identifier_with_terminal_period_detected_without_echo(
        self, prefix: str
    ) -> None:
        marker = "synthetic-project-42"
        candidate = prefix + synthetic_private_repo(marker) + "."
        findings = security_scan.scan_text("candidate-public-text", candidate)
        matching = [
            finding
            for finding in findings
            if finding.rule == "private-repository-identifier"
        ]
        self.assertEqual(1, len(matching), findings)
        rendered = security_scan.render_finding(matching[0])
        self.assertNotIn(marker, matching[0].message)
        self.assertNotIn(marker, rendered)
        self.assertNotIn(candidate, rendered)

    def test_detects_private_repository_url_with_trailing_path(self) -> None:
        self.assert_repository_url_detected_without_echo("/issues/1")

    def test_detects_private_repository_url_with_clone_suffix(self) -> None:
        self.assert_repository_url_detected_without_echo(".git")

    def test_detects_private_repository_url_before_sentence_period(self) -> None:
        self.assert_repository_url_detected_without_echo(".")

    def test_detects_private_repository_url_before_ordinary_punctuation(self) -> None:
        for suffix in ("!", ":"):
            with self.subTest(suffix=suffix):
                self.assert_repository_url_detected_without_echo(suffix)

    def test_detects_bare_private_repository_before_sentence_period(self) -> None:
        self.assert_repository_identifier_with_terminal_period_detected_without_echo("")

    def test_detects_owner_private_repository_before_sentence_period(self) -> None:
        self.assert_repository_identifier_with_terminal_period_detected_without_echo(
            "public-owner/"
        )

    def test_dotted_repository_continuations_remain_allowed(self) -> None:
        marker = "synthetic-project-42"
        repo = synthetic_private_repo(marker)
        candidates = (
            repo + ".docs",
            "public-owner/" + repo + ".docs",
            repo + ".git",
            "https://github.com/" + "public-owner/" + repo + ".docs",
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                rules = {
                    finding.rule
                    for finding in security_scan.scan_text(
                        "candidate-public-text", candidate
                    )
                }
                self.assertNotIn("private-repository-identifier", rules)

    def test_invalid_utf8_allowlist_fails_closed_without_runtime_path_echo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / security_scan.ALLOWLIST_FILE).write_bytes(bytes([0xFF, 0xFE]))

            with self.assertRaises(security_scan.ScanError) as caught:
                security_scan.load_allowlist(root)
            self.assertIn(security_scan.ALLOWLIST_FILE, str(caught.exception))
            self.assertNotIn(str(root), str(caught.exception))

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = security_scan.main(["--root", str(root)])
            rendered = stderr.getvalue()
            self.assertEqual(2, exit_code)
            self.assertIn("[security:scan] ERROR:", rendered)
            self.assertNotIn("Traceback", rendered)
            self.assertNotIn(str(root), rendered)


if __name__ == "__main__":
    unittest.main()
