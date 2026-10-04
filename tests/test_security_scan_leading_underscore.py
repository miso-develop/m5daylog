from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "security_scan.py"
SPEC = importlib.util.spec_from_file_location("security_scan_leading_underscore", MODULE_PATH)
assert SPEC and SPEC.loader
security_scan = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = security_scan
SPEC.loader.exec_module(security_scan)


def windows_home(marker: str) -> str:
    return "C:" + "\\" + "Users" + "\\" + marker + "\\" + "workspace"


def wsl_home(marker: str) -> str:
    return (
        "\\" + "\\" + ("wsl" + "$") + "\\" + "SyntheticDistro" + "\\"
        + "home" + "\\" + marker + "\\" + "workspace"
    )


def posix_home(root_name: str, marker: str) -> str:
    return "/" + root_name + "/" + marker + "/workspace"


class LeadingUnderscoreUserHomeTests(unittest.TestCase):
    def test_detects_leading_underscore_user_home_paths_without_echo(self) -> None:
        marker = "_synthetic-user-42"
        cases = (
            (windows_home(marker), "machine-path-windows"),
            (wsl_home(marker), "machine-path-wsl"),
            (posix_home("home", marker), "machine-path-posix-home"),
            (posix_home("Users", marker), "machine-path-posix-home"),
        )

        for candidate, expected_rule in cases:
            with self.subTest(expected_rule=expected_rule, candidate_kind=candidate[:2]):
                findings = security_scan.scan_content("sample.txt", candidate.encode())
                matching = [finding for finding in findings if finding.rule == expected_rule]
                self.assertEqual(1, len(matching), findings)
                rendered = security_scan.render_finding(matching[0])
                self.assertNotIn(marker, matching[0].message)
                self.assertNotIn(marker, rendered)
                self.assertNotIn(candidate, rendered)


if __name__ == "__main__":
    unittest.main()
