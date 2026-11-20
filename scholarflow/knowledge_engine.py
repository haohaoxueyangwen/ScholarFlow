"""Production AI knowledge-engine boundary backed by a LightRAG sidecar.

Queries are deliberately read-only. Governed ingestion and deletion keep the
LightRAG document lifecycle synchronized with an auditable ScholarFlow source map.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import time
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .domain_queries import DomainQueryError
from .evidence import EvidenceBundle, EvidenceSource
from .persistence import atomic_write_json, file_lock

LIGHTRAG_PINNED_VERSION = "1.5.4"
LIGHTRAG_MODES = ("auto", "mix", "hybrid", "local", "global", "naive")
SOURCE_MAP_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class KnowledgeEngineConfig:
    engine: str = "disabled"
    base_url: str = "http://127.0.0.1:9621"
    api_key: str = ""
    workspace: str = "scholarflow"
    timeout_seconds: float = 120.0
    allow_remote: bool = False

    @classmethod
    def from_env(cls) -> "KnowledgeEngineConfig":
        timeout_text = os.environ.get("SCHOLARFLOW_LIGHTRAG_TIMEOUT_SECONDS", "120").strip()
        try:
            timeout = float(timeout_text)
        except ValueError as exc:
            raise DomainQueryError(
                "knowledge_configuration_error",
                "SCHOLARFLOW_LIGHTRAG_TIMEOUT_SECONDS must be a number",
            ) from exc
        if timeout <= 0 or timeout > 300:
            raise DomainQueryError(
                "knowledge_configuration_error",
                "SCHOLARFLOW_LIGHTRAG_TIMEOUT_SECONDS must be between 0 and 300",
            )
        return cls(
            engine=os.environ.get("SCHOLARFLOW_KNOWLEDGE_ENGINE", "disabled").strip().casefold(),
            base_url=os.environ.get(
                "SCHOLARFLOW_LIGHTRAG_BASE_URL", "http://127.0.0.1:9621"
            ).strip(),
            api_key=os.environ.get("SCHOLARFLOW_LIGHTRAG_API_KEY", "").strip(),
            workspace=os.environ.get(
                "SCHOLARFLOW_LIGHTRAG_WORKSPACE", "scholarflow"
            ).strip(),
            timeout_seconds=timeout,
            allow_remote=_env_boolean("SCHOLARFLOW_LIGHTRAG_ALLOW_REMOTE", False),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "base_url": self.base_url,
            "workspace": self.workspace,
            "timeout_seconds": self.timeout_seconds,
            "allow_remote": self.allow_remote,
            "api_key_configured": bool(self.api_key),
            "pinned_version": LIGHTRAG_PINNED_VERSION,
        }


@dataclass(frozen=True)
class KnowledgeSource:
    engine_file_path: str
    source_ref: str
    source_type: str
    title: str
    content_hash: str
    source_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class KnowledgeHit:
    reference_id: str
    file_path: str
    chunks: tuple[str, ...]
    source: KnowledgeSource
    engine_rank: int = 0
    relevance_score: float = 0.0


@dataclass(frozen=True)
class KnowledgeSearchResult:
    query: str
    mode: str
    hits: tuple[KnowledgeHit, ...]
    engine: str = "lightrag"
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


class KnowledgeEngine(Protocol):
    def health(self) -> dict[str, Any]: ...

    def search(
        self,
        query: str,
        *,
        mode: str = "auto",
        limit: int = 5,
        scope: str | None = None,
        project_stage_id: str | None = None,
    ) -> KnowledgeSearchResult: ...


class KnowledgeSourceMap:
    """Atomic file-path to ScholarFlow-source mapping owned by ingestion."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "_knowledge" / "source-map.json"

    def register(self, source: KnowledgeSource) -> KnowledgeSource:
        normalized = _normalize_engine_path(source.engine_file_path)
        normalized_source = KnowledgeSource(
            engine_file_path=normalized,
            source_ref=_required(source.source_ref, "source_ref"),
            source_type=_required(source.source_type, "source_type"),
            title=_required(source.title, "title"),
            content_hash=_required(source.content_hash, "content_hash"),
            source_version=str(source.source_version or "").strip(),
            metadata=dict(source.metadata),
            updated_at=source.updated_at or _utc_now(),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path):
            payload = self._read()
            payload["items"][normalized] = normalized_source.to_dict()
            atomic_write_json(self.path, payload)
        return normalized_source

    def resolve(self, engine_file_path: str) -> KnowledgeSource:
        normalized = _normalize_engine_path(engine_file_path)
        items = self._read()["items"]
        exact = items.get(normalized)
        if exact is not None:
            return KnowledgeSource(**exact)
        suffix_matches = [
            value
            for path, value in items.items()
            if path.endswith(f"/{normalized}") or normalized.endswith(f"/{path}")
        ]
        if len(suffix_matches) == 1:
            return KnowledgeSource(**suffix_matches[0])
        details = {"file_path": normalized, "matches": len(suffix_matches)}
        code = "knowledge_provenance_ambiguous" if suffix_matches else "knowledge_provenance_missing"
        raise DomainQueryError(
            code,
            f"LightRAG result cannot be mapped to exactly one ScholarFlow source: {normalized}",
            details=details,
        )

    def count(self) -> int:
        return len(self._read()["items"])

    def list_sources(self, *, project_stage_id: str | None = None) -> list[KnowledgeSource]:
        """Read canonical ingestion mappings; never infer project membership."""
        sources=[KnowledgeSource(**value) for _,value in sorted(self._read()['items'].items())]
        return sources if project_stage_id is None else [s for s in sources if s.source_ref=='project:'+project_stage_id or s.metadata.get('project_stage_id')==project_stage_id or (isinstance(s.metadata.get('project_stage_ids'),list) and project_stage_id in s.metadata['project_stage_ids'])]

    def update_sources(self, sources: list[KnowledgeSource], *, expected_sha256: str | None) -> dict[str, Any]:
        """CAS one source-map file for a reviewed host batch; preserve other items."""
        from hashlib import sha256
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with file_lock(self.path):
            payload=self._read();updates={}
            for source in sources:
                path=_normalize_engine_path(source.engine_file_path)
                normalized=KnowledgeSource(path,_required(source.source_ref,'source_ref'),_required(source.source_type,'source_type'),_required(source.title,'title'),_required(source.content_hash,'content_hash'),source.source_version,dict(source.metadata),source.updated_at or _utc_now())
                if path in updates:raise DomainQueryError('invalid_argument','Duplicate source mapping target')
                updates[path]=normalized.to_dict()
            if all(payload['items'].get(k)==v for k,v in updates.items()):return {'idempotent_replay':True,'count':len(updates)}
            actual=sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
            if actual!=expected_sha256:raise DomainQueryError('version_conflict','Source mappings changed after host review')
            payload['items'].update(updates);atomic_write_json(self.path,payload)
            return {'idempotent_replay':False,'count':len(updates)}

    def remove(self, engine_file_path: str) -> KnowledgeSource:
        """Atomically remove one unambiguous provenance mapping."""

        normalized = _normalize_engine_path(engine_file_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path):
            payload = self._read()
            items = payload["items"]
            matched_path = normalized if normalized in items else None
            if matched_path is None:
                matches = [
                    path
                    for path in items
                    if path.endswith(f"/{normalized}") or normalized.endswith(f"/{path}")
                ]
                if len(matches) == 1:
                    matched_path = matches[0]
                elif len(matches) > 1:
                    raise DomainQueryError(
                        "knowledge_provenance_ambiguous",
                        f"knowledge source mapping is ambiguous: {normalized}",
                        details={"file_path": normalized, "matches": len(matches)},
                    )
            if matched_path is None:
                raise DomainQueryError(
                    "knowledge_provenance_missing",
                    f"knowledge source mapping does not exist: {normalized}",
                    details={"file_path": normalized},
                )
            value = items.pop(matched_path)
            atomic_write_json(self.path, payload)
        return KnowledgeSource(**value)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": SOURCE_MAP_SCHEMA_VERSION, "items": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DomainQueryError(
                "knowledge_source_map_invalid",
                f"knowledge source map is unreadable: {self.path}",
            ) from exc
        if payload.get("schema_version") != SOURCE_MAP_SCHEMA_VERSION or not isinstance(
            payload.get("items"), dict
        ):
            raise DomainQueryError(
                "knowledge_source_map_invalid",
                "knowledge source map schema is unsupported",
            )
        return payload


