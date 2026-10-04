from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "security_scan.py"
SPEC = importlib.util.spec_from_file_location("security_scan", MODULE_PATH)
assert SPEC and SPEC.loader
security_scan = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = security_scan
SPEC.loader.exec_module(security_scan)


def synthetic_github_token() -> str:
    return "g" + "hp_" + "A" * 36


def synthetic_hf_token() -> str:
    return "h" + "f_" + "A" * 32


def private_key_marker() -> str:
    return "-" * 5 + "BEGIN " + "PRIVATE KEY" + "-" * 5


def quoted_property(name: str, value: str) -> str:
    return '{"' + name + '": "' + value + '"}'


def synthetic_windows_home(marker: str = "synthetic-user-42") -> str:
    return "C:" + "\\" + "Users" + "\\" + marker + "\\" + "workspace" + "\\" + "notes.txt"


def synthetic_wsl_home(namespace: str, marker: str = "synthetic-user-42") -> str:
    return (
        "\\" + "\\" + namespace + "\\" + "SyntheticDistro" + "\\"
        + "home" + "\\" + marker + "\\" + "workspace"
    )


def synthetic_posix_home(root_name: str, marker: str = "synthetic-user-42") -> str:
    return "/" + root_name + "/" + marker + "/workspace/notes.txt"


def synthetic_private_repo(marker: str = "synthetic-project-42") -> str:
    return marker + "-" + "private"


