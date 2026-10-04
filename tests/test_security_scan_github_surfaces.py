from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "github_surface_scan.py"
EVENT_WORKFLOW = ROOT / ".github" / "workflows" / "github-surface-scan.yml"
AUDIT_WORKFLOW = ROOT / ".github" / "workflows" / "github-surface-audit.yml"


def load_surface_scan():
    if not MODULE_PATH.exists():
        raise AssertionError("GitHub public-surface scanner is not implemented")
    spec = importlib.util.spec_from_file_location("github_surface_scan", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def synthetic_private_repo(marker: str = "synthetic-project-42") -> str:
    return marker + "-" + "private"


def synthetic_windows_home(marker: str = "synthetic-user-42") -> str:
    return "C:" + "\\" + "Users" + "\\" + marker + "\\" + "workspace"


class GithubSurfaceScanTests(unittest.TestCase):
    def test_each_supported_event_extracts_only_required_public_text_fields(self) -> None:
        module = load_surface_scan()
        cases = [
            (
                "issues",
                {"action": "opened", "issue": {"number": 12, "title": "issue title", "body": "issue body"}},
                [
                    ("issue", "issue#12:title", "issue title"),
                    ("issue", "issue#12:body", "issue body"),
                ],
            ),
            (
                "pull_request_target",
                {"action": "edited", "pull_request": {"number": 13, "title": "pr title", "body": "pr body"}},
                [
                    ("pull-request", "pr#13:title", "pr title"),
                    ("pull-request", "pr#13:body", "pr body"),
                ],
            ),
            (
                "issue_comment",
                {"action": "created", "issue": {"number": 14}, "comment": {"id": 1401, "body": "conversation body"}},
                [("issue-comment", "issue#14/comment#1401", "conversation body")],
            ),
            (
                "pull_request_review",
                {"action": "submitted", "pull_request": {"number": 15}, "review": {"id": 1501, "body": "review body"}},
                [("pull-request-review", "pr#15/review#1501", "review body")],
            ),
            (
                "pull_request_review_comment",
                {"action": "edited", "pull_request": {"number": 16}, "comment": {"id": 1601, "body": "inline body"}},
                [("pull-request-review-comment", "pr#16/review-comment#1601", "inline body")],
            ),
        ]
        for event_name, payload, expected in cases:
            with self.subTest(event_name=event_name):
                candidates = module.extract_event_candidates(event_name, payload)
                actual = [(item.surface, item.locator, item.text) for item in candidates]
                self.assertEqual(expected, actual)

    def test_supported_event_actions_match_contract(self) -> None:
        module = load_surface_scan()
        cases = {
            "issues": ("opened", "edited", "transferred", "reopened"),
            "pull_request_target": ("opened", "edited", "reopened"),
            "issue_comment": ("created", "edited"),
            "pull_request_review": ("submitted", "edited"),
            "pull_request_review_comment": ("created", "edited"),
        }
        for event_name, actions in cases.items():
            for action in actions:
                with self.subTest(event_name=event_name, action=action):
                    if event_name == "issues":
                        payload = {"action": action, "issue": {"number": 1, "title": "safe", "body": None}}
                    elif event_name == "pull_request_target":
                        payload = {"action": action, "pull_request": {"number": 1, "title": "safe", "body": None}}
                    elif event_name == "issue_comment":
                        payload = {"action": action, "issue": {"number": 1}, "comment": {"id": 2, "body": "safe"}}
                    elif event_name == "pull_request_review":
                        payload = {"action": action, "pull_request": {"number": 1}, "review": {"id": 2, "body": None}}
                    else:
                        payload = {"action": action, "pull_request": {"number": 1}, "comment": {"id": 2, "body": "safe"}}
                    module.extract_event_candidates(event_name, payload)

    def test_unsafe_event_text_is_detected_without_echo_and_safe_text_passes(self) -> None:
        module = load_surface_scan()
        marker = "synthetic-project-42"
        unsafe = synthetic_private_repo(marker)
        payload = {"action": "opened", "issue": {"number": 21, "title": "safe", "body": unsafe}}
        findings = module.scan_event("issues", payload)
        self.assertEqual(1, len(findings))
        self.assertEqual("private-repository-identifier", findings[0].rule)
        rendered = module.render_public_finding(findings[0])
        self.assertIn("issue#21:body", rendered)
        self.assertNotIn(marker, rendered)
        self.assertNotIn(unsafe, rendered)

        safe_payload = {"action": "opened", "issue": {"number": 22, "title": "safe", "body": "<repo-root>/notes"}}
        self.assertEqual([], module.scan_event("issues", safe_payload))

    def test_malformed_or_missing_expected_event_field_fails_closed(self) -> None:
        module = load_surface_scan()
        invalid_payloads = [
            ("issues", {"action": "opened", "issue": {"number": 1, "body": "missing title"}}),
            ("pull_request_target", {"action": "opened", "pull_request": {"number": 1, "title": 123, "body": "safe"}}),
            ("issue_comment", {"action": "created", "issue": {"number": 1}, "comment": {"id": 2}}),
            ("pull_request_review", {"action": "submitted", "pull_request": {"number": 1}, "review": {"body": "missing id"}}),
            ("pull_request_review_comment", {"action": "created", "pull_request": {"number": 1}, "comment": {"id": 2, "body": None}}),
        ]
        for event_name, payload in invalid_payloads:
            with self.subTest(event_name=event_name):
                with self.assertRaises(module.SurfaceScanError):
                    module.extract_event_candidates(event_name, payload)

    def test_cli_event_failure_and_finding_outputs_never_echo_candidate_or_payload(self) -> None:
        module = load_surface_scan()
        marker = "synthetic-user-42"
        candidate = synthetic_windows_home(marker)
        payload = {"action": "edited", "comment": {"id": 7, "body": candidate}, "issue": {"number": 3}}
        with tempfile.TemporaryDirectory() as temp_dir:
            event_path = Path(temp_dir) / "event.json"
            event_path.write_text(json.dumps(payload), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = module.main(["event", "--event-name", "issue_comment", "--event-path", str(event_path)])
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(1, code)
        self.assertNotIn(marker, combined)
        self.assertNotIn(candidate, combined)
        self.assertNotIn(json.dumps(payload), combined)
        self.assertIn("machine-path-windows", combined)

        with tempfile.TemporaryDirectory() as temp_dir:
            event_path = Path(temp_dir) / "event.json"
            event_path.write_text("{", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = module.main(["event", "--event-name", "issues", "--event-path", str(event_path)])
        self.assertEqual(2, code)
        self.assertIn("event-json-invalid", stderr.getvalue())
        self.assertNotIn(str(event_path), stderr.getvalue())

    def test_historical_audit_paginates_every_required_surface(self) -> None:
        module = load_surface_scan()
        calls: list[tuple[str, int, int]] = []

        def fetch_page(endpoint: str, page: int, per_page: int):
            calls.append((endpoint, page, per_page))
            if endpoint == "/issues":
                if page == 1:
                    return [
                        {"number": 1, "title": "safe", "body": "safe"},
                        {"number": 2, "title": "safe", "body": None},
                    ]
                if page == 2:
                    return [{"number": 3, "title": "safe", "body": "safe"}]
            if endpoint == "/pulls":
                if page == 1:
                    return [
                        {"number": 7, "title": "safe", "body": "safe"},
                        {"number": 8, "title": "safe", "body": None},
                    ]
                if page == 2:
                    return [{"number": 9, "title": "safe", "body": "safe"}]
            if endpoint == "/issues/comments":
                if page == 1:
                    return [
                        {"id": 11, "issue_url": "https://api.github.com/repos/public-owner/public-repo/issues/1", "body": "safe"},
                        {"id": 12, "issue_url": "https://api.github.com/repos/public-owner/public-repo/issues/2", "body": "safe"},
                    ]
                if page == 2:
                    return [{"id": 13, "issue_url": "https://api.github.com/repos/public-owner/public-repo/issues/3", "body": "safe"}]
            if endpoint == "/pulls/comments":
                if page == 1:
                    return [
                        {"id": 21, "pull_request_url": "https://api.github.com/repos/public-owner/public-repo/pulls/7", "body": "safe"},
                        {"id": 22, "pull_request_url": "https://api.github.com/repos/public-owner/public-repo/pulls/8", "body": "safe"},
                    ]
                if page == 2:
                    return [{"id": 23, "pull_request_url": "https://api.github.com/repos/public-owner/public-repo/pulls/9", "body": "safe"}]
            if endpoint == "/pulls/7/reviews":
                if page == 1:
                    return [{"id": 31, "body": "safe"}, {"id": 32, "body": None}]
                if page == 2:
                    return [{"id": 33, "body": "safe"}]
            return []

        findings = module.audit_repository("public-owner/public-repo", fetch_page, page_size=2)
        self.assertEqual([], findings)
        for endpoint in ("/issues", "/pulls", "/issues/comments", "/pulls/comments", "/pulls/7/reviews"):
            self.assertIn((endpoint, 2, 2), calls)

    def test_historical_retrieval_or_pagination_failure_fails_closed(self) -> None:
        module = load_surface_scan()

        def fetch_page(endpoint: str, page: int, per_page: int):
            if endpoint == "/issues" and page == 2:
                raise RuntimeError("synthetic transport detail that must not escape")
            if endpoint == "/issues" and page == 1:
                return [
                    {"number": 1, "title": "safe", "body": "safe"},
                    {"number": 2, "title": "safe", "body": "safe"},
                ]
            return []

        with self.assertRaises(module.SurfaceScanError) as caught:
            module.audit_repository("public-owner/public-repo", fetch_page, page_size=2)
        self.assertEqual("api-request-failed", caught.exception.error_class)
        self.assertNotIn("synthetic transport detail", str(caught.exception))

    def test_historical_findings_render_only_safe_locator_rule_and_count(self) -> None:
        module = load_surface_scan()
        marker = "synthetic-project-42"
        unsafe = synthetic_private_repo(marker)

        def fetch_page(endpoint: str, page: int, per_page: int):
            if endpoint == "/issues" and page == 1:
                return [{"number": 7, "title": "safe", "body": unsafe}]
            return []

        findings = module.audit_repository("public-owner/public-repo", fetch_page, page_size=100)
        self.assertEqual(1, len(findings))
        rendered = module.render_public_finding(findings[0])
        self.assertIn("issue#7:body", rendered)
        self.assertIn("private-repository-identifier", rendered)
        self.assertIn("count=1", rendered)
        self.assertNotIn(marker, rendered)
        self.assertNotIn(unsafe, rendered)

    def test_workflows_enforce_trusted_code_permissions_and_no_payload_artifacts(self) -> None:
        self.assertTrue(EVENT_WORKFLOW.exists(), "event workflow is missing")
        self.assertTrue(AUDIT_WORKFLOW.exists(), "historical audit workflow is missing")
        event_text = EVENT_WORKFLOW.read_text(encoding="utf-8")
        audit_text = AUDIT_WORKFLOW.read_text(encoding="utf-8")

        for event_name in (
            "issues:",
            "pull_request_target:",
            "issue_comment:",
            "pull_request_review:",
            "pull_request_review_comment:",
        ):
            self.assertIn(event_name, event_text)

        self.assertIn("ref: ${{ github.event.pull_request.base.sha }}", event_text)
        self.assertNotIn("github.event.pull_request.head.sha", event_text)
        self.assertNotIn("github.head_ref", event_text)
        self.assertIn("permissions:\n  contents: read\n", event_text)
        self.assertNotIn("issues: write", event_text)
        self.assertNotIn("pull-requests: write", event_text)
        self.assertNotIn("checks: write", event_text)
        self.assertNotIn("statuses: write", event_text)
        self.assertNotIn("actions: write", event_text)

        self.assertIn("workflow_dispatch:", audit_text)
        self.assertIn("permissions:\n  contents: read\n  issues: read\n  pull-requests: read\n", audit_text)

        for workflow_text in (event_text, audit_text):
            self.assertNotIn("upload-artifact", workflow_text)
            self.assertNotIn("actions/cache", workflow_text)
            self.assertNotIn("github-script", workflow_text)


if __name__ == "__main__":
    unittest.main()
