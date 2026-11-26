"""Tests for bounded and resilient online literature search adapters."""

from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlparse

from scholarflow.literature_search import (
    CrossrefLiteratureSearch,
    LiteratureSearchError,
    OpenAlexLiteratureSearch,
    ResilientLiteratureSearch,
)


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def read(self):
        return self.payload


class OpenAlexLiteratureSearchTest(unittest.TestCase):
    def test_provider_filters_and_pagination_are_sent_upstream(self):
        captured = []
        def opener(request, timeout=0):
            captured.append(parse_qs(urlparse(request.full_url).query))
            return _Response({"meta": {"count": 120}, "results": []})
        result = OpenAlexLiteratureSearch(opener=opener).search("research", page=3, author="Ada Scholar", year_from=2020, year_to=2025, oa_only=True)
        self.assertEqual(captured[0]["page"], ["3"])
        self.assertIn("publication_year:2020-2025", captured[0]["filter"][0])
        self.assertIn("raw_author_name.search:Ada Scholar", captured[0]["filter"][0])
        self.assertIn("open_access.is_oa:true", captured[0]["filter"][0])
        self.assertTrue(result.metadata["has_more"])
        self.assertEqual(result.metadata["total_matches"], 120)

    def test_search_normalizes_results_and_bounds_provider_query(self):
        captured = {}

        def opener(request, timeout=0):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return _Response({"results": [{
                "id": "https://openalex.org/W123",
                "doi": "https://doi.org/10.1000/Test",
                "title": "A governed research agent",
                "publication_year": 2026,
                "authorships": [{"author": {"display_name": "A. Researcher"}}],
                "primary_location": {
                    "landing_page_url": "https://doi.org/10.1000/Test",
                    "source": {"display_name": "Journal of Tests"},
                },
                "best_oa_location": {"pdf_url": "https://example.org/paper.pdf"},
                "open_access": {"is_oa": True, "oa_status": "gold"},
                "cited_by_count": 12,
                "abstract_inverted_index": {"Useful": [0], "evidence": [1]},
                "type": "article",
            }]})

        result = OpenAlexLiteratureSearch(opener=opener).search("  research   agent ", limit=50)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["doi"], "10.1000/test")
        self.assertEqual(result[0]["abstract"], "Useful evidence")
        self.assertEqual(result[0]["pdf_url"], "https://example.org/paper.pdf")
        self.assertEqual(result[0]["open_pdf_url"], "https://example.org/paper.pdf")
        query = parse_qs(urlparse(captured["url"]).query)
        self.assertEqual(query["search"], ["research agent"])
        self.assertEqual(query["per-page"], ["20"])

    def test_search_drops_unsafe_external_urls(self):
        def opener(request, timeout=0):  # noqa: ARG001
            return _Response({"results": [{
                "id": "https://openalex.org/W9",
                "title": "Unsafe PDF fixture",
                "authorships": [],
                "primary_location": {"landing_page_url": "file:///private/paper.pdf"},
                "best_oa_location": {"pdf_url": "http://example.org/paper.pdf"},
                "open_access": {"is_oa": False},
            }]})

        result = OpenAlexLiteratureSearch(opener=opener).search("unsafe fixture")

        self.assertEqual(result[0]["primary_url"], "https://openalex.org/W9")
        self.assertEqual(result[0]["pdf_url"], "")

    def test_search_rejects_empty_query_before_network(self):
        with self.assertRaises(LiteratureSearchError) as caught:
            OpenAlexLiteratureSearch().search(" ")
        self.assertEqual(caught.exception.code, "invalid_argument")

    def test_work_level_open_access_does_not_imply_publisher_pdf_access(self):
        def opener(request, timeout=0):
            return _Response({"results": [{
                "id": "https://openalex.org/W500", "title": "Synthetic work",
                "primary_location": {"is_oa": False, "pdf_url": "https://example.org/closed.pdf"},
                "best_oa_location": {"is_oa": True, "landing_page_url": "https://example.org/record"},
                "open_access": {"is_oa": True},
            }]})
        result = OpenAlexLiteratureSearch(opener=opener).search("synthetic work")
        self.assertEqual(result[0]["open_pdf_url"], "")

    def test_search_uses_verified_open_pdf_from_other_locations(self):
        captured = {}
        def opener(request, timeout=0):
            captured["url"] = request.full_url
            return _Response({"results": [{
                "id": "https://openalex.org/W501", "title": "Open repository copy",
                "primary_location": {"is_oa": False, "pdf_url": "https://example.org/closed.pdf"},
                "best_oa_location": {"is_oa": True, "pdf_url": None},
                "locations": [None, {"is_oa": False, "pdf_url": "https://example.org/closed.pdf"},
                              {"is_oa": True, "pdf_url": "http://example.org/insecure.pdf"},
                              {"is_oa": True, "pdf_url": "https://example.org/repository.pdf"}],
            }]})
        result = OpenAlexLiteratureSearch(opener=opener).search("open repository")
        self.assertIn("locations", parse_qs(urlparse(captured["url"]).query)["select"][0].split(","))
        self.assertEqual(result[0]["open_pdf_url"], "https://example.org/repository.pdf")

    def test_unverified_other_locations_are_never_treated_as_open(self):
        def opener(request, timeout=0):
            return _Response({"results": [{
                "id": "https://openalex.org/W502", "title": "Unverified repository copy",
                "locations": [{"pdf_url": "https://example.org/unknown.pdf"},
                              {"is_oa": False, "pdf_url": "https://example.org/closed.pdf"}],
                "open_access": {"is_oa": True},
            }]})
        result = OpenAlexLiteratureSearch(opener=opener).search("unverified repository")
        self.assertEqual(result[0]["open_pdf_url"], "")


