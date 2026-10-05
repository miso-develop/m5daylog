from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "security_scan.py"
SPEC = importlib.util.spec_from_file_location("security_scan_issue95", MODULE_PATH)
assert SPEC and SPEC.loader
security_scan = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = security_scan
SPEC.loader.exec_module(security_scan)


def synthetic_openai_token() -> str:
    return "s" + "k-" + "A" * 28


def synthetic_posix_home(root_name: str, user_component: str) -> str:
    return "/" + root_name + "/" + user_component + "/workspace/notes.txt"


class Issue95ScannerRegressionTests(unittest.TestCase):
    def test_openai_signature_does_not_start_inside_larger_identifier(self) -> None:
        token = synthetic_openai_token()
        candidate = "synthetic-prefix-" + token
        rules = {finding.rule for finding in security_scan.scan_text("candidate", candidate)}
        self.assertNotIn("openai-token", rules)

    def test_standalone_openai_signature_still_detects_without_echo(self) -> None:
        token = synthetic_openai_token()
        findings = [
            finding
            for finding in security_scan.scan_text("candidate", token)
            if finding.rule == "openai-token"
        ]
        self.assertEqual(1, len(findings), findings)
        rendered = security_scan.render_finding(findings[0])
        self.assertNotIn(token, findings[0].message)
        self.assertNotIn(token, rendered)

    def test_placeholder_only_posix_home_component_is_not_concrete_home(self) -> None:
        placeholder_component = "." * 3
        for root_name in ("home", "Users"):
            with self.subTest(root_name=root_name):
                candidate = synthetic_posix_home(root_name, placeholder_component)
                rules = {
                    finding.rule
                    for finding in security_scan.scan_text("candidate", candidate)
                }
                self.assertNotIn("machine-path-posix-home", rules)

    def test_underscore_leading_concrete_posix_home_still_detects_without_echo(self) -> None:
        marker = "_synthetic-user-42"
        candidate = synthetic_posix_home("home", marker)
        findings = [
            finding
            for finding in security_scan.scan_text("candidate", candidate)
            if finding.rule == "machine-path-posix-home"
        ]
        self.assertEqual(1, len(findings), findings)
        rendered = security_scan.render_finding(findings[0])
        self.assertNotIn(marker, findings[0].message)
        self.assertNotIn(marker, rendered)


if __name__ == "__main__":
    unittest.main()
