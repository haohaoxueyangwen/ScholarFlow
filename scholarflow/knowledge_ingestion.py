"""Governed discovery and incremental synchronization for the AI knowledge base."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable, Protocol

from .domain_queries import DomainQueryError
from .knowledge_engine import KnowledgeSource
from .paper_service import normalize_zotero_item
from .persistence import atomic_write_json, file_lock
from .scientific_document import parse_scientific_pdf
from .zotero_client import ZoteroClient, read_zotero_snapshot

INGESTION_MANIFEST_VERSION = 1
TEXT_EXTENSIONS = {".md", ".markdown", ".txt"}
PDF_EXTENSIONS = {".pdf"}
DEFAULT_IGNORED_DIRECTORY_NAMES = {
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "_knowledge",
}


@dataclass(frozen=True)
class IngestionCandidate:
    source_ref: str
    source_type: str
    title: str
    text: str
    scope: str
    locator: str
    source_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    content_digest: str = ""
    structured_document: dict[str, Any] | None = None

    @property
    def digest(self) -> str:
        normalized = str(self.content_digest or "").removeprefix("sha256:")
        if normalized and re.fullmatch(r"[0-9a-fA-F]{64}", normalized):
            return normalized.casefold()
        return sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def engine_file_path(self) -> str:
        safe_ref = re.sub(r"[^A-Za-z0-9._-]+", "__", self.source_ref).strip("._-")
        ref_digest = sha256(self.source_ref.encode("utf-8")).hexdigest()
        return (
            f"{(safe_ref or 'source')[:96]}"
            f"__{ref_digest[:12]}__{self.digest[:16]}.md"
        )


@dataclass(frozen=True)
class IngestionAction:
    action: str
    source_ref: str
    reason: str
    candidate: IngestionCandidate | None = None
    previous: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.candidate is not None:
            payload["candidate"].pop("text", None)
            payload["candidate"].pop("structured_document", None)
            locator = str(payload["candidate"].get("locator") or "")
            relative = str(self.candidate.metadata.get("relative_path") or "")
            payload["candidate"]["locator"] = (
                relative
                if relative
                else locator
                if locator.startswith("zotero:")
                else "<approved-local-source>"
            )
            payload["candidate"]["content_hash"] = f"sha256:{self.candidate.digest}"
            payload["candidate"]["engine_file_path"] = self.candidate.engine_file_path
        return payload


@dataclass(frozen=True)
class IngestionPlan:
    scopes: tuple[str, ...]
    actions: tuple[IngestionAction, ...]

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for action in self.actions:
            counts[action.action] = counts.get(action.action, 0) + 1
        return {
            "schema_version": 1,
            "scopes": list(self.scopes),
            "counts": counts,
            "actions": [action.to_dict() for action in self.actions],
            "requires_confirmation": any(
                action.action in {"add", "update", "refresh", "remove"} for action in self.actions
            ),
        }


class IngestionEngine(Protocol):
    def ingest_text(self, text: str, source: KnowledgeSource) -> dict[str, Any]: ...

    def refresh_source(self, source: KnowledgeSource) -> dict[str, Any]: ...

    def track_status(self, track_id: str) -> dict[str, Any]: ...

    def find_document_by_path(self, engine_file_path: str) -> dict[str, Any] | None: ...

    def delete_documents(
        self,
        documents: dict[str, str],
        *,
        delete_llm_cache: bool = False,
        wait_timeout_seconds: float = 300.0,
    ) -> dict[str, Any]: ...


class KnowledgeIngestionManifest:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "_knowledge" / "ingestion-manifest.json"
        self.document_root = self.path.parent / "scientific-documents"

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": INGESTION_MANIFEST_VERSION, "sources": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DomainQueryError(
                "knowledge_ingestion_manifest_invalid",
                f"ingestion manifest is unreadable: {self.path}",
            ) from exc
        if (
            payload.get("schema_version") != INGESTION_MANIFEST_VERSION
            or not isinstance(payload.get("sources"), dict)
        ):
            raise DomainQueryError(
                "knowledge_ingestion_manifest_invalid",
                "ingestion manifest schema is unsupported",
            )
        return payload

    def set(self, source_ref: str, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path):
            payload = self.read()
            payload["sources"][source_ref] = dict(record)
            atomic_write_json(self.path, payload)

    def remove(self, source_ref: str) -> dict[str, Any] | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path):
            payload = self.read()
            removed = payload["sources"].pop(source_ref, None)
            atomic_write_json(self.path, payload)
        return removed

    def store_structured_document(
        self,
        source_ref: str,
        content_digest: str,
        payload: dict[str, Any],
    ) -> str:
        source_key = sha256(source_ref.encode("utf-8")).hexdigest()[:24]
        normalized_digest = str(content_digest).removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_digest):
            raise DomainQueryError(
                "knowledge_scientific_document_invalid",
                "structured document content digest is invalid",
            )
        target = (
            self.document_root
            / source_key
            / f"{normalized_digest}.json"
        )
        atomic_write_json(target, dict(payload))
        return target.relative_to(self.path.parent).as_posix()

    def remove_structured_document(self, record: dict[str, Any]) -> None:
        relative = str(record.get("structured_document_path") or "").strip()
        if not relative:
            return
        root = self.document_root.resolve()
        target = (self.path.parent / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise DomainQueryError(
                "knowledge_ingestion_manifest_invalid",
                "structured document path escaped the catalog root",
            ) from exc
        if target.exists():
            target.unlink()
        parent = target.parent
        if parent != root and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()


def summarize_ingestion_manifest(
    manifest: KnowledgeIngestionManifest,
    *,
    scope: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    records = manifest.read()["sources"]
    selected = [
        value
        for value in records.values()
        if isinstance(value, dict) and (scope is None or value.get("scope") == scope)
    ]
    selected.sort(key=lambda item: str(item.get("source_ref") or ""))
    by_scope: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for item in selected:
        item_scope = str(item.get("scope") or "unknown")
        item_type = str(item.get("source_type") or "unknown")
        by_scope[item_scope] = by_scope.get(item_scope, 0) + 1
        by_type[item_type] = by_type.get(item_type, 0) + 1
    return {
        "count": len(selected),
        "by_scope": by_scope,
        "by_type": by_type,
        "sources": [
            {
                key: item.get(key)
                for key in (
                    "source_ref",
                    "source_type",
                    "title",
                    "scope",
                    "source_version",
                    "content_hash",
                    "status",
                    "document_id",
                )
            }
            for item in selected[: max(1, min(int(limit), 500))]
        ],
    }


def discover_file_sources(
    root: Path,
    *,
    source_type: str,
    root_id: str,
    extensions: Iterable[str] = (*TEXT_EXTENSIONS, *PDF_EXTENSIONS),
    max_file_bytes: int = 25_000_000,
    ignored_directory_names: Iterable[str] = DEFAULT_IGNORED_DIRECTORY_NAMES,
    include_hidden: bool = False,
) -> list[IngestionCandidate]:
    """Discover bounded files without following paths outside the approved root."""

    approved_root = Path(root).resolve()
    if not approved_root.is_dir():
        raise DomainQueryError(
            "knowledge_source_root_invalid",
            f"source root is not a directory: {approved_root}",
        )
    normalized_extensions = {
        item if str(item).startswith(".") else f".{item}"
        for item in (str(value).casefold() for value in extensions)
    }
    ignored_names = {str(value).casefold() for value in ignored_directory_names}
    candidates: list[IngestionCandidate] = []
    for path in sorted(approved_root.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in normalized_extensions:
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(approved_root)
        except ValueError as exc:
            raise DomainQueryError(
                "knowledge_source_scope_escape",
                f"discovered file escaped approved root: {path}",
            ) from exc
        directory_parts = relative.parts[:-1]
        if any(part.casefold() in ignored_names for part in directory_parts):
            continue
        if not include_hidden and any(part.startswith(".") for part in relative.parts):
            continue
        size = resolved.stat().st_size
        if size > max_file_bytes:
            raise DomainQueryError(
                "knowledge_source_too_large",
                f"source exceeds maximum size: {relative.as_posix()}",
                details={"size": size, "maximum": max_file_bytes},
            )
        scientific = None
        if resolved.suffix.casefold() in PDF_EXTENSIONS:
            scientific = parse_scientific_pdf(resolved)
            text = scientific.to_index_markdown()
        else:
            text = extract_document_text(resolved)
        if not text.strip():
            continue
        relative_ref = relative.as_posix()
        title = (
            scientific.title
            if scientific is not None
            else _title_from_text(text, fallback=resolved.stem)
        )
        source_version = f"file:{resolved.stat().st_mtime_ns}:{size}"
        metadata: dict[str, Any] = {
            "root_id": root_id,
            "relative_path": relative_ref,
            "extension": resolved.suffix.casefold(),
        }
        if scientific is None:
            frontmatter = _frontmatter_scalars(text)
            for key in ("project_stage_id", "type", "status", "generated_by"):
                value = str(frontmatter.get(key) or "").strip()
                if value:
                    metadata[key] = value
        content_digest = ""
        if scientific is not None:
            raw_digest = scientific.content_hash.removeprefix("sha256:")
            index_digest = sha256(
                (
                    f"{raw_digest}\0{scientific.schema_version}\0"
                    f"{scientific.parser_name}\0{scientific.parser_version}\0{text}"
                ).encode("utf-8")
            ).hexdigest()
            content_digest = index_digest
            source_version = (
                f"pdf:{raw_digest}:schema-{scientific.schema_version}:"
                f"{scientific.parser_name}:{scientific.parser_version}"
            )
            metadata.update(
                {
                    "scientific_document": scientific.structure_summary,
                    "source_content_hash": scientific.content_hash,
                }
            )
        candidates.append(
            IngestionCandidate(
                source_ref=f"{source_type}:{root_id}:{relative_ref}",
                source_type=source_type,
                title=title,
                text=text,
                scope=f"{source_type}:{root_id}",
                locator=str(resolved),
                source_version=source_version,
                metadata=metadata,
                content_digest=content_digest,
                structured_document=(
                    scientific.to_dict()
                    if scientific is not None
                    else None
                ),
            )
        )
    return candidates


def discover_zotero_sources(
    client: ZoteroClient,
    *,
    collection_key: str | None = None,
    limit: int = 100,
    complete_snapshot: bool = False,
    discovery: dict[str, Any] | None = None,
) -> list[IngestionCandidate]:
    """Materialize Zotero bibliographic records as rebuildable Markdown."""

    result = (read_zotero_snapshot(client, collection_key=collection_key)
              if complete_snapshot else client.list_items(collection_key=collection_key, limit=limit))
    if discovery is not None:
        discovery.update(result.extra)
        discovery["complete"] = result.extra.get("complete") is True
    if result.status != "ok" or not isinstance(result.data, list):
        raise DomainQueryError(
            "knowledge_zotero_unavailable",
            result.error or f"Zotero returned status {result.status}",
        )
    scope = f"zotero:{client.backend}:{collection_key or 'all'}"
    candidates: list[IngestionCandidate] = []
    for raw in result.data:
        if not isinstance(raw, dict):
            if discovery is not None:
                discovery["complete"] = False
            continue
        normalized = normalize_zotero_item(raw)
        key = str(normalized.get("item_key") or "").strip()
        if not key:
            if discovery is not None:
                discovery["complete"] = False
            continue
        if normalized.get("item_type") == "attachment":
            continue
        text = _zotero_markdown(normalized)
        candidates.append(
            IngestionCandidate(
                source_ref=f"zotero:{key}",
                source_type="paper",
                title=str(normalized["title"]),
                text=text,
                scope=scope,
                locator=f"zotero:{client.backend}:{key}",
                source_version=f"zotero:{normalized.get('item_version') or 'unknown'}",
                metadata={
                    "backend": client.backend,
                    "collection_key": collection_key,
                    "item_key": key,
                    "doi": normalized.get("doi") or "",
                },
            )
        )
    return candidates


def discover_zotero_attachment_sources(
    client: ZoteroClient,
    storage_root: Path,
    *,
    collection_key: str | None = None,
    limit: int = 500,
) -> list[IngestionCandidate]:
    """Resolve approved Zotero storage PDFs to their parent bibliographic items."""

    root = Path(storage_root).resolve()
    if not root.is_dir():
        raise DomainQueryError(
            "knowledge_zotero_storage_invalid",
            f"Zotero storage root is not a directory: {root}",
        )
    result = client.list_items(collection_key=collection_key, limit=limit)
    if result.status != "ok" or not isinstance(result.data, list):
        raise DomainQueryError(
            "knowledge_zotero_unavailable",
            result.error or f"Zotero returned status {result.status}",
        )
    items: dict[str, dict[str, Any]] = {}
    attachments: list[dict[str, Any]] = []
    for raw in result.data:
        if not isinstance(raw, dict):
            continue
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        key = str(data.get("key") or raw.get("key") or "").strip()
        if not key:
            continue
        if data.get("itemType") == "attachment":
            attachments.append(data)
        else:
            items[key] = data
    scope = f"zotero-attachment:{client.backend}:{collection_key or 'all'}"
    candidates: list[IngestionCandidate] = []
    seen_paths: set[Path] = set()
    seen_parent_content: set[tuple[str, str]] = set()
    for attachment in attachments:
        attachment_key = str(attachment.get("key") or "").strip()
        parent_key = str(attachment.get("parentItem") or "").strip()
        if not attachment_key or not parent_key or parent_key not in items:
            continue
        attachment_dir = (root / attachment_key).resolve()
        try:
            attachment_dir.relative_to(root)
        except ValueError as exc:
            raise DomainQueryError(
                "knowledge_source_scope_escape",
                "Zotero attachment directory escaped the approved storage root",
            ) from exc
        if not attachment_dir.is_dir():
            continue
        filename = str(attachment.get("filename") or "").strip()
        exact = (attachment_dir / filename).resolve() if filename else None
        pdf_paths = (
            [exact]
            if exact is not None and exact.is_file() and exact.suffix.casefold() == ".pdf"
            else sorted(attachment_dir.glob("*.pdf"))
        )
        if len(pdf_paths) != 1:
            continue
        pdf_path = pdf_paths[0].resolve()
        if pdf_path in seen_paths:
            continue
        seen_paths.add(pdf_path)
        discovered = discover_file_sources(
            attachment_dir,
            source_type="zotero-attachment",
            root_id=attachment_key,
            extensions=PDF_EXTENSIONS,
        )
        source = next(
            (
                item
                for item in discovered
                if Path(item.locator).resolve() == pdf_path
            ),
            None,
        )
        if source is None:
            continue
        source_content_hash = str(
            source.metadata.get("source_content_hash") or source.digest
        )
        parent_content_key = (parent_key, source_content_hash)
        if parent_content_key in seen_parent_content:
            continue
        seen_parent_content.add(parent_content_key)
        parent = items[parent_key]
        title = re.sub(r"<[^>]+>", "", str(parent.get("title") or "")).strip()
        metadata = {
            **source.metadata,
            "backend": client.backend,
            "collection_key": collection_key,
            "item_key": parent_key,
            "attachment_key": attachment_key,
            "doi": str(parent.get("DOI") or "").strip(),
        }
        candidates.append(
            replace(
                source,
                source_ref=f"zotero-attachment:{parent_key}:{attachment_key}",
                source_type="paper",
                title=title or source.title,
                scope=scope,
                source_version=(
                    f"zotero-attachment:{attachment_key}:{source.source_version}"
                ),
                metadata=metadata,
            )
        )
    return candidates


def build_ingestion_plan(
    manifest: KnowledgeIngestionManifest,
    candidates: Iterable[IngestionCandidate],
    *,
    scopes: Iterable[str],
    detect_missing: bool = True,
    complete_scopes: Iterable[str] = (),
) -> IngestionPlan:
    selected_scopes = tuple(sorted({str(item).strip() for item in scopes if str(item).strip()}))
    existing = manifest.read()["sources"]
    candidate_map: dict[str, IngestionCandidate] = {}
    actions: list[IngestionAction] = []
    for candidate in candidates:
        if candidate.scope not in selected_scopes:
            raise DomainQueryError(
                "knowledge_source_scope_mismatch",
                f"candidate scope was not selected: {candidate.scope}",
            )
        if candidate.source_ref in candidate_map:
            raise DomainQueryError(
                "knowledge_source_duplicate",
                f"duplicate source ref discovered: {candidate.source_ref}",
            )
        candidate_map[candidate.source_ref] = candidate
        previous = existing.get(candidate.source_ref)
        if previous is None:
            action, reason = "add", "new_source"
        elif previous.get("content_hash") == f"sha256:{candidate.digest}":
            if _source_metadata_changed(previous, candidate):
                action, reason = "refresh", "source_metadata_changed"
            else:
                action, reason = "unchanged", "content_hash_matches"
        else:
            action, reason = "update", "content_hash_changed"
        actions.append(
            IngestionAction(
                action=action,
                source_ref=candidate.source_ref,
                reason=reason,
                candidate=candidate,
                previous=dict(previous) if isinstance(previous, dict) else None,
            )
        )
    if detect_missing:
        verified_scopes = set(complete_scopes)
        for source_ref, previous in existing.items():
            if (
                isinstance(previous, dict)
                and previous.get("scope") in selected_scopes
                and source_ref not in candidate_map
                and (not str(previous.get("scope") or "").startswith("zotero:") or previous.get("scope") in verified_scopes)
            ):
                actions.append(
                    IngestionAction(
                        action="remove",
                        source_ref=source_ref,
                        reason="source_missing_from_selected_scope",
                        previous=dict(previous),
                    )
                )
    order = {"add": 0, "update": 1, "refresh": 2, "unchanged": 3, "remove": 4}
    actions.sort(key=lambda item: (order[item.action], item.source_ref))
    return IngestionPlan(scopes=selected_scopes, actions=tuple(actions))


def apply_ingestion_plan(
    manifest: KnowledgeIngestionManifest,
    engine: IngestionEngine,
    plan: IngestionPlan,
    *,
    allow_deletes: bool = False,
    wait_timeout_seconds: float = 300.0,
    poll_interval_seconds: float = 1.0,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Apply a reviewed plan; deletion remains separately authorized."""

    results: list[dict[str, Any]] = []
    def record_result(result: dict[str, Any]) -> None:
        results.append(result)
        if progress is not None:
            progress(result)
    for action in plan.actions:
        if action.action == "unchanged":
            record_result({"source_ref": action.source_ref, "status": "unchanged"})
            continue
        if action.action == "remove":
            if not allow_deletes:
                record_result({"source_ref": action.source_ref, "status": "delete_deferred"})
                continue
            previous = action.previous or {}
            _delete_previous(engine, previous, wait_timeout_seconds)
            manifest.remove_structured_document(previous)
            manifest.remove(action.source_ref)
            record_result({"source_ref": action.source_ref, "status": "deleted"})
            continue
        candidate = action.candidate
        if candidate is None:
            raise DomainQueryError(
                "knowledge_ingestion_plan_invalid",
                f"{action.action} action is missing a candidate",
            )
        source = _knowledge_source(candidate)
        if action.action == "refresh":
            if not action.previous:
                raise DomainQueryError(
                    "knowledge_ingestion_plan_invalid",
                    "refresh action is missing the previous source record",
                )
            engine.refresh_source(source)
            previous = dict(action.previous)
            record = {
                **previous,
                "source_ref": candidate.source_ref,
                "source_type": candidate.source_type,
                "title": candidate.title,
                "scope": candidate.scope,
                "locator": candidate.locator,
                "source_version": candidate.source_version,
                "content_hash": f"sha256:{candidate.digest}",
                "engine_file_path": candidate.engine_file_path,
                "metadata": _candidate_metadata(candidate),
            }
            manifest.set(candidate.source_ref, record)
            record_result({"source_ref": candidate.source_ref, "status": "refreshed"})
            continue
        response = _submit_or_reconcile(engine, candidate, source, wait_timeout_seconds)
        track_id = str(response.get("track_id") or "").strip()
        document = _wait_for_processed(
            engine,
            track_id,
            timeout_seconds=wait_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        structured_document_path = ""
        if candidate.structured_document is not None:
            structured_document_path = manifest.store_structured_document(
                candidate.source_ref,
                candidate.digest,
                candidate.structured_document,
            )
        if action.action == "update" and action.previous:
            _delete_previous(engine, action.previous, wait_timeout_seconds)
        record = {
            "source_ref": candidate.source_ref,
            "source_type": candidate.source_type,
            "title": candidate.title,
            "scope": candidate.scope,
            "locator": candidate.locator,
            "source_version": candidate.source_version,
            "content_hash": f"sha256:{candidate.digest}",
            "engine_file_path": candidate.engine_file_path,
            "track_id": track_id,
            "document_id": document["id"],
            "status": "processed",
            "metadata": _candidate_metadata(candidate),
        }
        if structured_document_path:
            record["structured_document_path"] = structured_document_path
        manifest.set(candidate.source_ref, record)
        if action.action == "update" and action.previous:
            manifest.remove_structured_document(action.previous)
        record_result(
            {
                "source_ref": candidate.source_ref,
                "status": "updated" if action.action == "update" else "added",
                "track_id": track_id,
                "document_id": document["id"],
            }
        )
    return {
        "status": "applied",
        "counts": _count_results(results),
        "results": results,
    }


def extract_document_text(path: Path) -> str:
    suffix = Path(path).suffix.casefold()
    if suffix in TEXT_EXTENSIONS:
        try:
            return Path(path).read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DomainQueryError(
                "knowledge_source_encoding_error",
                f"text source is not UTF-8: {path}",
            ) from exc
    if suffix in PDF_EXTENSIONS:
        return parse_scientific_pdf(Path(path)).to_index_markdown()
    raise DomainQueryError(
        "knowledge_source_type_unsupported",
        f"unsupported source extension: {suffix}",
    )


def _wait_for_processed(
    engine: IngestionEngine,
    track_id: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    if not track_id:
        raise DomainQueryError(
            "knowledge_response_invalid",
            "ingestion response did not include a Track ID",
        )
    deadline = time.monotonic() + timeout_seconds
    while True:
        payload = engine.track_status(track_id)
        documents = payload.get("documents")
        if not isinstance(documents, list) or len(documents) > 1:
            raise DomainQueryError(
                "knowledge_response_invalid",
                "Track ID must resolve to at most one document while processing",
            )
        if not documents:
            if time.monotonic() >= deadline:
                raise DomainQueryError(
                    "knowledge_ingestion_timeout",
                    "LightRAG ingestion did not materialize before the deadline",
                    details={"track_id": track_id},
                )
            time.sleep(max(0.0, poll_interval_seconds))
            continue
        document = documents[0]
        status = str(document.get("status") or "").casefold()
        if status == "processed":
            if not str(document.get("id") or "").strip():
                raise DomainQueryError(
                    "knowledge_response_invalid",
                    "processed document is missing its ID",
                )
            return document
        if status == "failed":
            raise DomainQueryError(
                "knowledge_ingestion_failed",
                str(document.get("error_msg") or "LightRAG ingestion failed"),
                details={"track_id": track_id},
            )
        if time.monotonic() >= deadline:
            raise DomainQueryError(
                "knowledge_ingestion_timeout",
                "LightRAG ingestion did not finish before the deadline",
                details={"track_id": track_id},
            )
        time.sleep(max(0.0, poll_interval_seconds))


def _submit_or_reconcile(
    engine: IngestionEngine,
    candidate: IngestionCandidate,
    source: KnowledgeSource,
    wait_timeout_seconds: float,
) -> dict[str, Any]:
    """Recover exact content-hash documents left by a crashed/failed submit."""

    try:
        return engine.ingest_text(candidate.text, source)
    except DomainQueryError as exc:
        if (
            exc.code != "knowledge_engine_rejected"
            or exc.details.get("status") != 409
        ):
            raise
        existing = engine.find_document_by_path(candidate.engine_file_path)
        if existing is None:
            raise
        status = str(existing.get("status") or "").casefold()
        track_id = str(existing.get("track_id") or "").strip()
        document_id = str(existing.get("id") or "").strip()
        if status in {"pending", "processing", "processed"} and track_id:
            return {
                "status": f"reconciled_{status}",
                "message": "reused exact existing LightRAG document",
                "track_id": track_id,
            }
        if status != "failed" or not document_id:
            raise
        engine.delete_documents(
            {document_id: candidate.engine_file_path},
            delete_llm_cache=True,
            wait_timeout_seconds=wait_timeout_seconds,
        )
        return engine.ingest_text(candidate.text, source)


def _delete_previous(
    engine: IngestionEngine,
    previous: dict[str, Any],
    wait_timeout_seconds: float,
) -> None:
    document_id = str(previous.get("document_id") or "").strip()
    engine_path = str(previous.get("engine_file_path") or "").strip()
    if not document_id or not engine_path:
        raise DomainQueryError(
            "knowledge_ingestion_manifest_invalid",
            "previous source is missing document lifecycle identifiers",
        )
    deadline = time.monotonic() + wait_timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DomainQueryError(
                "knowledge_delete_timeout",
                "LightRAG remained busy before document deletion could start",
                details={"document_id": document_id},
            )
        try:
            engine.delete_documents(
                {document_id: engine_path},
                delete_llm_cache=True,
                wait_timeout_seconds=remaining,
            )
            return
        except DomainQueryError as exc:
            if exc.code != "knowledge_engine_busy":
                raise
            time.sleep(min(0.5, remaining))


def _zotero_markdown(item: dict[str, Any]) -> str:
    authors = ", ".join(item.get("authors") or []) or "Unknown"
    tags = ", ".join(item.get("tags") or []) or "None"
    return "\n\n".join(
        [
            f"# {item['title']}",
            f"- Zotero key: {item.get('item_key') or ''}",
            f"- Authors: {authors}",
            f"- Year: {item.get('year') or 'Unknown'}",
            f"- Publication: {item.get('publication') or item.get('publisher') or 'Unknown'}",
            f"- DOI: {item.get('doi') or 'None'}",
            f"- Tags: {tags}",
            "## Abstract",
            str(item.get("abstract") or "No abstract available."),
        ]
    )


def _title_from_text(text: str, *, fallback: str) -> str:
    frontmatter = _frontmatter_scalars(text)
    title = str(frontmatter.get("title") or "").strip()
    if title:
        return title[:200]
    lines = text.splitlines()
    start = _frontmatter_end(lines)
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            normalized = stripped.lstrip("#").strip()
            if normalized:
                return normalized[:200]
    for line in lines[start:]:
        normalized = line.strip()
        if normalized and normalized not in {"---", "***", "___"} and not normalized.startswith("<!--"):
            return normalized[:200]
    return fallback


def _frontmatter_scalars(text: str) -> dict[str, str]:
    lines = text.splitlines()
    end = _frontmatter_end(lines)
    if end == 0:
        return {}
    values: dict[str, str] = {}
    for line in lines[1 : end - 1]:
        if not line or line[:1].isspace() or line.lstrip().startswith("-") or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        normalized_key = key.strip().casefold()
        if not re.fullmatch(r"[a-z0-9_-]+", normalized_key):
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value and value not in {"|", ">", "[]", "{}", "null", "~"}:
            values[normalized_key] = value
    return values


def _frontmatter_end(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        return 0
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return index + 1
    return 0


def _candidate_metadata(candidate: IngestionCandidate) -> dict[str, Any]:
    metadata = dict(candidate.metadata)
    metadata["scope"] = candidate.scope
    return metadata


def _knowledge_source(candidate: IngestionCandidate) -> KnowledgeSource:
    return KnowledgeSource(
        engine_file_path=candidate.engine_file_path,
        source_ref=candidate.source_ref,
        source_type=candidate.source_type,
        title=candidate.title,
        content_hash=f"sha256:{candidate.digest}",
        source_version=candidate.source_version,
        metadata=_candidate_metadata(candidate),
    )


def _source_metadata_changed(
    previous: dict[str, Any],
    candidate: IngestionCandidate,
) -> bool:
    return any(
        (
            str(previous.get("source_type") or "") != candidate.source_type,
            str(previous.get("title") or "") != candidate.title,
            str(previous.get("scope") or "") != candidate.scope,
            str(previous.get("locator") or "") != candidate.locator,
            str(previous.get("source_version") or "") != candidate.source_version,
            dict(previous.get("metadata") or {}) != _candidate_metadata(candidate),
        )
    )


def _count_results(results: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        status = str(result.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts
