from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "github_surface_scan.py"


def load_surface_scan():
    spec = importlib.util.spec_from_file_location("github_surface_scan_pagination", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload: list[dict[str, object]], link_header: str | None = None) -> None:
        self.status = 200
        self.headers = {} if link_header is None else {"Link": link_header}
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


class GithubSurfacePaginationRegressionTests(unittest.TestCase):
    def test_short_page_with_continuation_metadata_fetches_next_page(self) -> None:
        module = load_surface_scan()
        calls: list[tuple[str, int, int]] = []

        def fetch_page(endpoint: str, page: int, per_page: int):
            calls.append((endpoint, page, per_page))
            if endpoint == "/issues" and page == 1:
                return module.FetchPageResult(
                    items=[{"number": 1, "title": "safe", "body": "safe"}],
                    next_page=2,
                )
            if endpoint == "/issues" and page == 2:
                return module.FetchPageResult(
                    items=[{"number": 2, "title": "safe", "body": "safe"}],
                    next_page=None,
                )
            return module.FetchPageResult(items=[], next_page=None)

        findings = module.audit_repository("public-owner/public-repo", fetch_page, page_size=2)

        self.assertEqual([], findings)
        self.assertIn(("/issues", 2, 2), calls)

    def test_invalid_continuation_metadata_fails_closed(self) -> None:
        module = load_surface_scan()

        def fetch_page(endpoint: str, page: int, per_page: int):
            if endpoint == "/issues" and page == 1:
                return module.FetchPageResult(
                    items=[{"number": 1, "title": "safe", "body": "safe"}],
                    next_page=3,
                )
            return module.FetchPageResult(items=[], next_page=None)

        with self.assertRaises(module.SurfaceScanError) as caught:
            module.audit_repository("public-owner/public-repo", fetch_page, page_size=2)

        self.assertEqual("api-pagination-ambiguous", caught.exception.error_class)

    def test_link_header_returns_exact_validated_next_url(self) -> None:
        module = load_surface_scan()
        request_url = (
            "https://api.github.com/repos/public-owner/public-repo/issues"
            "?per_page=2&page=1&state=all&sort=created&direction=asc"
        )
        next_url = (
            "https://api.github.com/repos/public-owner/public-repo/issues"
            "?per_page=2&page=2&state=all&sort=created&direction=asc"
        )
        link_header = (
            f'<{next_url}>; rel="next", '
            '<https://api.github.com/repos/public-owner/public-repo/issues?per_page=2&page=4&state=all&sort=created&direction=asc>; rel="last"'
        )

        self.assertEqual(next_url, module._next_url_from_link(link_header, request_url))

    def test_link_header_accepts_github_canonical_repository_path(self) -> None:
        module = load_surface_scan()
        request_url = (
            "https://api.github.com/repos/public-owner/public-repo/issues"
            "?per_page=2&page=1&state=all&sort=created&direction=asc"
        )
        next_url = (
            "https://api.github.com/repositories/123456/issues"
            "?per_page=2&page=2&state=all&sort=created&direction=asc"
        )
        link_header = f'<{next_url}>; rel="next"'

        self.assertEqual(next_url, module._next_url_from_link(link_header, request_url))

    def test_link_header_accepts_canonical_nested_review_resource(self) -> None:
        module = load_surface_scan()
        request_url = (
            "https://api.github.com/repos/public-owner/public-repo/pulls/7/reviews"
            "?per_page=2&page=1"
        )
        next_url = (
            "https://api.github.com/repositories/123456/pulls/7/reviews"
            "?per_page=2&page=2"
        )
        link_header = f'<{next_url}>; rel="next"'

        self.assertEqual(next_url, module._next_url_from_link(link_header, request_url))

    def test_link_header_preserves_opaque_github_pagination_query_and_required_semantics(self) -> None:
        module = load_surface_scan()
        request_url = (
            "https://api.github.com/repos/public-owner/public-repo/issues/comments"
            "?per_page=2&page=1&sort=created&direction=asc"
        )
        next_url = (
            "https://api.github.com/repositories/123456/issues/comments"
            "?per_page=2&after=opaque-pagination-token"
        )
        expected_url = f"{next_url}&sort=created&direction=asc"
        link_header = f'<{next_url}>; rel="next"'

        self.assertEqual(expected_url, module._next_url_from_link(link_header, request_url))

    def test_page_fetcher_preserves_required_semantics_when_link_omits_them(self) -> None:
        module = load_surface_scan()
        next_url = (
            "https://api.github.com/repositories/123456/issues"
            "?per_page=1&after=opaque-pagination-token"
        )
        expected_request_url = f"{next_url}&state=all&sort=created&direction=asc"
        responses = [
            FakeResponse(
                [{"number": 1, "title": "safe", "body": "safe"}],
                f'<{next_url}>; rel="next"',
            ),
            FakeResponse([], None),
        ]
        requested_urls: list[str] = []

        def fake_urlopen(request, timeout=30):
            requested_urls.append(request.full_url)
            return responses.pop(0)

        with mock.patch.object(module.urllib.request, "urlopen", side_effect=fake_urlopen):
            fetch_page = module.make_github_fetch_page("public-owner/public-repo", "synthetic-token")
            first = fetch_page("/issues", 1, 1)
            second = fetch_page("/issues", 2, 1)

        self.assertEqual(2, first.next_page)
        self.assertIsNone(second.next_page)
        self.assertEqual(expected_request_url, requested_urls[1])

    def test_link_header_rejects_changed_semantic_filters(self) -> None:
        module = load_surface_scan()
        request_url = (
            "https://api.github.com/repos/public-owner/public-repo/issues"
            "?per_page=2&page=1&state=all&sort=created&direction=asc"
        )
        invalid_headers = [
            '<https://api.github.com/repositories/123456/issues?page=2&state=closed>; rel="next"',
            '<https://api.github.com/repositories/123456/issues?page=2&sort=updated>; rel="next"',
            '<https://api.github.com/repositories/123456/issues?page=2&direction=desc>; rel="next"',
        ]

        for header in invalid_headers:
            with self.subTest(header=header):
                with self.assertRaises(module.SurfaceScanError) as caught:
                    module._next_url_from_link(header, request_url)
                self.assertEqual("api-pagination-ambiguous", caught.exception.error_class)

    def test_malformed_or_misdirected_link_header_fails_closed(self) -> None:
        module = load_surface_scan()
        request_url = (
            "https://api.github.com/repos/public-owner/public-repo/issues"
            "?per_page=2&page=1&state=all&sort=created&direction=asc"
        )
        invalid_headers = [
            "not-a-link",
            '<https://example.invalid/repos/public-owner/public-repo/issues?page=2>; rel="next"',
            '<https://api.github.com/repos/public-owner/public-repo/pulls?page=2>; rel="next"',
            '<https://api.github.com/repositories/123456/pulls?page=2>; rel="next"',
            '<https://api.github.com/repositories/not-numeric/issues?page=2>; rel="next"',
            '<https://api.github.com/repos/public-owner/public-repo/issues?page=2#fragment>; rel="next"',
            '<https://api.github.com/repos/public-owner/public-repo/issues?page=2>; rel="next", <https://api.github.com/repos/public-owner/public-repo/issues?page=3>; rel="next"',
        ]

        for header in invalid_headers:
            with self.subTest(header=header):
                with self.assertRaises(module.SurfaceScanError) as caught:
                    module._next_url_from_link(header, request_url)
                self.assertEqual("api-pagination-ambiguous", caught.exception.error_class)


if __name__ == "__main__":
    unittest.main()
