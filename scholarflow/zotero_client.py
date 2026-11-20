"""Zotero client adapters used by the paper service.

This module deliberately avoids third-party dependencies (no `requests` /
`pyzotero`) so that ScholarFlow can run in a minimal Python environment.
The clients are intentionally thin and return plain dictionaries so the
caller can decide how to normalize the payload.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol


class ZoteroClientError(RuntimeError):
    """Raised when a Zotero endpoint cannot satisfy a request."""


class ZoteroClientUnavailable(ZoteroClientError):
    """Raised when the configured Zotero endpoint is not reachable."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ZoteroClientResult:
    """Standardized Zotero response envelope returned to callers."""

    status: str
    backend: str
    data: Any = None
    error: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "backend": self.backend,
            "data": self.data,
            "error": self.error,
            "extra": dict(self.extra),
        }


class ZoteroClient(Protocol):
    backend: str

    def list_collections(self) -> ZoteroClientResult: ...

    def list_items(
        self,
        *,
        collection_key: Optional[str] = None,
        limit: int = 25,
    ) -> ZoteroClientResult: ...

    def get_item(self, item_key: str) -> ZoteroClientResult: ...

    def create_item(self, item_payload: dict[str, Any]) -> ZoteroClientResult: ...

    def add_tags(self, item_key: str, tags: list[str]) -> ZoteroClientResult: ...

    def update_tags(
        self,
        item_key: str,
        tags: list[dict[str, Any]],
        *,
        version: int | None = None,
    ) -> ZoteroClientResult: ...


class NullZoteroClient:
    """A no-op Zotero client used when no real endpoint is configured."""

    backend = "null"

    def __init__(self, *, reason: str = "no_zotero_endpoint_configured") -> None:
        self._reason = reason

    def list_collections(self) -> ZoteroClientResult:
        return ZoteroClientResult(
            status="unavailable",
            backend=self.backend,
            data=[],
            error=self._reason,
        )

    def list_items(
        self,
        *,
        collection_key: Optional[str] = None,
        limit: int = 25,
    ) -> ZoteroClientResult:
        return ZoteroClientResult(
            status="unavailable",
            backend=self.backend,
            data=[],
            error=self._reason,
            extra={"collection_key": collection_key, "limit": limit},
        )

    def get_item(self, item_key: str) -> ZoteroClientResult:
        return ZoteroClientResult(
            status="unavailable",
            backend=self.backend,
            data=None,
            error=self._reason,
            extra={"item_key": item_key},
        )

    def create_item(self, item_payload: dict[str, Any]) -> ZoteroClientResult:
        return ZoteroClientResult(
            status="unavailable",
            backend=self.backend,
            data=None,
            error=self._reason,
            extra={"payload_keys": sorted(item_payload.keys())},
        )

    def add_tags(self, item_key: str, tags: list[str]) -> ZoteroClientResult:
        return ZoteroClientResult(
            status="unavailable",
            backend=self.backend,
            data=None,
            error=self._reason,
            extra={"item_key": item_key, "tags": list(tags)},
        )

    def update_tags(
        self,
        item_key: str,
        tags: list[dict[str, Any]],
        *,
        version: int | None = None,
    ) -> ZoteroClientResult:
        return ZoteroClientResult(
            status="unavailable",
            backend=self.backend,
            data=None,
            error=self._reason,
            extra={"item_key": item_key, "version": version, "tags": list(tags)},
        )


