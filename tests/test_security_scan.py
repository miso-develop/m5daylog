from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

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


class SecurityScanTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
