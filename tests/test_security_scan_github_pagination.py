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


if __name__ == "__main__":
    unittest.main()