class CrossrefLiteratureSearchTest(unittest.TestCase):
    def test_fallback_cannot_claim_full_open_access_filter(self):
        with self.assertRaises(LiteratureSearchError) as caught:
            CrossrefLiteratureSearch().search("research", oa_only=True)
        self.assertEqual(caught.exception.code, "literature_filter_unsupported")

    def test_search_normalizes_crossref_result(self):
        def opener(request, timeout=0):  # noqa: ARG001
            return _Response({"message": {"items": [{
                "DOI": "10.1000/Crossref",
                "URL": "https://doi.org/10.1000/Crossref",
                "title": ["Fallback literature result"],
                "author": [{"given": "Ada", "family": "Scholar"}],
                "published-online": {"date-parts": [[2025, 4, 2]]},
                "container-title": ["Journal of Resilience"],
                "is-referenced-by-count": 7,
                "abstract": "<jats:p>Bounded &amp; readable.</jats:p>",
                "type": "journal-article",
                "link": [{
                    "URL": "https://example.org/fallback.pdf",
                    "content-type": "application/pdf",
                }],
            }]}})

        result = CrossrefLiteratureSearch(opener=opener).search("fallback query")

        self.assertEqual(result[0]["provider"], "crossref")
        self.assertEqual(result[0]["result_id"], "10.1000/crossref")
        self.assertEqual(result[0]["authors"], ["Ada Scholar"])
        self.assertEqual(result[0]["year"], 2025)
        self.assertEqual(result[0]["abstract"], "Bounded & readable.")
        self.assertEqual(result[0]["pdf_url"], "https://example.org/fallback.pdf")

    def test_resilient_search_falls_back_and_caches_result(self):
        class Unavailable:
            calls = 0

            def search(self, query, *, limit=12):  # noqa: ARG002
                self.calls += 1
                raise LiteratureSearchError("literature_search_rate_limited", "limited")

        class Fallback:
            calls = 0

            def search(self, query, *, limit=12):
                self.calls += 1
                return [{"provider": "crossref", "result_id": query, "title": query}]

        primary = Unavailable()
        fallback = Fallback()
        search = ResilientLiteratureSearch(primary=primary, fallback=fallback, clock=lambda: 100.0)

        first = search.search("same query", limit=5)
        first[0]["title"] = "mutated"
        second = search.search("same query", limit=5)

        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(second[0]["title"], "same query")


if __name__ == "__main__":
    unittest.main()
