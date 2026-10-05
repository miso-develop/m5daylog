#!/usr/bin/env python3
"""Non-echoing GitHub public-surface scanner and historical baseline auditor."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import security_scan  # noqa: E402

DEFAULT_PAGE_SIZE = 100
API_ROOT = "https://api.github.com"
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
NUMBER_URL_RE = re.compile(r"/(issues|pulls)/(\d+)\Z")

SUPPORTED_ACTIONS = {
    "issues": frozenset({"opened", "edited", "transferred", "reopened"}),
    "pull_request_target": frozenset({"opened", "edited", "reopened"}),
    "issue_comment": frozenset({"created", "edited"}),
    "pull_request_review": frozenset({"submitted", "edited"}),
    "pull_request_review_comment": frozenset({"created", "edited"}),
}


class SurfaceScanError(RuntimeError):
    """Fail-closed error carrying only a public-safe error class."""

    def __init__(self, error_class: str):
        self.error_class = error_class
        super().__init__(error_class)


@dataclass(frozen=True)
class SurfaceCandidate:
    surface: str
    locator: str
    text: str


@dataclass(frozen=True)
class PublicFinding:
    surface: str
    locator: str
    rule: str
    count: int


@dataclass(frozen=True)
class FetchPageResult:
    items: list[dict[str, object]]
    next_page: int | None


FetchPage = Callable[[str, int, int], FetchPageResult | list[dict[str, object]]]


def _require_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SurfaceScanError("event-structure-invalid")
    return value


def _require_child_object(parent: dict[str, object], key: str) -> dict[str, object]:
    if key not in parent:
        raise SurfaceScanError("event-structure-invalid")
    return _require_object(parent[key])


def _require_positive_int(parent: dict[str, object], key: str) -> int:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SurfaceScanError("event-structure-invalid")
    return value


def _require_text(parent: dict[str, object], key: str, *, nullable: bool = False) -> str:
    if key not in parent:
        raise SurfaceScanError("event-structure-invalid")
    value = parent[key]
    if value is None and nullable:
        return ""
    if not isinstance(value, str):
        raise SurfaceScanError("event-structure-invalid")
    return value


def extract_event_candidates(event_name: str, payload: object) -> list[SurfaceCandidate]:
    root = _require_object(payload)
    actions = SUPPORTED_ACTIONS.get(event_name)
    if actions is None:
        raise SurfaceScanError("event-unsupported")
    action = root.get("action")
    if not isinstance(action, str) or action not in actions:
        raise SurfaceScanError("event-action-invalid")

    if event_name == "issues":
        issue = _require_child_object(root, "issue")
        number = _require_positive_int(issue, "number")
        return [
            SurfaceCandidate("issue", f"issue#{number}:title", _require_text(issue, "title")),
            SurfaceCandidate("issue", f"issue#{number}:body", _require_text(issue, "body", nullable=True)),
        ]

    if event_name == "pull_request_target":
        pull = _require_child_object(root, "pull_request")
        number = _require_positive_int(pull, "number")
        return [
            SurfaceCandidate("pull-request", f"pr#{number}:title", _require_text(pull, "title")),
            SurfaceCandidate("pull-request", f"pr#{number}:body", _require_text(pull, "body", nullable=True)),
        ]

    if event_name == "issue_comment":
        issue = _require_child_object(root, "issue")
        comment = _require_child_object(root, "comment")
        number = _require_positive_int(issue, "number")
        comment_id = _require_positive_int(comment, "id")
        return [
            SurfaceCandidate(
                "issue-comment",
                f"issue#{number}/comment#{comment_id}",
                _require_text(comment, "body"),
            )
        ]

    if event_name == "pull_request_review":
        pull = _require_child_object(root, "pull_request")
        review = _require_child_object(root, "review")
        number = _require_positive_int(pull, "number")
        review_id = _require_positive_int(review, "id")
        return [
            SurfaceCandidate(
                "pull-request-review",
                f"pr#{number}/review#{review_id}",
                _require_text(review, "body", nullable=True),
            )
        ]

    pull = _require_child_object(root, "pull_request")
    comment = _require_child_object(root, "comment")
    number = _require_positive_int(pull, "number")
    comment_id = _require_positive_int(comment, "id")
    return [
        SurfaceCandidate(
            "pull-request-review-comment",
            f"pr#{number}/review-comment#{comment_id}",
            _require_text(comment, "body"),
        )
    ]


def scan_candidates(candidates: Iterable[SurfaceCandidate]) -> list[PublicFinding]:
    public_findings: list[PublicFinding] = []
    for candidate in candidates:
        try:
            findings = security_scan.scan_text(candidate.locator, candidate.text)
        except Exception as exc:
            raise SurfaceScanError("scanner-failure") from exc
        counts = Counter(finding.rule for finding in findings)
        for rule in sorted(counts):
            public_findings.append(
                PublicFinding(candidate.surface, candidate.locator, rule, counts[rule])
            )
    return public_findings


def scan_event(event_name: str, payload: object) -> list[PublicFinding]:
    return scan_candidates(extract_event_candidates(event_name, payload))


def render_public_finding(finding: PublicFinding) -> str:
    return (
        f"[github-surface:scan] {finding.surface} {finding.locator} "
        f"[{finding.rule}] count={finding.count}"
    )


def _read_event_json(path: Path) -> object:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise SurfaceScanError("event-json-invalid") from exc
    except (OSError, UnicodeError) as exc:
        raise SurfaceScanError("event-read-failed") from exc


def _page_digest(items: list[dict[str, object]]) -> bytes:
    try:
        payload = json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SurfaceScanError("api-response-invalid") from exc
    return hashlib.sha256(payload.encode("utf-8")).digest()


def _paginate(endpoint: str, fetch_page: FetchPage, page_size: int) -> Iterable[dict[str, object]]:
    if page_size < 1 or page_size > 100:
        raise SurfaceScanError("pagination-config-invalid")
    page = 1
    seen_pages: set[bytes] = set()
    while True:
        try:
            result = fetch_page(endpoint, page, page_size)
        except SurfaceScanError:
            raise
        except Exception as exc:
            raise SurfaceScanError("api-request-failed") from exc

        if isinstance(result, FetchPageResult):
            items = result.items
            next_page = result.next_page
            metadata_driven = True
        elif isinstance(result, list):
            # Compatibility for pre-existing injected synthetic fixtures. The real
            # GitHub fetch path always returns FetchPageResult and never infers
            # production continuation from item count.
            items = result
            next_page = None if len(items) < page_size else page + 1
            metadata_driven = False
        else:
            raise SurfaceScanError("api-response-invalid")

        if len(items) > page_size or any(not isinstance(item, dict) for item in items):
            raise SurfaceScanError("api-response-invalid")
        if items:
            digest = _page_digest(items)
            if digest in seen_pages:
                raise SurfaceScanError("api-pagination-stalled")
            seen_pages.add(digest)
        for item in items:
            yield item

        if next_page is None:
            return
        if isinstance(next_page, bool) or not isinstance(next_page, int) or next_page != page + 1:
            raise SurfaceScanError("api-pagination-ambiguous")
        if metadata_driven and not items:
            raise SurfaceScanError("api-pagination-ambiguous")
        page = next_page


def _api_positive_int(item: dict[str, object], key: str) -> int:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SurfaceScanError("api-response-invalid")
    return value


def _api_text(item: dict[str, object], key: str, *, nullable: bool = False) -> str:
    if key not in item:
        raise SurfaceScanError("api-response-invalid")
    value = item[key]
    if value is None and nullable:
        return ""
    if not isinstance(value, str):
        raise SurfaceScanError("api-response-invalid")
    return value


def _number_from_api_url(value: object, expected_kind: str) -> int:
    if not isinstance(value, str):
        raise SurfaceScanError("api-response-invalid")
    match = NUMBER_URL_RE.search(value)
    if not match or match.group(1) != expected_kind:
        raise SurfaceScanError("api-response-invalid")
    number = int(match.group(2))
    if number <= 0:
        raise SurfaceScanError("api-response-invalid")
    return number


def _extend_findings(target: list[PublicFinding], candidates: list[SurfaceCandidate]) -> None:
    target.extend(scan_candidates(candidates))


def audit_repository(
    repository: str,
    fetch_page: FetchPage,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[PublicFinding]:
    if not REPOSITORY_RE.fullmatch(repository):
        raise SurfaceScanError("repository-invalid")

    findings: list[PublicFinding] = []
    seen_issue_numbers: set[int] = set()
    seen_pull_numbers: set[int] = set()
    pull_numbers: list[int] = []
    seen_issue_comment_ids: set[int] = set()
    seen_review_comment_ids: set[int] = set()

    for item in _paginate("/issues", fetch_page, page_size):
        if "pull_request" in item:
            continue
        number = _api_positive_int(item, "number")
        if number in seen_issue_numbers:
            raise SurfaceScanError("api-pagination-ambiguous")
        seen_issue_numbers.add(number)
        _extend_findings(
            findings,
            [
                SurfaceCandidate("issue", f"issue#{number}:title", _api_text(item, "title")),
                SurfaceCandidate("issue", f"issue#{number}:body", _api_text(item, "body", nullable=True)),
            ],
        )

    for item in _paginate("/pulls", fetch_page, page_size):
        number = _api_positive_int(item, "number")
        if number in seen_pull_numbers:
            raise SurfaceScanError("api-pagination-ambiguous")
        seen_pull_numbers.add(number)
        pull_numbers.append(number)
        _extend_findings(
            findings,
            [
                SurfaceCandidate("pull-request", f"pr#{number}:title", _api_text(item, "title")),
                SurfaceCandidate("pull-request", f"pr#{number}:body", _api_text(item, "body", nullable=True)),
            ],
        )

    for item in _paginate("/issues/comments", fetch_page, page_size):
        comment_id = _api_positive_int(item, "id")
        if comment_id in seen_issue_comment_ids:
            raise SurfaceScanError("api-pagination-ambiguous")
        seen_issue_comment_ids.add(comment_id)
        number = _number_from_api_url(item.get("issue_url"), "issues")
        _extend_findings(
            findings,
            [
                SurfaceCandidate(
                    "issue-comment",
                    f"issue#{number}/comment#{comment_id}",
                    _api_text(item, "body"),
                )
            ],
        )

    for item in _paginate("/pulls/comments", fetch_page, page_size):
        comment_id = _api_positive_int(item, "id")
        if comment_id in seen_review_comment_ids:
            raise SurfaceScanError("api-pagination-ambiguous")
        seen_review_comment_ids.add(comment_id)
        number = _number_from_api_url(item.get("pull_request_url"), "pulls")
        _extend_findings(
            findings,
            [
                SurfaceCandidate(
                    "pull-request-review-comment",
                    f"pr#{number}/review-comment#{comment_id}",
                    _api_text(item, "body"),
                )
            ],
        )

    for number in pull_numbers:
        seen_review_ids: set[int] = set()
        for item in _paginate(f"/pulls/{number}/reviews", fetch_page, page_size):
            review_id = _api_positive_int(item, "id")
            if review_id in seen_review_ids:
                raise SurfaceScanError("api-pagination-ambiguous")
            seen_review_ids.add(review_id)
            _extend_findings(
                findings,
                [
                    SurfaceCandidate(
                        "pull-request-review",
                        f"pr#{number}/review#{review_id}",
                        _api_text(item, "body", nullable=True),
                    )
                ],
            )

    return findings


def _pagination_resource_path_matches(expected_path: str, candidate_path: str) -> bool:
    if candidate_path == expected_path:
        return True

    expected_match = re.fullmatch(r"/repos/[^/]+/[^/]+(?P<suffix>/.*)", expected_path)
    candidate_match = re.fullmatch(r"/repositories/[1-9][0-9]*(?P<suffix>/.*)", candidate_path)
    return bool(
        expected_match
        and candidate_match
        and expected_match.group("suffix") == candidate_match.group("suffix")
    )


def _next_url_from_link(link_header: str | None, request_url: str) -> str | None:
    if link_header is None:
        return None
    if not isinstance(link_header, str) or not link_header.strip():
        raise SurfaceScanError("api-pagination-ambiguous")

    relations: dict[str, str] = {}
    for raw_entry in link_header.split(","):
        parts = [part.strip() for part in raw_entry.split(";")]
        if len(parts) < 2 or not parts[0].startswith("<") or not parts[0].endswith(">"):
            raise SurfaceScanError("api-pagination-ambiguous")
        target = parts[0][1:-1]
        if not target:
            raise SurfaceScanError("api-pagination-ambiguous")

        rel_tokens: list[str] | None = None
        for parameter in parts[1:]:
            if "=" not in parameter:
                raise SurfaceScanError("api-pagination-ambiguous")
            name, value = (piece.strip() for piece in parameter.split("=", 1))
            if not name or not value:
                raise SurfaceScanError("api-pagination-ambiguous")
            if name.lower() == "rel":
                if rel_tokens is not None or len(value) < 2 or value[0] != '"' or value[-1] != '"':
                    raise SurfaceScanError("api-pagination-ambiguous")
                rel_tokens = value[1:-1].split()
                if not rel_tokens:
                    raise SurfaceScanError("api-pagination-ambiguous")

        if rel_tokens is None:
            raise SurfaceScanError("api-pagination-ambiguous")
        for relation in rel_tokens:
            if relation in relations:
                raise SurfaceScanError("api-pagination-ambiguous")
            relations[relation] = target

    next_url = relations.get("next")
    if next_url is None:
        return None

    try:
        expected = urllib.parse.urlsplit(request_url)
        candidate = urllib.parse.urlsplit(next_url)
        expected_query = urllib.parse.parse_qs(
            expected.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
        candidate_query = urllib.parse.parse_qs(
            candidate.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError as exc:
        raise SurfaceScanError("api-pagination-ambiguous") from exc

    if (
        expected.scheme != "https"
        or expected.netloc != "api.github.com"
        or candidate.scheme != expected.scheme
        or candidate.netloc != expected.netloc
        or not _pagination_resource_path_matches(expected.path, candidate.path)
        or candidate.fragment
        or expected.fragment
    ):
        raise SurfaceScanError("api-pagination-ambiguous")

    # Preserve GitHub-owned continuation state, but never allow pagination to
    # silently drop or change the stable semantics of the initial audit request.
    missing_semantics: list[tuple[str, str]] = []
    for key in ("per_page", "state", "sort", "direction"):
        expected_values = expected_query.get(key)
        candidate_values = candidate_query.get(key)
        if expected_values is not None and len(expected_values) != 1:
            raise SurfaceScanError("api-pagination-ambiguous")
        if candidate_values is not None:
            if (
                expected_values is None
                or len(candidate_values) != 1
                or candidate_values != expected_values
            ):
                raise SurfaceScanError("api-pagination-ambiguous")
        elif expected_values is not None:
            missing_semantics.append((key, expected_values[0]))

    if missing_semantics:
        semantic_query = urllib.parse.urlencode(missing_semantics)
        combined_query = (
            f"{candidate.query}&{semantic_query}" if candidate.query else semantic_query
        )
        next_url = urllib.parse.urlunsplit(
            (candidate.scheme, candidate.netloc, candidate.path, combined_query, "")
        )

    return next_url


def _initial_github_url(repository: str, endpoint: str, page: int, per_page: int) -> str:
    if not endpoint.startswith("/") or ".." in endpoint:
        raise SurfaceScanError("api-endpoint-invalid")
    if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
        raise SurfaceScanError("api-pagination-ambiguous")
    if isinstance(per_page, bool) or not isinstance(per_page, int) or per_page < 1 or per_page > 100:
        raise SurfaceScanError("pagination-config-invalid")

    params: dict[str, str | int] = {"per_page": per_page, "page": page}
    if endpoint in {"/issues", "/pulls"}:
        params.update({"state": "all", "sort": "created", "direction": "asc"})
    elif endpoint in {"/issues/comments", "/pulls/comments"}:
        params.update({"sort": "created", "direction": "asc"})
    return f"{API_ROOT}/repos/{repository}{endpoint}?{urllib.parse.urlencode(params)}"


def make_github_fetch_page(repository: str, token: str) -> FetchPage:
    if not REPOSITORY_RE.fullmatch(repository):
        raise SurfaceScanError("repository-invalid")
    if not token:
        raise SurfaceScanError("auth-missing")

    continuations: dict[tuple[str, int], str] = {}
    seen_urls: dict[str, set[str]] = {}

    def fetch_page(endpoint: str, page: int, per_page: int) -> FetchPageResult:
        if not endpoint.startswith("/") or ".." in endpoint:
            raise SurfaceScanError("api-endpoint-invalid")
        if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
            raise SurfaceScanError("api-pagination-ambiguous")

        if page == 1:
            url = _initial_github_url(repository, endpoint, page, per_page)
        else:
            url = continuations.pop((endpoint, page), "")
            if not url:
                raise SurfaceScanError("api-pagination-ambiguous")

        endpoint_seen = seen_urls.setdefault(endpoint, set())
        if url in endpoint_seen:
            raise SurfaceScanError("api-pagination-stalled")
        endpoint_seen.add(url)

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "m5daylog-public-surface-audit",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status != 200:
                    raise SurfaceScanError("api-request-failed")
                link_header = response.headers.get("Link")
                raw = response.read()
            next_url = _next_url_from_link(link_header, url)
            payload = json.loads(raw)
        except SurfaceScanError:
            raise
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            OSError,
            TimeoutError,
            json.JSONDecodeError,
            UnicodeError,
        ) as exc:
            raise SurfaceScanError("api-request-failed") from exc

        if not isinstance(payload, list):
            raise SurfaceScanError("api-response-invalid")
        if any(not isinstance(item, dict) for item in payload):
            raise SurfaceScanError("api-response-invalid")

        next_page: int | None = None
        if next_url is not None:
            if next_url in endpoint_seen:
                raise SurfaceScanError("api-pagination-stalled")
            next_page = page + 1
            continuation_key = (endpoint, next_page)
            if continuation_key in continuations:
                raise SurfaceScanError("api-pagination-ambiguous")
            continuations[continuation_key] = next_url

        return FetchPageResult(payload, next_page)

    return fetch_page


def _print_findings(findings: list[PublicFinding]) -> None:
    for finding in findings:
        print(render_public_finding(finding), file=sys.stderr)


def _run_event(args: argparse.Namespace) -> int:
    payload = _read_event_json(args.event_path)
    findings = scan_event(args.event_name, payload)
    _print_findings(findings)
    if findings:
        print(f"[github-surface:scan] FAILED: {len(findings)} finding group(s)", file=sys.stderr)
        return 1
    print("[github-surface:scan] OK: event surface clean")
    return 0


def _run_audit(args: argparse.Namespace) -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    fetch_page = make_github_fetch_page(args.repository, token)
    findings = audit_repository(args.repository, fetch_page)
    _print_findings(findings)
    if findings:
        print(f"[github-surface:audit] FAILED: {len(findings)} finding group(s)", file=sys.stderr)
        return 1
    print("[github-surface:audit] OK: historical baseline clean")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan GitHub public discussion surfaces without echoing matched content")
    subparsers = parser.add_subparsers(dest="command", required=True)

    event_parser = subparsers.add_parser("event", help="scan one GitHub Actions event payload")
    event_parser.add_argument("--event-name", required=True, choices=tuple(SUPPORTED_ACTIONS))
    event_parser.add_argument("--event-path", type=Path, required=True)
    event_parser.set_defaults(handler=_run_event)

    audit_parser = subparsers.add_parser("audit", help="scan all historical public discussion surfaces")
    audit_parser.add_argument("--repository", required=True)
    audit_parser.set_defaults(handler=_run_audit)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        return int(args.handler(args))
    except SurfaceScanError as exc:
        print(f"[github-surface:scan] ERROR: {exc.error_class}", file=sys.stderr)
        return 2
    except Exception:
        print("[github-surface:scan] ERROR: internal-error", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())