class DisabledKnowledgeEngine:
    def __init__(self, config: KnowledgeEngineConfig):
        self.config = config

    def health(self) -> dict[str, Any]:
        return {
            "status": "disabled",
            "available": False,
            "config": self.config.public_dict(),
        }

    def search(
        self,
        query: str,
        *,
        mode: str = "auto",
        limit: int = 5,
        scope: str | None = None,
        project_stage_id: str | None = None,
    ) -> KnowledgeSearchResult:
        del query, mode, limit, scope, project_stage_id
        raise DomainQueryError(
            "knowledge_engine_unavailable",
            "AI knowledge search is disabled; set SCHOLARFLOW_KNOWLEDGE_ENGINE=lightrag",
        )


class LightRAGClient:
    def __init__(
        self,
        config: KnowledgeEngineConfig,
        source_map: KnowledgeSourceMap,
        *,
        opener: Callable[..., Any] = urlopen,
    ):
        self.config = config
        self.source_map = source_map
        self._opener = opener
        self._base_url = _validate_base_url(config)
        if not config.api_key:
            raise DomainQueryError(
                "knowledge_configuration_error",
                "SCHOLARFLOW_LIGHTRAG_API_KEY is required when LightRAG is enabled",
            )

    def health(self) -> dict[str, Any]:
        payload = self._request("GET", "/health")
        service_status = str(payload.get("status") or "unknown").strip().casefold()
        incompatible = service_status in {"error", "failed", "unhealthy"}
        if not incompatible:
            status_counts = self._request("GET", "/documents/status_counts")
            _validate_document_status_counts(status_counts)
        return {
            "status": "degraded" if incompatible else "ready",
            "available": not incompatible,
            "engine": "lightrag",
            "pinned_version": LIGHTRAG_PINNED_VERSION,
            "source_map_entries": self.source_map.count(),
            "service": _redact_sensitive(payload),
            "config": self.config.public_dict(),
        }

    def search(
        self,
        query: str,
        *,
        mode: str = "auto",
        limit: int = 5,
        scope: str | None = None,
        project_stage_id: str | None = None,
    ) -> KnowledgeSearchResult:
        normalized_query = _required(query, "query")
        if len(normalized_query) < 3:
            raise DomainQueryError(
                "invalid_argument",
                "query must contain at least 3 characters",
                details={"field": "query", "minimum_length": 3},
            )
        normalized_mode = str(mode or "auto").strip().casefold()
        if normalized_mode not in LIGHTRAG_MODES:
            raise DomainQueryError(
                "invalid_argument",
                f"mode must be one of: {', '.join(LIGHTRAG_MODES)}",
                details={"field": "mode"},
            )
        normalized_limit = _normalize_limit(limit)
        normalized_scope = str(scope or "").strip() or None
        normalized_project = str(project_stage_id or "").strip() or None
        effective_mode = "mix" if normalized_mode == "auto" else normalized_mode
        retrieval_limit = min(200, max(40, normalized_limit * 8))
        payload = self._request(
            "POST",
            "/query",
            {
                "query": normalized_query,
                "mode": effective_mode,
                "only_need_context": True,
                "include_references": True,
                "include_chunk_content": True,
                "enable_rerank": True,
                "top_k": retrieval_limit,
                "chunk_top_k": retrieval_limit,
            },
        )
        references = payload.get("references")
        if not isinstance(references, list):
            raise DomainQueryError(
                "knowledge_response_invalid",
                "LightRAG query response did not include a references array",
            )
        mapped_hits: list[KnowledgeHit] = []
        for index, reference in enumerate(references):
            if not isinstance(reference, dict):
                raise DomainQueryError(
                    "knowledge_response_invalid",
                    "LightRAG returned a non-object reference",
                    details={"reference_index": index},
                )
            file_path = str(reference.get("file_path") or "").strip()
            chunks_value = reference.get("content")
            if isinstance(chunks_value, str):
                chunks = (chunks_value.strip(),) if chunks_value.strip() else ()
            elif isinstance(chunks_value, list):
                chunks = tuple(
                    str(item).strip() for item in chunks_value if str(item).strip()
                )
            else:
                chunks = ()
            if not file_path or not chunks:
                raise DomainQueryError(
                    "knowledge_response_invalid",
                    "LightRAG references must contain file_path and non-empty content",
                    details={"reference_index": index},
                )
            source = self.source_map.resolve(file_path)
            mapped_hits.append(
                KnowledgeHit(
                    reference_id=str(reference.get("reference_id") or index + 1),
                    file_path=file_path,
                    chunks=chunks,
                    source=source,
                    engine_rank=index + 1,
                )
            )
        eligible_hits = [
            replace(
                hit,
                relevance_score=_knowledge_relevance_score(
                    normalized_query,
                    hit.source,
                    hit.chunks,
                ),
            )
            for hit in mapped_hits
            if (normalized_scope is None or _source_matches_scope(hit.source, normalized_scope))
            and (
                normalized_project is None
                or _source_matches_project(hit.source, normalized_project)
            )
        ]
        eligible_hits.sort(key=lambda hit: (-hit.relevance_score, hit.engine_rank))
        hits = eligible_hits[:normalized_limit]
        return KnowledgeSearchResult(
            query=normalized_query,
            mode=effective_mode,
            hits=tuple(hits),
            trace=(
                {
                    "engine": "lightrag",
                    "mode": effective_mode,
                    "reference_count": len(references),
                    "eligible_count": len(eligible_hits),
                    "filtered_out_count": len(mapped_hits) - len(eligible_hits),
                    "selected_count": len(hits),
                    "scope": normalized_scope,
                    "project_stage_id": normalized_project,
                    "ranking": "scholarflow-lexical-v1",
                    "pinned_version": LIGHTRAG_PINNED_VERSION,
                },
            ),
        )

    def ingest_text(self, text: str, source: KnowledgeSource) -> dict[str, Any]:
        """Queue one governed text snapshot and persist its provenance mapping."""

        normalized_text = _required(text, "text")
        normalized_path = _normalize_engine_path(source.engine_file_path)
        if "/" in normalized_path or normalized_path in {".", ".."}:
            raise DomainQueryError(
                "invalid_argument",
                "text ingestion engine_file_path must be a safe basename",
                details={"field": "engine_file_path"},
            )
        registered = self.source_map.register(
            KnowledgeSource(
                engine_file_path=normalized_path,
                source_ref=source.source_ref,
                source_type=source.source_type,
                title=source.title,
                content_hash=source.content_hash,
                source_version=source.source_version,
                metadata=source.metadata,
                updated_at=source.updated_at,
            )
        )
        payload = self._request(
            "POST",
            "/documents/text",
            {"text": normalized_text, "file_source": normalized_path},
        )
        track_id = str(payload.get("track_id") or "").strip()
        if not track_id:
            raise DomainQueryError(
                "knowledge_response_invalid",
                "LightRAG ingestion response did not include track_id",
            )
        return {
            "status": str(payload.get("status") or "unknown"),
            "message": str(payload.get("message") or ""),
            "track_id": track_id,
            "source": registered.to_dict(),
        }

    def refresh_source(self, source: KnowledgeSource) -> dict[str, Any]:
        """Refresh ScholarFlow provenance metadata without re-ingesting content."""

        registered = self.source_map.register(source)
        return {
            "status": "refreshed",
            "source_ref": registered.source_ref,
            "engine_file_path": registered.engine_file_path,
        }

    def track_status(self, track_id: str) -> dict[str, Any]:
        normalized = _required(track_id, "track_id")
        payload = self._request(
            "GET",
            f"/documents/track_status/{quote(normalized, safe='')}",
        )
        if str(payload.get("track_id") or "") != normalized:
            raise DomainQueryError(
                "knowledge_response_invalid",
                "LightRAG track status returned a different track_id",
            )
        if not isinstance(payload.get("documents"), list) or not isinstance(
            payload.get("status_summary"), dict
        ):
            raise DomainQueryError(
                "knowledge_response_invalid",
                "LightRAG track status payload is malformed",
            )
        return payload

    def find_document_by_path(self, engine_file_path: str) -> dict[str, Any] | None:
        """Find one exact LightRAG document for crash-safe ingestion recovery."""

        normalized_path = _normalize_engine_path(engine_file_path)
        payload = self._request("GET", "/documents")
        statuses = payload.get("statuses")
        if not isinstance(statuses, dict):
            raise DomainQueryError(
                "knowledge_response_invalid",
                "LightRAG document inventory is malformed",
            )
        matches: list[dict[str, Any]] = []
        for documents in statuses.values():
            if not isinstance(documents, list):
                raise DomainQueryError(
                    "knowledge_response_invalid",
                    "LightRAG document inventory contains a malformed status group",
                )
            for document in documents:
                if (
                    isinstance(document, dict)
                    and _normalize_engine_path(str(document.get("file_path") or ""))
                    == normalized_path
                ):
                    matches.append(dict(document))
        if len(matches) > 1:
            raise DomainQueryError(
                "knowledge_response_invalid",
                "LightRAG lists more than one document for the exact engine path",
                details={"engine_file_path": normalized_path},
            )
        return matches[0] if matches else None

    def delete_documents(
        self,
        documents: dict[str, str],
        *,
        delete_llm_cache: bool = False,
        wait_timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """Delete processed documents and retire provenance after confirmation.

        ``documents`` maps LightRAG document IDs to their exact engine file paths.
        Source-map entries remain available until LightRAG reports an idle pipeline
        and no longer lists any target document, preventing premature provenance loss.
        """

        if not documents:
            raise DomainQueryError(
                "invalid_argument",
                "documents must contain at least one document ID and file path",
                details={"field": "documents"},
            )
        if wait_timeout_seconds <= 0 or wait_timeout_seconds > 1800:
            raise DomainQueryError(
                "invalid_argument",
                "wait_timeout_seconds must be between 0 and 1800",
                details={"field": "wait_timeout_seconds"},
            )
        normalized: dict[str, str] = {}
        for document_id, engine_file_path in documents.items():
            normalized_id = _required(document_id, "document_id")
            normalized_path = _normalize_engine_path(engine_file_path)
            if normalized_id in normalized:
                raise DomainQueryError(
                    "invalid_argument",
                    "document IDs must be unique",
                    details={"field": "documents"},
                )
            self.source_map.resolve(normalized_path)
            normalized[normalized_id] = normalized_path
        if len(set(normalized.values())) != len(normalized):
            raise DomainQueryError(
                "invalid_argument",
                "engine file paths must be unique",
                details={"field": "documents"},
            )

        response = self._request(
            "DELETE",
            "/documents/delete_document",
            {
                "doc_ids": list(normalized),
                "delete_file": False,
                "delete_llm_cache": bool(delete_llm_cache),
            },
        )
        status = str(response.get("status") or "").strip()
        if status == "busy":
            raise DomainQueryError(
                "knowledge_engine_busy",
                "LightRAG is busy and did not schedule document deletion",
                details={"document_ids": list(normalized)},
            )
        if status != "deletion_started":
            raise DomainQueryError(
                "knowledge_response_invalid",
                "LightRAG did not confirm document deletion was started",
                details={"status": status},
            )

        deadline = time.monotonic() + wait_timeout_seconds
        while True:
            pipeline = self._request("GET", "/documents/pipeline_status")
            if not bool(pipeline.get("busy")) and not bool(
                pipeline.get("destructive_busy")
            ):
                break
            if time.monotonic() >= deadline:
                raise DomainQueryError(
                    "knowledge_delete_timeout",
                    "LightRAG document deletion did not finish before the deadline",
                    details={"document_ids": list(normalized)},
                )
            time.sleep(0.5)

        remaining = _collect_document_ids(self._request("GET", "/documents"))
        not_deleted = sorted(set(normalized).intersection(remaining))
        if not_deleted:
            raise DomainQueryError(
                "knowledge_delete_incomplete",
                "LightRAG finished deletion but still lists target documents",
                details={"document_ids": not_deleted},
            )

        retired = [
            self.source_map.remove(engine_file_path).to_dict()
            for engine_file_path in normalized.values()
        ]
        return {
            "status": "deleted",
            "document_ids": list(normalized),
            "retired_sources": retired,
        }

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "X-API-Key": self.config.api_key,
            "X-Workspace-ID": self.config.workspace,
        }
        encoded = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self._base_url}{path}",
            data=encoded,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            raise DomainQueryError(
                "knowledge_engine_rejected",
                f"LightRAG returned HTTP {exc.code}",
                details={"status": exc.code},
            ) from exc
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise DomainQueryError(
                "knowledge_engine_unavailable",
                "LightRAG is unavailable or timed out",
                details={"base_url": self._base_url},
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DomainQueryError(
                "knowledge_response_invalid",
                "LightRAG returned invalid JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise DomainQueryError(
                "knowledge_response_invalid",
                "LightRAG response must be a JSON object",
            )
        return payload


def build_knowledge_engine(
    data_dir: Path,
    *,
    config: KnowledgeEngineConfig | None = None,
    opener: Callable[..., Any] = urlopen,
) -> KnowledgeEngine:
    effective = config or KnowledgeEngineConfig.from_env()
    if effective.engine in {"", "disabled", "none", "null"}:
        return DisabledKnowledgeEngine(effective)
    if effective.engine != "lightrag":
        raise DomainQueryError(
            "knowledge_configuration_error",
            f"unsupported knowledge engine: {effective.engine}",
        )
    source_map = KnowledgeSourceMap(Path(data_dir))
    return LightRAGClient(effective, source_map, opener=opener)


def prepare_knowledge_bundle(
    engine: KnowledgeEngine,
    *,
    query: str,
    mode: str = "auto",
    limit: int = 5,
    scope: str | None = None,
    project_stage_id: str | None = None,
) -> EvidenceBundle:
    search_options: dict[str, Any] = {"mode": mode, "limit": limit}
    if str(scope or "").strip():
        search_options["scope"] = scope
    if str(project_stage_id or "").strip():
        search_options["project_stage_id"] = project_stage_id
    result = engine.search(query, **search_options)
    sources: list[EvidenceSource] = []
    citations: list[str] = []
    for hit in result.hits:
        source = hit.source
        if source.source_ref not in citations:
            citations.append(source.source_ref)
        sources.append(
            EvidenceSource(
                ref=source.source_ref,
                source_type=source.source_type,
                title=source.title,
                detail=f"LightRAG reference {hit.reference_id} | {hit.file_path}",
                snippet=_query_focused_snippet(
                    hit.chunks,
                    query=result.query,
                    limit=1200,
                ),
                record_json=json.dumps(
                    {
                        "engine_file_path": hit.file_path,
                        "content_hash": source.content_hash,
                        "source_version": source.source_version,
                        "metadata": source.metadata,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                retrieval_score=hit.relevance_score,
                retrieval_channels=("lightrag", result.mode),
                retrieval_provenance=(
                    f"lightrag:{LIGHTRAG_PINNED_VERSION}",
                    f"reference:{hit.reference_id}",
                    f"engine-rank:{hit.engine_rank}",
                    f"content_hash:{source.content_hash}",
                ),
            )
        )
    return EvidenceBundle(
        query=result.query,
        summary=(
            f'Prepared {len(sources)} provenance-checked AI knowledge sources for "{result.query}".'
            if sources
            else f'No AI knowledge sources matched "{result.query}".'
        ),
        citations=tuple(citations),
        sources=tuple(sources),
        retriever=f"lightrag:{result.mode}",
        retrieval_trace=result.trace,
    )


def _source_matches_scope(source: KnowledgeSource, scope: str) -> bool:
    normalized = scope.strip().casefold()
    if not normalized:
        return True
    source_ref = source.source_ref.casefold()
    metadata_scope = str(source.metadata.get("scope") or "").strip().casefold()
    return (
        metadata_scope == normalized
        or source_ref == normalized
        or source_ref.startswith(f"{normalized}:")
    )


def _source_matches_project(source: KnowledgeSource, project_stage_id: str) -> bool:
    normalized = project_stage_id.strip().casefold()
    if normalized.startswith("project:"):
        normalized = normalized.removeprefix("project:")
    if not normalized:
        return True
    metadata_project = str(source.metadata.get("project_stage_id") or "").strip().casefold()
    projects = source.metadata.get("project_stage_ids") or []
    project_ids = {str(item).strip().casefold().removeprefix("project:") for item in projects} if isinstance(projects, list) else set()
    return normalized in project_ids or metadata_project == normalized or source.source_ref.casefold() == f"project:{normalized}"


def _knowledge_relevance_score(
    query: str,
    source: KnowledgeSource,
    chunks: tuple[str, ...],
) -> float:
    """Provide a deterministic tie-breaker above LightRAG's opaque reference order."""

    terms = _knowledge_query_terms(query)
    if not terms:
        return 0.0
    identity = f"{source.title}\n{source.source_ref}".casefold()
    body = "\n".join(chunks).casefold()
    matched = 0.0
    possible = 0.0
    for term in terms:
        weight = min(6.0, max(1.0, len(term) / 2.0))
        possible += weight * 4.0
        matched += weight * min(3, identity.count(term)) * 2.0
        matched += weight * min(2, body.count(term))
    return round(min(1.0, matched / max(1.0, possible)), 6)


def _knowledge_query_terms(query: str) -> tuple[str, ...]:
    normalized = query.casefold()
    terms = set(re.findall(r"[a-z0-9]+(?:[./_-][a-z0-9]+)*", normalized))
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        if 2 <= len(run) <= 12:
            terms.add(run)
        for width in (2, 3):
            terms.update(run[index : index + width] for index in range(len(run) - width + 1))
    return tuple(sorted(term for term in terms if len(term) >= 2))


def _validate_base_url(config: KnowledgeEngineConfig) -> str:
    parsed = urlparse(config.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DomainQueryError(
            "knowledge_configuration_error",
            "SCHOLARFLOW_LIGHTRAG_BASE_URL must be an absolute HTTP(S) URL",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DomainQueryError(
            "knowledge_configuration_error",
            "LightRAG base URL must not contain credentials, query, or fragment",
        )
    is_loopback = parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}
    if not is_loopback and not config.allow_remote:
        raise DomainQueryError(
            "knowledge_remote_blocked",
            "remote LightRAG endpoints require SCHOLARFLOW_LIGHTRAG_ALLOW_REMOTE=true",
        )
    if not is_loopback and parsed.scheme != "https":
        raise DomainQueryError(
            "knowledge_remote_blocked",
            "remote LightRAG endpoints must use HTTPS",
        )
    return config.base_url.rstrip("/")


def _normalize_engine_path(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    normalized = normalized.removeprefix("./").rstrip("/")
    return _required(normalized, "engine_file_path")


def _required(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise DomainQueryError(
            "invalid_argument",
            f"{field_name} is required",
            details={"field": field_name},
        )
    return normalized


def _normalize_limit(value: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise DomainQueryError("invalid_argument", "limit must be an integer") from exc
    if normalized < 1 or normalized > 20:
        raise DomainQueryError(
            "invalid_argument",
            "limit must be between 1 and 20",
            details={"field": "limit", "minimum": 1, "maximum": 20},
        )
    return normalized


def _validate_document_status_counts(payload: dict[str, Any]) -> None:
    """Validate LightRAG's small, authenticated document-readiness response."""

    status_counts = payload.get("status_counts")
    if not isinstance(status_counts, dict) or any(
        not isinstance(status, str)
        or not status.strip()
        or type(count) is not int
        or count < 0
        for status, count in status_counts.items()
    ):
        raise DomainQueryError(
            "knowledge_response_invalid",
            "LightRAG document status-count response is malformed",
        )


def _collect_document_ids(value: Any) -> set[str]:
    """Collect document IDs from LightRAG's status-grouped response."""

    found: set[str] = set()
    if isinstance(value, dict):
        document_id = value.get("id")
        if isinstance(document_id, str) and document_id.strip():
            found.add(document_id.strip())
        for item in value.values():
            found.update(_collect_document_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_collect_document_ids(item))
    return found


def _env_boolean(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise DomainQueryError(
        "knowledge_configuration_error",
        f"{name} must be true or false",
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _truncate(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _query_focused_snippet(
    chunks: tuple[str, ...],
    *,
    query: str,
    limit: int,
) -> str:
    text = "\n\n".join(str(item).strip() for item in chunks if str(item).strip())
    if len(text) <= limit:
        return text
    stopwords = {
        "about",
        "from",
        "reported",
        "section",
        "that",
        "the",
        "this",
        "what",
        "when",
        "where",
        "which",
        "with",
    }
    terms = _query_snippet_terms(query, stopwords=stopwords)
    lowered = text.casefold()
    candidate_positions = [
        match.start()
        for term in terms
        for match in re.finditer(re.escape(term), lowered)
    ]
    if not candidate_positions:
        return _truncate(text, limit)

    def candidate_window(position: int) -> tuple[int, int, int]:
        start = max(0, position - limit // 3)
        anchor = lowered.rfind(
            "<!-- sf-anchor",
            max(0, position - 600),
            position + 1,
        )
        if anchor >= 0:
            start = anchor
        end = min(len(text), start + limit)
        window = lowered[start:end]
        score = sum(
            min(window.count(term), 3)
            for term in terms
        )
        return score, -start, start

    _score, _negative_start, start = max(
        candidate_window(position)
        for position in candidate_positions
    )
    excerpt = text[start : start + limit]
    if start > 0:
        excerpt = "... " + excerpt
    if start + limit < len(text):
        excerpt = excerpt.rstrip() + " ..."
    return excerpt


def _query_snippet_terms(
    query: str,
    *,
    stopwords: set[str],
) -> set[str]:
    terms: set[str] = set()
    for item in re.findall(
        r"[A-Za-z0-9_.-]+|[\u3400-\u9fff]+",
        str(query or ""),
    ):
        folded = item.casefold()
        if re.fullmatch(r"[\u3400-\u9fff]+", item):
            for width in (2, 3, 4):
                if len(item) >= width:
                    terms.update(
                        item[index : index + width]
                        for index in range(len(item) - width + 1)
                    )
        elif len(item) >= 3 and folded not in stopwords:
            terms.add(folded)
    return terms


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(marker in normalized for marker in ("api_key", "password", "secret", "token")):
                redacted[str(key)] = "<redacted>" if item else item
            else:
                redacted[str(key)] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value
