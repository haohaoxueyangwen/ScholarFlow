"""Bounded, read-only scholarly search adapters for the Workbench."""

from __future__ import annotations

import json
import os
import copy
import html
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
import urllib.error
import urllib.parse
import urllib.request


class LiteratureSearchError(RuntimeError):
    """Raised when an external literature provider cannot satisfy a search."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LiteratureBatch(list):
    """List-compatible result with per-request pagination provenance."""
    def __init__(self, items, *, provider: str, page: int, limit: int, total: Any, raw_count: int, filters: dict):
        super().__init__(items)
        total_count = int(total) if isinstance(total, (int, float)) and total >= 0 else None
        self.metadata = {"provider": provider, "page": page, "page_size": limit, "total_matches": total_count, "has_more": page * limit < total_count if total_count is not None else raw_count == limit, "complete_snapshot": False, "filter_scope": "provider_query", "filters": filters, "author_matching": "byline_text_not_disambiguated_identity"}


def _search_filters(options: dict) -> dict:
    page = int(options.get("page") or 1)
    minimum, maximum = int(options.get("year_from") or 0), int(options.get("year_to") or 0)
    author = " ".join(str(options.get("author") or "").split())
    if not 1 <= page <= 500 or any(year and not 1000 <= year <= 2100 for year in (minimum, maximum)) or (minimum and maximum and minimum > maximum) or len(author) > 200 or any(char in author for char in ",|:"):
        raise LiteratureSearchError("invalid_argument", "invalid literature page, author text, or year range")
    return {"page": page, "year_from": minimum, "year_to": maximum, "author": author, "oa_only": bool(options.get("oa_only"))}


@dataclass
class OpenAlexLiteratureSearch:
    """Minimal OpenAlex search client with a stable ScholarFlow result shape."""

    endpoint: str = "https://api.openalex.org/works"
    timeout_seconds: float = 12.0
    opener: Callable[..., Any] | None = None

    def search(self, query: str, *, limit: int = 12, **options) -> list[dict[str, Any]]:
        normalized_query = " ".join(str(query or "").split())
        if len(normalized_query) < 2:
            raise LiteratureSearchError(
                "invalid_argument",
                "literature search requires at least 2 characters",
            )
        if len(normalized_query) > 400:
            raise LiteratureSearchError(
                "invalid_argument",
                "literature search exceeds the 400 character limit",
            )
        bounded_limit = max(1, min(int(limit), 20))
        params = {
            "search": normalized_query,
            "per-page": str(bounded_limit),
            "select": ",".join(
                [
                    "id",
                    "doi",
                    "title",
                    "publication_year",
                    "authorships",
                    "primary_location",
                    "best_oa_location",
                    "locations",
                    "open_access",
                    "cited_by_count",
                    "abstract_inverted_index",
                    "type",
                ]
            ),
        }
        filters = _search_filters(options)
        params["page"] = str(filters["page"])
        provider_filters = []
        if filters["year_from"] or filters["year_to"]:
            provider_filters.append(f"publication_year:{filters['year_from'] or 1000}-{filters['year_to'] or 2100}")
        if filters["author"]:
            provider_filters.append(f"raw_author_name.search:{filters['author']}")
        if filters["oa_only"]:
            provider_filters.append("open_access.is_oa:true")
        if provider_filters:
            params["filter"] = ",".join(provider_filters)
        api_key = str(os.environ.get("OPENALEX_API_KEY") or "").strip()
        if api_key:
            params["api_key"] = api_key
        url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "ScholarFlow/1.0 (local research workbench)",
            },
        )
        opener = self.opener or urllib.request.urlopen
        try:
            with opener(request, timeout=self.timeout_seconds) as response:  # type: ignore[call-arg]
                raw = response.read()
        except urllib.error.HTTPError as exc:
            code = "literature_search_rate_limited" if exc.code == 429 else "literature_search_unavailable"
            raise LiteratureSearchError(code, "OpenAlex literature search is temporarily unavailable") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LiteratureSearchError(
                "literature_search_unavailable",
                "OpenAlex literature search is temporarily unavailable",
            ) from exc

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LiteratureSearchError(
                "literature_search_invalid_response",
                "OpenAlex returned an unreadable response",
            ) from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
            raise LiteratureSearchError(
                "literature_search_invalid_response",
                "OpenAlex returned an unsupported response",
            )
        return LiteratureBatch([
            normalized
            for item in payload["results"][:bounded_limit]
            if isinstance(item, Mapping)
            for normalized in [_normalize_openalex_work(item)]
            if normalized is not None
        ], provider="openalex", page=filters["page"], limit=bounded_limit, total=(payload.get("meta") or {}).get("count"), raw_count=len(payload["results"]), filters=filters)


def _normalize_openalex_work(work: Mapping[str, Any]) -> dict[str, Any] | None:
    title = _bounded_text(work.get("title"), 500)
    openalex_url = _https_url(work.get("id"))
    if not title or not openalex_url:
        return None
    authors: list[str] = []
    for authorship in work.get("authorships") or []:
        if not isinstance(authorship, Mapping):
            continue
        author = authorship.get("author")
        if not isinstance(author, Mapping):
            continue
        display_name = _bounded_text(author.get("display_name"), 160)
        if display_name and display_name not in authors:
            authors.append(display_name)
        if len(authors) >= 8:
            break
    primary_location = work.get("primary_location")
    primary = primary_location if isinstance(primary_location, Mapping) else {}
    source = primary.get("source")
    source_map = source if isinstance(source, Mapping) else {}
    best_oa_location = work.get("best_oa_location")
    best_oa = best_oa_location if isinstance(best_oa_location, Mapping) else {}
    open_access = work.get("open_access")
    oa_map = open_access if isinstance(open_access, Mapping) else {}
    doi = _normalize_doi(work.get("doi"))
    primary_url = _https_url(primary.get("landing_page_url")) or (
        f"https://doi.org/{doi}" if doi else openalex_url
    )
    # OpenAlex's preferred OA location can be a landing page even when another
    # location supplies an open PDF.  Never infer a location's access from the
    # work-level OA flag, and keep the remote fetcher's normal SSRF checks.
    locations = work.get("locations")
    other_open_pdf = next((
        url for location in (locations[:100] if isinstance(locations, list) else [])
        if isinstance(location, Mapping) and location.get("is_oa") is True
        for url in [_https_url(location.get("pdf_url"))] if url
    ), "")
    open_pdf_url = (_https_url(best_oa.get("pdf_url")) if best_oa.get("is_oa") is not False else "") \
        or (_https_url(primary.get("pdf_url")) if primary.get("is_oa") is True else "") \
        or other_open_pdf
    pdf_url = _https_url(best_oa.get("pdf_url")) or _https_url(primary.get("pdf_url")) or open_pdf_url
    return {
        "provider": "openalex",
        "result_id": openalex_url.rsplit("/", 1)[-1],
        "provider_url": openalex_url,
        "openalex_id": openalex_url.rsplit("/", 1)[-1],
        "openalex_url": openalex_url,
        "title": title,
        "authors": authors,
        "year": _integer(work.get("publication_year")),
        "venue": _bounded_text(source_map.get("display_name"), 240),
        "doi": doi,
        "type": _bounded_text(work.get("type"), 80),
        "cited_by_count": max(0, _integer(work.get("cited_by_count")) or 0),
        "abstract": _reconstruct_abstract(work.get("abstract_inverted_index")),
        "open_access": {
            "is_oa": bool(oa_map.get("is_oa")),
            "status": _bounded_text(oa_map.get("oa_status"), 40),
        },
        "primary_url": primary_url,
        "pdf_url": pdf_url,
        # A work-level OA flag is not permission to fetch its closed publisher PDF.
        "open_pdf_url": open_pdf_url,
    }


@dataclass
class CrossrefLiteratureSearch:
    """Crossref fallback with the same bounded result contract as OpenAlex."""

    endpoint: str = "https://api.crossref.org/works"
    timeout_seconds: float = 12.0
    opener: Callable[..., Any] | None = None

    def search(self, query: str, *, limit: int = 12, **options) -> list[dict[str, Any]]:
        normalized_query, bounded_limit = _bounded_search_input(query, limit)
        filters = _search_filters(options)
        if filters["oa_only"]:
            raise LiteratureSearchError("literature_filter_unsupported", "Crossref cannot reliably filter open access; use OpenAlex or disable that filter")
        fields = [
            "DOI",
            "URL",
            "title",
            "author",
            "published-print",
            "published-online",
            "container-title",
            "is-referenced-by-count",
            "abstract",
            "type",
            "link",
            "license",
        ]
        params = {
            "query.bibliographic": normalized_query,
            "rows": str(bounded_limit),
            "select": ",".join(fields),
        }
        params["offset"] = str((filters["page"] - 1) * bounded_limit)
        if filters["author"]:
            params["query.author"] = filters["author"]
        date_filters = []
        if filters["year_from"]:
            date_filters.append(f"from-pub-date:{filters['year_from']}-01-01")
        if filters["year_to"]:
            date_filters.append(f"until-pub-date:{filters['year_to']}-12-31")
        if date_filters:
            params["filter"] = ",".join(date_filters)
        mailto = str(os.environ.get("CROSSREF_MAILTO") or "").strip()
        if mailto:
            params["mailto"] = mailto
        url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "ScholarFlow/1.0 (local research workbench)",
            },
        )
        opener = self.opener or urllib.request.urlopen
        try:
            with opener(request, timeout=self.timeout_seconds) as response:  # type: ignore[call-arg]
                raw = response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LiteratureSearchError(
                "literature_search_unavailable",
                "Crossref literature search is temporarily unavailable",
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LiteratureSearchError(
                "literature_search_invalid_response",
                "Crossref returned an unreadable response",
            ) from exc
        message = payload.get("message") if isinstance(payload, Mapping) else None
        items = message.get("items") if isinstance(message, Mapping) else None
        if not isinstance(items, list):
            raise LiteratureSearchError(
                "literature_search_invalid_response",
                "Crossref returned an unsupported response",
            )
        return LiteratureBatch([
            normalized
            for item in items[:bounded_limit]
            if isinstance(item, Mapping)
            for normalized in [_normalize_crossref_work(item)]
            if normalized is not None
        ], provider="crossref", page=filters["page"], limit=bounded_limit, total=message.get("total-results"), raw_count=len(items), filters=filters)


@dataclass
class ResilientLiteratureSearch:
    """Cache searches and fall back to Crossref when OpenAlex is unavailable."""

    primary: Any = field(default_factory=OpenAlexLiteratureSearch)
    fallback: Any = field(default_factory=CrossrefLiteratureSearch)
    cache_ttl_seconds: float = 600.0
    clock: Callable[[], float] = time.monotonic
    _cache: dict[tuple, tuple[float, list[dict[str, Any]]]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def search(self, query: str, *, limit: int = 12, **options) -> list[dict[str, Any]]:
        normalized_query, bounded_limit = _bounded_search_input(query, limit)
        provider = str(options.pop("provider", "") or "")
        if provider not in {"", "openalex", "crossref"}:
            raise LiteratureSearchError("invalid_argument", "unsupported literature provider")
        key = (normalized_query.casefold(), bounded_limit, provider, json.dumps(options, sort_keys=True))
        now = self.clock()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] <= self.cache_ttl_seconds:
            return copy.deepcopy(cached[1])
        try:
            items = (self.fallback if provider == "crossref" else self.primary).search(normalized_query, limit=bounded_limit, **options)
        except LiteratureSearchError as primary_error:
            if provider or primary_error.code == "invalid_argument":
                raise
            try:
                items = self.fallback.search(normalized_query, limit=bounded_limit, **options)
            except LiteratureSearchError as fallback_error:
                if fallback_error.code == "literature_filter_unsupported":
                    raise
                raise LiteratureSearchError(
                    fallback_error.code,
                    "Online literature providers are temporarily unavailable",
                ) from primary_error
        self._cache[key] = (now, copy.deepcopy(items))
        if len(self._cache) > 100:
            self._cache.pop(min(self._cache, key=lambda item: self._cache[item][0]))
        return items


def _normalize_crossref_work(work: Mapping[str, Any]) -> dict[str, Any] | None:
    doi = _normalize_doi(work.get("DOI"))
    title = _first_bounded(work.get("title"), 500)
    primary_url = _https_url(work.get("URL")) or (f"https://doi.org/{doi}" if doi else "")
    if not title or not primary_url:
        return None
    authors: list[str] = []
    for author in work.get("author") or []:
        if not isinstance(author, Mapping):
            continue
        name = " ".join(
            part for part in (
                _bounded_text(author.get("given"), 80),
                _bounded_text(author.get("family"), 100),
            ) if part
        )
        if name and name not in authors:
            authors.append(name)
        if len(authors) >= 8:
            break
    pdf_url = ""
    for link in work.get("link") or []:
        if not isinstance(link, Mapping):
            continue
        candidate = _https_url(link.get("URL"))
        content_type = _bounded_text(link.get("content-type"), 80).casefold()
        if candidate and ("pdf" in content_type or candidate.casefold().endswith(".pdf")):
            pdf_url = candidate
            break
    venue = _first_bounded(work.get("container-title"), 240)
    result_id = doi or primary_url
    return {
        "provider": "crossref",
        "result_id": result_id,
        "provider_url": primary_url,
        "title": title,
        "authors": authors,
        "year": _crossref_year(work),
        "venue": venue,
        "doi": doi,
        "type": _bounded_text(work.get("type"), 80),
        "cited_by_count": max(0, _integer(work.get("is-referenced-by-count")) or 0),
        "abstract": _strip_markup(work.get("abstract"), 4000),
        "open_access": {
            "is_oa": False,
            "status": "unknown",
        },
        "primary_url": primary_url,
        "pdf_url": pdf_url,
    }


def _bounded_search_input(query: str, limit: int) -> tuple[str, int]:
    normalized_query = " ".join(str(query or "").split())
    if len(normalized_query) < 2:
        raise LiteratureSearchError("invalid_argument", "literature search requires at least 2 characters")
    if len(normalized_query) > 400:
        raise LiteratureSearchError("invalid_argument", "literature search exceeds the 400 character limit")
    return normalized_query, max(1, min(int(limit), 20))


def _first_bounded(value: Any, limit: int) -> str:
    if isinstance(value, list):
        return _bounded_text(value[0], limit) if value else ""
    return _bounded_text(value, limit)


def _crossref_year(work: Mapping[str, Any]) -> int | None:
    for field_name in ("published-print", "published-online"):
        published = work.get(field_name)
        date_parts = published.get("date-parts") if isinstance(published, Mapping) else None
        if isinstance(date_parts, list) and date_parts and isinstance(date_parts[0], list) and date_parts[0]:
            year = _integer(date_parts[0][0])
            if year is not None:
                return year
    return None


def _strip_markup(value: Any, limit: int) -> str:
    raw = _bounded_text(value, limit * 2)
    return _bounded_text(html.unescape(re.sub(r"<[^>]+>", " ", raw)), limit)


def _reconstruct_abstract(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    positioned: list[tuple[int, str]] = []
    for raw_word, raw_positions in list(value.items())[:1200]:
        word = _bounded_text(raw_word, 80)
        if not word or not isinstance(raw_positions, list):
            continue
        for position in raw_positions[:40]:
            if isinstance(position, int) and 0 <= position <= 20_000:
                positioned.append((position, word))
                if len(positioned) >= 600:
                    break
        if len(positioned) >= 600:
            break
    positioned.sort(key=lambda item: item[0])
    return " ".join(word for _, word in positioned)[:4000]


def _normalize_doi(value: Any) -> str:
    normalized = _bounded_text(value, 300).strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.casefold().startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    return normalized.strip().casefold()


def _https_url(value: Any) -> str:
    normalized = _bounded_text(value, 2000)
    try:
        parsed = urllib.parse.urlparse(normalized)
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return normalized


def _bounded_text(value: Any, limit: int) -> str:
    normalized = (
        str(value or "")
        .replace("\\r", " ")
        .replace("\\n", " ")
        .replace("\\t", " ")
    )
    return " ".join(normalized.split())[:limit]


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
