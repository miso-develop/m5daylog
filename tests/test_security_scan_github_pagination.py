from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "github_surface_scan.py"


def load_surface_scan():
    spec = importlib.util.spec_from_file_location("github_surface_scan_pagination", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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

    def test_link_header_next_relation_is_bound_to_expected_request(self) -> None:
        module = load_surface_scan()
        request_url = (
            "https://api.github.com/repos/public-owner/public-repo/issues"
            "?per_page=2&page=1&state=all&sort=created&direction=asc"
        )
        link_header = (
            '<https://api.github.com/repos/public-owner/public-repo/issues?per_page=2&page=2&state=all&sort=created&direction=asc>; rel="next", '
            '<https://api.github.com/repos/public-owner/public-repo/issues?per_page=2&page=4&state=all&sort=created&direction=asc>; rel="last"'
        )

        self.assertEqual(2, module._next_page_from_link(link_header, request_url, 1))

    def test_link_header_accepts_github_canonical_repository_path(self) -> None:
        module = load_surface_scan()
        request_url = (
            "https://api.github.com/repos/public-owner/public-repo/issues"
            "?per_page=2&page=1&state=all&sort=created&direction=asc"
        )
        link_header = (
            '<https://api.github.com/repositories/123456/issues?per_page=2&page=2&state=all&sort=created&direction=asc>; rel="next", '
            '<https://api.github.com/repositories/123456/issues?per_page=2&page=4&state=all&sort=created&direction=asc>; rel="last"'
        )

        self.assertEqual(2, module._next_page_from_link(link_header, request_url, 1))

    def test_link_header_accepts_canonical_nested_review_resource(self) -> None:
        module = load_surface_scan()
        request_url = (
            "https://api.github.com/repos/public-owner/public-repo/pulls/7/reviews"
            "?per_page=2&page=1"
        )
        link_header = (
            '<https://api.github.com/repositories/123456/pulls/7/reviews?per_page=2&page=2>; rel="next"'
        )

        self.assertEqual(2, module._next_page_from_link(link_header, request_url, 1))

    def test_link_header_accepts_navigation_only_query_from_github(self) -> None:
        module = load_surface_scan()
        request_url = (
            "https://api.github.com/repos/public-owner/public-repo/issues"
            "?per_page=2&page=1&state=all&sort=created&direction=asc"
        )
        link_header = (
            '<https://api.github.com/repositories/123456/issues?page=2>; rel="next", '
            '<https://api.github.com/repositories/123456/issues?page=4>; rel="last"'
        )

        self.assertEqual(2, module._next_page_from_link(link_header, request_url, 1))

    def test_link_header_rejects_changed_or_unexpected_query_values(self) -> None:
        module = load_surface_scan()
        request_url = (
            "https://api.github.com/repos/public-owner/public-repo/issues"
            "?per_page=2&page=1&state=all&sort=created&direction=asc"
        )
        invalid_headers = [
            '<https://api.github.com/repositories/123456/issues?page=2&state=closed>; rel="next"',
            '<https://api.github.com/repositories/123456/issues?page=2&unexpected=value>; rel="next"',
        ]

        for header in invalid_headers:
            with self.subTest(header=header):
                with self.assertRaises(module.SurfaceScanError) as caught:
                    module._next_page_from_link(header, request_url, 1)
                self.assertEqual("api-pagination-ambiguous", caught.exception.error_class)

    def test_malformed_or_misdirected_link_header_fails_closed(self) -> None:
        module = load_surface_scan()
        request_url = (
            "https://api.github.com/repos/public-owner/public-repo/issues"
            "?per_page=2&page=1&state=all&sort=created&direction=asc"
        )
        invalid_headers = [
            "not-a-link",
            '<https://api.github.com/repos/public-owner/public-repo/pulls?per_page=2&page=2&state=all&sort=created&direction=asc>; rel="next"',
            '<https://api.github.com/repositories/123456/pulls?per_page=2&page=2&state=all&sort=created&direction=asc>; rel="next"',
            '<https://api.github.com/repositories/not-numeric/issues?per_page=2&page=2&state=all&sort=created&direction=asc>; rel="next"',
            '<https://api.github.com/repos/public-owner/public-repo/issues?per_page=2&page=3&state=all&sort=created&direction=asc>; rel="next"',
        ]

        for header in invalid_headers:
            with self.subTest(header=header):
                with self.assertRaises(module.SurfaceScanError) as caught:
                    module._next_page_from_link(header, request_url, 1)
                self.assertEqual("api-pagination-ambiguous", caught.exception.error_class)


if __name__ == "__main__":
    unittest.main()