class SecurityScanTests(unittest.TestCase):
    def assert_privacy_finding_non_echoing(
        self,
        candidate: str,
        expected_rule: str,
        marker: str,
        *,
        line: int = 1,
    ) -> None:
        findings = security_scan.scan_content("sample.txt", candidate.encode())
        matching = [finding for finding in findings if finding.rule == expected_rule]
        self.assertEqual(1, len(matching), findings)
        finding = matching[0]
        self.assertEqual(line, finding.line)
        self.assertNotIn(marker, finding.message)
        self.assertNotIn(candidate, finding.message)
        self.assertTrue(hasattr(security_scan, "render_finding"))
        rendered = security_scan.render_finding(finding)
        self.assertNotIn(marker, rendered)
        self.assertNotIn(candidate, rendered)

    def test_detects_github_token_without_echoing_value(self) -> None:
        value = synthetic_github_token()
        findings = security_scan.scan_content("sample.txt", value.encode())
        self.assertEqual(["github-token"], [f.rule for f in findings])
        self.assertNotIn(value, findings[0].message)

    def test_detects_huggingface_token(self) -> None:
        findings = security_scan.scan_content("sample.txt", synthetic_hf_token().encode())
        self.assertEqual(["huggingface-token"], [f.rule for f in findings])

    def test_detects_private_key_marker(self) -> None:
        findings = security_scan.scan_content("sample.txt", private_key_marker().encode())
        self.assertEqual(["private-key"], [f.rule for f in findings])

    def test_detects_credential_bearing_uri(self) -> None:
        uri = "https://" + "user:" + "synthetic-password" + "@example.invalid/resource"
        findings = security_scan.scan_content("sample.txt", uri.encode())
        self.assertEqual(["credential-uri"], [f.rule for f in findings])

    def test_detects_credential_literal_in_config(self) -> None:
        content = quoted_property("access_" + "token", "synthetic-value-only").encode()
        findings = security_scan.scan_content("config.json", content)
        self.assertEqual(["credential-literal"], [f.rule for f in findings])

    def test_documentation_credential_words_are_not_findings(self) -> None:
        content = b"Tokens and passwords must never be committed."
        self.assertEqual([], security_scan.scan_content("SECURITY.md", content))

    def test_env_example_allows_empty_values(self) -> None:
        content = b"# comment\nHF_TOKEN=\nM5DAYLOG_DATA_DIR=\n"
        self.assertEqual([], security_scan.scan_content(".env.example", content))

    def test_env_example_rejects_nonempty_values_without_echoing_them(self) -> None:
        marker = "synthetic-local-value"
        key = "HF_" + "TOKEN"
        content = (key + "=" + marker + "\n").encode()
        findings = security_scan.scan_content(".env.example", content)
        self.assertEqual(["env-template-value"], [f.rule for f in findings])
        self.assertNotIn(marker, findings[0].message)

    def test_tracked_local_env_path_is_forbidden(self) -> None:
        findings = security_scan.scan_path(".env.local")
        self.assertEqual(["forbidden-path"], [f.rule for f in findings])

    def test_audio_requires_explicit_allowlist(self) -> None:
        findings = security_scan.scan_path("fixtures/synthetic/sample.wav")
        self.assertEqual(["audio-file"], [f.rule for f in findings])

    def test_dangerous_log_detects_token_variable(self) -> None:
        content = ("logger." + "info(access_" + "token)").encode()
        findings = security_scan.scan_content("app.py", content)
        self.assertEqual(["dangerous-log"], [f.rule for f in findings])

    def test_detects_windows_user_profile_path_without_echo(self) -> None:
        marker = "synthetic-user-42"
        self.assert_privacy_finding_non_echoing(
            synthetic_windows_home(marker), "machine-path-windows", marker
        )

    def test_detects_wsl_dollar_namespace_home_without_echo(self) -> None:
        marker = "synthetic-user-42"
        self.assert_privacy_finding_non_echoing(
            synthetic_wsl_home("wsl" + "$", marker), "machine-path-wsl", marker
        )

    def test_detects_wsl_localhost_namespace_home_without_echo(self) -> None:
        marker = "synthetic-user-42"
        self.assert_privacy_finding_non_echoing(
            synthetic_wsl_home("wsl" + ".localhost", marker), "machine-path-wsl", marker
        )

    def test_detects_linux_user_home_without_echo(self) -> None:
        marker = "synthetic-user-42"
        self.assert_privacy_finding_non_echoing(
            synthetic_posix_home("home", marker), "machine-path-posix-home", marker
        )

    def test_detects_macos_user_home_without_echo(self) -> None:
        marker = "synthetic-user-42"
        self.assert_privacy_finding_non_echoing(
            synthetic_posix_home("Users", marker), "machine-path-posix-home", marker
        )

    def test_detects_bare_private_repository_identifier_without_echo(self) -> None:
        marker = "synthetic-project-42"
        self.assert_privacy_finding_non_echoing(
            synthetic_private_repo(marker), "private-repository-identifier", marker
        )

    def test_detects_owner_private_repository_identifier_without_echo(self) -> None:
        marker = "synthetic-project-42"
        candidate = "public-owner/" + synthetic_private_repo(marker)
        self.assert_privacy_finding_non_echoing(
            candidate, "private-repository-identifier", marker
        )

    def test_multiple_privacy_findings_preserve_line_numbers_without_echo(self) -> None:
        user_marker = "synthetic-user-42"
        repo_marker = "synthetic-project-42"
        text = (
            "safe first line\n"
            + synthetic_windows_home(user_marker)
            + "\nsafe middle line\n"
            + synthetic_private_repo(repo_marker)
            + "\n"
        )
        findings = security_scan.scan_content("public-text", text.encode())
        locations = {(finding.rule, finding.line) for finding in findings}
        self.assertEqual(
            {
                ("machine-path-windows", 2),
                ("private-repository-identifier", 4),
            },
            locations,
        )
        self.assertTrue(hasattr(security_scan, "render_finding"))
        rendered = "\n".join(security_scan.render_finding(finding) for finding in findings)
        self.assertNotIn(user_marker, rendered)
        self.assertNotIn(repo_marker, rendered)

    def test_reusable_text_scanner_uses_same_privacy_rules(self) -> None:
        marker = "synthetic-user-42"
        text = synthetic_posix_home("home", marker)
        self.assertTrue(hasattr(security_scan, "scan_text"))
        findings = security_scan.scan_text("candidate-public-text", text)
        self.assertEqual(["machine-path-posix-home"], [finding.rule for finding in findings])
        self.assertNotIn(marker, security_scan.render_finding(findings[0]))

    def test_safe_path_and_repository_forms_remain_allowed(self) -> None:
        safe_values = [
            r"%USERPROFILE%\Documents\M5Daylog",
            r"%HOMEDRIVE%%HOMEPATH%\Documents\M5Daylog",
            "$HOME/workspace",
            "${HOME}/workspace",
            "~/workspace",
            "<repo-root>/scripts/security_scan.py",
            "<worktree>/tests",
            "/" + "home/" + "<user>/workspace",
            "<evidence-dir>/result.json",
            "<private-repo>",
            "scripts/security_scan.py",
            "docs/architecture.md",
            "/api/v1/users/123",
            "/usr/local/bin/python3",
            "https://example.invalid/Users/example",
            "public-owner/public-project",
        ]
        for value in safe_values:
            with self.subTest(value=value):
                rules = {finding.rule for finding in security_scan.scan_content("sample.txt", value.encode())}
                self.assertFalse(
                    rules
                    & {
                        "machine-path-windows",
                        "machine-path-wsl",
                        "machine-path-posix-home",
                        "private-repository-identifier",
                    },
                    rules,
                )

    def test_allowlist_rejects_nonfixture_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / security_scan.ALLOWLIST_FILE).write_text(json.dumps({
                "version": 1,
                "entries": [{
                    "path": "src/example.txt",
                    "rule": "github-token",
                    "kind": "synthetic-fixture",
                    "file_sha256": "0" * 64,
                    "reason": "synthetic test",
                }],
            }), encoding="utf-8")
            with self.assertRaises(security_scan.ScanError):
                security_scan.load_allowlist(root)

    def test_privacy_rules_cannot_be_allowlisted(self) -> None:
        privacy_rules = (
            "machine-path-windows",
            "machine-path-wsl",
            "machine-path-posix-home",
            "private-repository-identifier",
        )
        for rule in privacy_rules:
            with self.subTest(rule=rule), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                (root / security_scan.ALLOWLIST_FILE).write_text(json.dumps({
                    "version": 1,
                    "entries": [{
                        "path": "fixtures/synthetic/example.txt",
                        "rule": rule,
                        "kind": "synthetic-fixture",
                        "file_sha256": "0" * 64,
                        "reason": "synthetic test",
                    }],
                }), encoding="utf-8")
                with self.assertRaises(security_scan.ScanError):
                    security_scan.load_allowlist(root)

    def test_exact_fixture_hash_can_be_allowlisted(self) -> None:
        data = synthetic_github_token().encode()
        digest = hashlib.sha256(data).hexdigest()
        path = "fixtures/synthetic/example.txt"
        finding = security_scan.scan_content(path, data)[0]
        entry = security_scan.AllowEntry(path, finding.rule, "synthetic-fixture", digest, "scanner fixture")
        remaining, stale = security_scan.apply_allowlist([finding], {path: digest}, [entry])
        self.assertEqual([], remaining)
        self.assertEqual([], stale)

    def test_changed_fixture_makes_allowlist_stale(self) -> None:
        data = synthetic_hf_token().encode()
        digest = hashlib.sha256(data).hexdigest()
        path = "fixtures/synthetic/example.txt"
        finding = security_scan.scan_content(path, data)[0]
        entry = security_scan.AllowEntry(path, finding.rule, "synthetic-fixture", "0" * 64, "scanner fixture")
        remaining, stale = security_scan.apply_allowlist([finding], {path: digest}, [entry])
        self.assertEqual([finding], remaining)
        self.assertEqual([entry], stale)

    def test_tracked_file_enumeration_failure_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(security_scan.ScanError):
                security_scan.git_tracked_files(Path(temp_dir))

    def test_tracked_file_read_failure_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / security_scan.ALLOWLIST_FILE).write_text(
                json.dumps({"version": 1, "entries": []}), encoding="utf-8"
            )
            with mock.patch.object(security_scan, "git_tracked_files", return_value=["missing.txt"]):
                with self.assertRaises(security_scan.ScanError):
                    security_scan.scan_repository(root)

    def test_scanner_source_and_tests_do_not_self_trigger_privacy_rules(self) -> None:
        privacy_rules = {
            "machine-path-windows",
            "machine-path-wsl",
            "machine-path-posix-home",
            "private-repository-identifier",
        }
        paths = [MODULE_PATH, Path(__file__).resolve()]
        for path in paths:
            with self.subTest(path=path.name):
                relative = path.relative_to(Path(__file__).resolve().parents[1]).as_posix()
                findings = security_scan.scan_content(relative, path.read_bytes())
                self.assertFalse(privacy_rules & {finding.rule for finding in findings}, findings)


if __name__ == "__main__":
    unittest.main()