@dataclass
class ZoteroWebClient:
    """Zotero Web API v3 client.

    Uses only `urllib.request` so the dependency footprint stays minimal.
    Credentials default to ``ZOTERO_USER_ID`` / ``ZOTERO_API_KEY`` environment
    variables but can be overridden through the constructor.
    """

    user_id: str
    api_key: str
    base_url: str = "https://api.zotero.org"
    api_version: int = 3
    timeout_seconds: float = 10.0
    opener: Optional[Callable[..., Any]] = None
    backend: str = "web"

    def list_collections(self) -> ZoteroClientResult:
        url = self._url(f"/users/{self.user_id}/collections")
        try:
            payload = self._request_json("GET", url)
        except ZoteroClientUnavailable as exc:
            return ZoteroClientResult(status="unavailable", backend=self.backend, data=[], error=str(exc))
        return ZoteroClientResult(status="ok", backend=self.backend, data=payload)

    def list_items(
        self,
        *,
        collection_key: Optional[str] = None,
        limit: int = 25,
    ) -> ZoteroClientResult:
        if collection_key:
            url = self._url(f"/users/{self.user_id}/collections/{collection_key}/items")
        else:
            url = self._url(f"/users/{self.user_id}/items")
        url = self._append_query(url, {"limit": max(1, int(limit)), "format": "json"})
        try:
            payload = self._request_json("GET", url)
        except ZoteroClientUnavailable as exc:
            return ZoteroClientResult(
                status="unavailable",
                backend=self.backend,
                data=[],
                error=str(exc),
                extra={"collection_key": collection_key, "limit": limit},
            )
        if not isinstance(payload, list):
            return ZoteroClientResult(
                status="unexpected_payload",
                backend=self.backend,
                data=[],
                error="Zotero items endpoint did not return a JSON array",
            )
        return ZoteroClientResult(
            status="ok",
            backend=self.backend,
            data=payload,
            extra={"count": len(payload), "collection_key": collection_key, "limit": limit},
        )

    def get_item(self, item_key: str) -> ZoteroClientResult:
        url = self._url(f"/users/{self.user_id}/items/{item_key}")
        try:
            payload = self._request_json("GET", url)
        except ZoteroClientUnavailable as exc:
            return ZoteroClientResult(
                status="unavailable",
                backend=self.backend,
                data=None,
                error=str(exc),
                extra={"item_key": item_key},
            )
        return ZoteroClientResult(
            status="ok",
            backend=self.backend,
            data=payload,
            extra={"item_key": item_key, "version": _item_version(payload)},
        )

    def create_item(self, item_payload: dict[str, Any]) -> ZoteroClientResult:
        if not isinstance(item_payload, dict):
            raise ZoteroClientError("create_item requires a dict payload")
        url = self._url(f"/users/{self.user_id}/items")
        try:
            payload = self._request_json(
                "POST",
                url,
                body=[item_payload],
                extra_headers={"Content-Type": "application/json"},
            )
        except ZoteroClientUnavailable as exc:
            return ZoteroClientResult(
                status="unavailable",
                backend=self.backend,
                data=None,
                error=str(exc),
            )
        created = None
        if isinstance(payload, dict):
            successful = payload.get("successful") or {}
            if isinstance(successful, dict) and successful:
                first_key = next(iter(successful.keys()))
                created = successful[first_key]
        return ZoteroClientResult(
            status="ok",
            backend=self.backend,
            data=created,
            extra={"raw": payload} if isinstance(payload, dict) else {},
        )

    def add_tags(self, item_key: str, tags: list[str]) -> ZoteroClientResult:
        cleaned = [str(tag).strip() for tag in tags if str(tag).strip()]
        if not cleaned:
            return ZoteroClientResult(
                status="noop",
                backend=self.backend,
                data=None,
                error="no tags provided",
            )
        return self.update_tags(item_key, [{"tag": tag} for tag in cleaned])

    def update_tags(
        self,
        item_key: str,
        tags: list[dict[str, Any]],
        *,
        version: int | None = None,
    ) -> ZoteroClientResult:
        cleaned = _normalize_tag_objects(tags)
        if not cleaned:
            return ZoteroClientResult(
                status="noop",
                backend=self.backend,
                data=None,
                error="no tags provided",
                extra={"item_key": item_key, "version": version, "tags": []},
            )
        url = self._url(f"/users/{self.user_id}/items/{item_key}")
        headers = {"Content-Type": "application/json"}
        if version is not None:
            headers["If-Unmodified-Since-Version"] = str(version)
        try:
            payload = self._request_json(
                "PATCH",
                url,
                body={"tags": cleaned},
                extra_headers=headers,
            )
        except ZoteroClientUnavailable as exc:
            status = "stale" if exc.status_code == 412 else "conflict" if exc.status_code == 409 else "unavailable"
            return ZoteroClientResult(
                status=status,
                backend=self.backend,
                data=None,
                error=str(exc),
                extra={"item_key": item_key, "version": version, "tags": cleaned, "status_code": exc.status_code},
            )
        return ZoteroClientResult(
            status="ok",
            backend=self.backend,
            data=payload,
            extra={"item_key": item_key, "version": version, "tags": cleaned, "version_after": _item_version(payload)},
        )

    def _url(self, path: str) -> str:
        base = self.base_url.rstrip("/")
        if not path.startswith("/"):
            path = "/" + path
        return f"{base}{path}"

    @staticmethod
    def _append_query(url: str, params: dict[str, Any]) -> str:
        if not params:
            return url
        encoded = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{encoded}"

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        body: Any = None,
        extra_headers: Optional[dict[str, str]] = None,
        response_metadata: Optional[dict[str, str]] = None,
    ) -> Any:
        data: Optional[bytes] = None
        headers: dict[str, str] = {
            "User-Agent": "ScholarFlow/0.1 (+zotero-web-client)",
            "Zotero-API-Key": self.api_key,
            "Zotero-API-Version": str(self.api_version),
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            elif isinstance(body, str):
                data = body.encode("utf-8")
            elif isinstance(body, (bytes, bytearray)):
                data = bytes(body)
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=headers,
        )
        opener_factory = self.opener or urllib.request.urlopen
        try:
            with opener_factory(request, timeout=self.timeout_seconds) as response:  # type: ignore[call-arg]
                raw = response.read()
                if response_metadata is not None:
                    response_headers = getattr(response, "headers", {})
                    for name in ("Last-Modified-Version", "Total-Results"):
                        value = response_headers.get(name)
                        if value is not None:
                            response_metadata[name] = str(value)
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            raise ZoteroClientUnavailable(
                f"Zotero Web API returned HTTP {exc.code} for {method} {url}",
                status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network path
            raise ZoteroClientUnavailable(
                f"Zotero Web API unreachable for {method} {url}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:  # pragma: no cover - network path
            raise ZoteroClientUnavailable(
                f"Zotero Web API timeout for {method} {url}"
            ) from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ZoteroClientError(
                f"Zotero Web API returned non-JSON payload for {method} {url}: {exc.msg}"
            ) from exc


@dataclass
class ZoteroLocalClient:
    """Adapter for the local Zotero Connector HTTP API."""

    base_url: str = "http://127.0.0.1:23119"
    timeout_seconds: float = 2.0
    opener: Optional[Callable[..., Any]] = None
    backend: str = "local"

    def list_collections(self) -> ZoteroClientResult:
        try:
            payload = self._request_json("GET", self._url("/api/users/0/collections"))
        except ZoteroClientUnavailable as exc:
            return ZoteroClientResult(status="unavailable", backend=self.backend, data=[], error=str(exc))
        return ZoteroClientResult(status="ok", backend=self.backend, data=payload or [])

    def list_items(
        self,
        *,
        collection_key: Optional[str] = None,
        limit: int = 25,
    ) -> ZoteroClientResult:
        requested_limit = max(1, int(limit))
        path = "/api/users/0/items"
        if collection_key:
            path = f"/api/users/0/collections/{collection_key}/items"
        try:
            url = f"{self._url(path)}?{urllib.parse.urlencode({'limit': requested_limit})}"
            payload = self._request_json("GET", url)
        except ZoteroClientUnavailable as exc:
            return ZoteroClientResult(
                status="unavailable",
                backend=self.backend,
                data=[],
                error=str(exc),
                extra={"collection_key": collection_key, "limit": limit},
            )
        if not isinstance(payload, list):
            return ZoteroClientResult(
                status="unexpected_payload",
                backend=self.backend,
                data=[],
                error="Zotero local endpoint did not return a JSON array",
            )
        truncated = payload[:requested_limit]
        return ZoteroClientResult(
            status="ok",
            backend=self.backend,
            data=truncated,
            extra={"count": len(truncated), "collection_key": collection_key, "limit": requested_limit},
        )

    def get_item(self, item_key: str) -> ZoteroClientResult:
        try:
            payload = self._request_json("GET", self._url(f"/api/users/0/items/{item_key}"))
        except ZoteroClientUnavailable as exc:
            return ZoteroClientResult(
                status="unavailable",
                backend=self.backend,
                data=None,
                error=str(exc),
                extra={"item_key": item_key},
            )
        return ZoteroClientResult(
            status="ok", backend=self.backend, data=payload, extra={"item_key": item_key}
        )

    def list_children(self, item_key: str) -> ZoteroClientResult:
        """Return child attachments for one local Zotero parent item."""

        encoded_key = urllib.parse.quote(str(item_key or "").strip(), safe="")
        try:
            payload = self._request_json(
                "GET",
                self._url(f"/api/users/0/items/{encoded_key}/children"),
            )
        except ZoteroClientUnavailable as exc:
            return ZoteroClientResult(
                status="unavailable",
                backend=self.backend,
                data=[],
                error=str(exc),
                extra={"item_key": item_key},
            )
        if not isinstance(payload, list):
            return ZoteroClientResult(
                status="unexpected_payload",
                backend=self.backend,
                data=[],
                error="Zotero local children endpoint did not return a JSON array",
                extra={"item_key": item_key},
            )
        return ZoteroClientResult(
            status="ok",
            backend=self.backend,
            data=payload,
            extra={"item_key": item_key, "count": len(payload)},
        )

    def attachment_file_url(self, attachment_key: str) -> ZoteroClientResult:
        """Resolve a local attachment to Zotero's file URL without exposing it broadly."""

        encoded_key = urllib.parse.quote(str(attachment_key or "").strip(), safe="")
        try:
            payload = self._request_text(
                "GET",
                self._url(f"/api/users/0/items/{encoded_key}/file/view/url"),
            )
        except ZoteroClientUnavailable as exc:
            return ZoteroClientResult(
                status="unavailable",
                backend=self.backend,
                data=None,
                error=str(exc),
                extra={"attachment_key": attachment_key},
            )
        return ZoteroClientResult(
            status="ok",
            backend=self.backend,
            data=payload,
            extra={"attachment_key": attachment_key},
        )

    def create_item(self, item_payload: dict[str, Any]) -> ZoteroClientResult:
        return ZoteroClientResult(
            status="unsupported",
            backend=self.backend,
            data=None,
            error="Zotero local connector does not support item creation",
        )

    def add_tags(self, item_key: str, tags: list[str]) -> ZoteroClientResult:
        return ZoteroClientResult(
            status="unsupported",
            backend=self.backend,
            data=None,
            error="Zotero local connector does not support tag mutation",
        )

    def update_tags(
        self,
        item_key: str,
        tags: list[dict[str, Any]],
        *,
        version: int | None = None,
    ) -> ZoteroClientResult:
        return ZoteroClientResult(
            status="unsupported",
            backend=self.backend,
            data=None,
            error="Zotero local connector does not support tag mutation",
            extra={"item_key": item_key, "version": version, "tags": list(tags)},
        )

    def _url(self, path: str) -> str:
        base = self.base_url.rstrip("/")
        if not path.startswith("/"):
            path = "/" + path
        return f"{base}{path}"

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        response_metadata: Optional[dict[str, str]] = None,
    ) -> Any:
        raw = self._request_bytes(
            method,
            url,
            accept="application/json",
            response_metadata=response_metadata,
        )
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ZoteroClientError(
                f"Zotero local API returned non-JSON payload for {method} {url}: {exc.msg}"
            ) from exc

    def _request_text(self, method: str, url: str) -> str:
        raw = self._request_bytes(method, url, accept="text/plain")
        try:
            return raw.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ZoteroClientError(
                f"Zotero local API returned non-text payload for {method} {url}"
            ) from exc

    def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        accept: str,
        response_metadata: Optional[dict[str, str]] = None,
    ) -> bytes:
        request = urllib.request.Request(url, method=method, headers={"Accept": accept})
        opener_factory = self.opener or urllib.request.urlopen
        try:
            with opener_factory(request, timeout=self.timeout_seconds) as response:  # type: ignore[call-arg]
                raw = response.read()
                if response_metadata is not None:
                    response_headers = getattr(response, "headers", {})
                    total_results = response_headers.get("Total-Results")
                    if total_results is not None:
                        response_metadata["Total-Results"] = str(total_results)
        except urllib.error.URLError as exc:  # pragma: no cover - network path
            raise ZoteroClientUnavailable(
                f"Zotero local API unreachable for {method} {url}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:  # pragma: no cover - network path
            raise ZoteroClientUnavailable(
                f"Zotero local API timeout for {method} {url}"
            ) from exc
        return raw


@dataclass
class ZoteroMCPClient:
    """Adapter that routes Zotero operations through an MCP invoker."""

    invoker: Callable[[str, str, dict[str, Any]], dict[str, Any]]
    server_name: str = "zotero"
    backend: str = "mcp"

    def list_collections(self) -> ZoteroClientResult:
        response = self._invoke("list_collections", {})
        return self._normalize(response, default=[])

    def list_items(
        self,
        *,
        collection_key: Optional[str] = None,
        limit: int = 25,
    ) -> ZoteroClientResult:
        response = self._invoke(
            "list_items",
            {"collection_key": collection_key, "limit": max(1, int(limit))},
        )
        return self._normalize(
            response,
            default=[],
            extra={"collection_key": collection_key, "limit": limit},
        )

    def get_item(self, item_key: str) -> ZoteroClientResult:
        response = self._invoke("get_item", {"item_key": item_key})
        return self._normalize(response, default=None, extra={"item_key": item_key})

    def create_item(self, item_payload: dict[str, Any]) -> ZoteroClientResult:
        response = self._invoke("create_item", {"item": dict(item_payload)})
        return self._normalize(response, default=None)

    def add_tags(self, item_key: str, tags: list[str]) -> ZoteroClientResult:
        response = self._invoke("add_tags", {"item_key": item_key, "tags": list(tags)})
        return self._normalize(response, default=None, extra={"item_key": item_key, "tags": list(tags)})

    def update_tags(
        self,
        item_key: str,
        tags: list[dict[str, Any]],
        *,
        version: int | None = None,
    ) -> ZoteroClientResult:
        response = self._invoke(
            "update_tags",
            {"item_key": item_key, "tags": list(tags), "version": version},
        )
        return self._normalize(
            response,
            default=None,
            extra={"item_key": item_key, "version": version, "tags": list(tags)},
        )

    def _invoke(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return dict(self.invoker(self.server_name, tool_name, dict(arguments)))
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "status": "unavailable",
                "error": f"mcp invoker raised {type(exc).__name__}: {exc}",
            }

    @staticmethod
    def _normalize(
        response: dict[str, Any],
        *,
        default: Any,
        extra: Optional[dict[str, Any]] = None,
    ) -> ZoteroClientResult:
        status = str(response.get("status") or "ok")
        data = response.get("data", default)
        error = response.get("error")
        envelope_extra = dict(extra or {})
        for key, value in response.items():
            if key in {"status", "data", "error"}:
                continue
            envelope_extra.setdefault(key, value)
        return ZoteroClientResult(
            status=status,
            backend="mcp",
            data=data,
            error=error if error else None,
            extra=envelope_extra,
        )


def read_zotero_snapshot(client: ZoteroClient, *, collection_key: str | None = None, max_items: int = 10000) -> ZoteroClientResult:
    """Enumerate a bounded library; completeness is evidence, never a guess.

    Local API uses a server-side limit and verifies the reported total. Web pagination is
    trusted for deletions only when every page reports the same library version
    and total. Unversioned MCP results stay explicitly incomplete.
    """
    maximum = max(1, int(max_items))
    try:
        if isinstance(client, ZoteroLocalClient):
            path = f"/api/users/0/collections/{collection_key}/items" if collection_key else "/api/users/0/items"
            metadata: dict[str, str] = {}
            url = f"{client._url(path)}?{urllib.parse.urlencode({'limit': maximum})}"
            payload = client._request_json("GET", url, response_metadata=metadata)
            if not isinstance(payload, list):
                raise ZoteroClientError("Zotero local snapshot did not return an array")
            total_text = str(metadata.get("Total-Results") or "").strip()
            total = int(total_text) if total_text.isdigit() else None
            items = payload[:maximum]
            seen_keys: set[str] = set()
            valid_items = True
            for item in payload:
                if not isinstance(item, dict):
                    valid_items = False
                    break
                key = item.get("key")
                if not key and isinstance(item.get("data"), dict):
                    key = item["data"].get("key")
                if not isinstance(key, str) or not key.strip():
                    valid_items = False
                    break
                normalized_key = key.strip()
                if normalized_key in seen_keys:
                    valid_items = False
                    break
                seen_keys.add(normalized_key)
            count_verified = total is not None and total <= maximum and len(payload) == total
            complete = count_verified and valid_items
            if complete:
                reason = ""
            elif total is not None and total > maximum:
                reason = "snapshot_item_limit"
            elif not valid_items:
                reason = "snapshot_items_invalid_or_duplicate"
            else:
                reason = "snapshot_version_or_count_unverified"
            return ZoteroClientResult(
                status="ok",
                backend=client.backend,
                data=items,
                extra={"complete": complete, "total": total, "count": len(items), "reason": reason},
            )
        if isinstance(client, ZoteroWebClient):
            path = f"/users/{client.user_id}/collections/{collection_key}/items" if collection_key else f"/users/{client.user_id}/items"
            items: list[Any] = []
            seen: set[str] = set()
            first_metadata: dict[str, str] | None = None
            stable = True
            while len(items) < maximum:
                metadata: dict[str, str] = {}
                page_size = min(100, maximum - len(items))
                url = client._append_query(client._url(path), {"limit": page_size, "start": len(items), "format": "json", "sort": "dateAdded", "direction": "asc"})
                page = client._request_json("GET", url, response_metadata=metadata)
                if not isinstance(page, list):
                    raise ZoteroClientError("Zotero Web snapshot page did not return an array")
                if first_metadata is None:
                    first_metadata = metadata
                stable = stable and bool(metadata.get("Last-Modified-Version")) and metadata == first_metadata
                for item in page:
                    key = str(item.get("key") or (item.get("data") or {}).get("key") or "") if isinstance(item, dict) else ""
                    if not key or key in seen:
                        stable = False
                    seen.add(key)
                items.extend(page[:page_size])
                total_text = metadata.get("Total-Results", "")
                total = int(total_text) if total_text.isdigit() else None
                if len(page) < page_size or (total is not None and len(items) >= total):
                    complete = stable and total is not None and len(items) == total
                    return ZoteroClientResult(status="ok", backend=client.backend, data=items, extra={"complete": complete, "total": total, "count": len(items), "library_version": metadata.get("Last-Modified-Version"), "reason": "" if complete else "snapshot_version_or_count_unverified"})
            return ZoteroClientResult(status="ok", backend=client.backend, data=items, extra={"complete": False, "count": len(items), "reason": "snapshot_item_limit"})
        result = client.list_items(collection_key=collection_key, limit=maximum)
        result.extra = {**result.extra, "complete": result.extra.get("complete") is True}
        return result
    except (ZoteroClientError, OSError, ValueError) as exc:
        return ZoteroClientResult(status="unavailable", backend=client.backend, data=[], error=str(exc), extra={"complete": False})


def build_zotero_client(
    *,
    mode: Optional[str] = None,
    settings: Optional[dict[str, Any]] = None,
) -> ZoteroClient:
    """Factory that resolves the right Zotero client from env / settings.

    Resolution order:

    1. explicit ``mode`` argument (used by CLI / tests)
    2. ``ZOTERO_MODE`` env var
    3. auto-detect: prefer ``web`` if ``ZOTERO_USER_ID`` + ``ZOTERO_API_KEY`` are
       set, then use the read-only local API when a running Zotero instance
       answers on the configured loopback endpoint.
    """

    resolved_settings = dict(settings or {})
    env = os.environ
    resolved_mode = (mode or env.get("ZOTERO_MODE") or "").strip().lower() or None
    user_id = str(resolved_settings.get("user_id") or env.get("ZOTERO_USER_ID") or "").strip()
    api_key = str(resolved_settings.get("api_key") or env.get("ZOTERO_API_KEY") or "").strip()
    local_base = str(
        resolved_settings.get("local_base") or env.get("ZOTERO_LOCAL_BASE") or "http://127.0.0.1:23119"
    ).strip()
    mcp_invoker = resolved_settings.get("mcp_invoker")
    local_probe = resolved_settings.get("local_probe") or probe_zotero_local
    auto_local = str(env.get("ZOTERO_AUTO_LOCAL", "1")).strip().casefold() not in {
        "0",
        "false",
        "no",
        "off",
    }

    if resolved_mode is None:
        if user_id and api_key:
            resolved_mode = "web"
        elif mcp_invoker is not None:
            resolved_mode = "mcp"
        elif auto_local and bool(local_probe(local_base)):
            resolved_mode = "local"
        else:
            resolved_mode = "null"

    if resolved_mode == "web":
        if not user_id or not api_key:
            return NullZoteroClient(reason="zotero_web_mode_missing_credentials")
        return ZoteroWebClient(
            user_id=user_id,
            api_key=api_key,
            base_url=str(resolved_settings.get("base_url") or env.get("ZOTERO_BASE_URL") or "https://api.zotero.org"),
        )
    if resolved_mode == "local":
        return ZoteroLocalClient(base_url=local_base)
    if resolved_mode == "mcp":
        if mcp_invoker is None:
            return NullZoteroClient(reason="zotero_mcp_mode_missing_invoker")
        return ZoteroMCPClient(
            invoker=mcp_invoker,
            server_name=str(resolved_settings.get("server_name") or env.get("ZOTERO_MCP_SERVER") or "zotero"),
        )
    if resolved_mode == "null":
        return NullZoteroClient()
    raise ZoteroClientError(f"unsupported zotero mode: {resolved_mode!r}")


def probe_zotero_local(
    base_url: str = "http://127.0.0.1:23119",
    *,
    timeout_seconds: float = 0.35,
    opener: Optional[Callable[..., Any]] = None,
) -> bool:
    """Return whether a Zotero desktop connector is reachable on loopback.

    The probe is deliberately limited to loopback and the connector ping
    endpoint. It does not read library contents and bypasses host proxy
    settings, which otherwise commonly break localhost detection on Windows.
    """

    normalized = str(base_url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        return False
    request = urllib.request.Request(
        f"{normalized}/connector/ping",
        method="GET",
        headers={"Accept": "text/html"},
    )
    opener_factory = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({})
    ).open
    try:
        with opener_factory(request, timeout=timeout_seconds) as response:  # type: ignore[call-arg]
            response.read(128)
    except (OSError, TimeoutError, urllib.error.URLError):
        return False
    return True


def describe_zotero_client(client: ZoteroClient) -> dict[str, Any]:
    """Return a metadata-only summary useful for CLI `--describe` output."""

    backend = getattr(client, "backend", "unknown")
    if isinstance(client, ZoteroWebClient):
        return {
            "backend": backend,
            "user_id": client.user_id,
            "base_url": client.base_url,
            "api_version": client.api_version,
        }
    if isinstance(client, ZoteroLocalClient):
        return {"backend": backend, "base_url": client.base_url}
    if isinstance(client, ZoteroMCPClient):
        return {"backend": backend, "server_name": client.server_name}
    return {"backend": backend}


def _item_version(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    candidates = [payload.get("version")]
    meta = payload.get("meta")
    if isinstance(meta, dict):
        candidates.append(meta.get("version"))
    for candidate in candidates:
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
        if isinstance(candidate, str) and candidate.strip().isdigit():
            return int(candidate.strip())
    return None


def _normalize_tag_objects(tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, Any]] = set()
    for raw in tags:
        if not isinstance(raw, dict):
            continue
        tag = str(raw.get("tag") or "").strip()
        if not tag:
            continue
        tag_type = raw.get("type")
        identity = (tag, tag_type)
        if identity in seen:
            continue
        seen.add(identity)
        item: dict[str, Any] = {"tag": tag}
        if tag_type is not None:
            item["type"] = tag_type
        normalized.append(item)
    return normalized
