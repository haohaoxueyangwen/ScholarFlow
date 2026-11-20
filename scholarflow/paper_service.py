"""Service layer that normalizes Zotero items into ScholarFlow papers.

The :class:`PaperService` is the only place that knows how to translate
between Zotero's loose JSON item shape and the strict ``Paper`` dataclass
defined in :mod:`scholarflow.models`.  All upstream code (tool runtime,
sync policy, vault automation, CLI) should depend on this service rather
than reading Zotero payloads directly.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable, Optional

from .contracts import StorageEnvelope
from .enums import PaperStatus
from .models import Experiment, Paper, ProjectStage, Task, canonical_paper_id
from .storage import ScholarFlowStore
from .zotero_client import ZoteroClient, ZoteroClientResult


class PaperServiceError(RuntimeError):
    """Raised when a Zotero payload cannot be normalized into a Paper."""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _coerce_authors(payload: Any) -> list[str]:
    authors: list[str] = []
    if not isinstance(payload, list):
        return authors
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        first = _clean(entry.get("firstName"))
        last = _clean(entry.get("lastName"))
        name = _clean(entry.get("name"))
        if name:
            authors.append(name)
        elif first or last:
            authors.append(_clean(f"{first} {last}"))
    return authors


def _coerce_year(payload: Any, *, date_text: str = "") -> Optional[int]:
    candidate = payload
    if candidate is None and date_text:
        candidate = date_text[:4]
    if candidate is None:
        return None
    try:
        value = int(str(candidate)[:4])
    except (TypeError, ValueError):
        return None
    if value < 1000 or value > 9999:
        return None
    return value


def _coerce_tags(payload: Any) -> list[str]:
    tags: list[str] = []
    if not isinstance(payload, list):
        return tags
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        tag = _clean(entry.get("tag"))
        if tag:
            tags.append(tag)
    return tags


def _coerce_collections(payload: Any) -> list[str]:
    collections: list[str] = []
    if not isinstance(payload, list):
        return collections
    for entry in payload:
        if isinstance(entry, str):
            key = _clean(entry)
            if key:
                collections.append(key)
            continue
        if not isinstance(entry, dict):
            continue
        key = _clean(entry.get("collectionKey") or entry.get("key") or entry.get("name"))
        if key:
            collections.append(key)
    return collections


def normalize_zotero_item(item: dict[str, Any]) -> dict[str, Any]:
    """Return a flat, schema-aware dict derived from a Zotero item payload.

    The output is intentionally JSON-serializable so it can be embedded in
    logs, traces, and KB note metadata without further work.
    """

    if not isinstance(item, dict):
        raise PaperServiceError("zotero item must be a dict")
    data = item.get("data") if isinstance(item.get("data"), dict) else item
    item_type = _clean(data.get("itemType") or item.get("itemType")) or "unknown"
    item_key = _clean(data.get("key") or item.get("key") or item.get("itemKey"))
    title = _clean(data.get("title")) or _clean(item.get("title"))
    if not title:
        title = f"untitled-{item_key or 'unknown'}"
    abstract = _clean(data.get("abstractNote")) or _clean(item.get("abstractNote"))
    date_text = _clean(data.get("date"))
    year = _coerce_year(data.get("date") or data.get("year"), date_text=date_text)
    authors = _coerce_authors(data.get("creators") or data.get("authors"))
    tags = _coerce_tags(data.get("tags"))
    collections = _coerce_collections(data.get("collections"))
    publication = _clean(data.get("publicationTitle") or data.get("proceedingsTitle") or data.get("bookTitle"))
    publisher = _clean(data.get("publisher"))
    doi = _clean(data.get("DOI") or data.get("doi"))
    url = _clean(data.get("url") or item.get("links", {}).get("self") if isinstance(item.get("links"), dict) else "")
    item_version = item.get("version") or data.get("version")
    source_label = publication or publisher or doi or "zotero"

    return {
        "item_key": item_key,
        "item_type": item_type,
        "title": title,
        "authors": authors,
        "year": year,
        "date": date_text,
        "publication": publication,
        "publisher": publisher,
        "doi": doi,
        "url": url,
        "abstract": abstract,
        "tags": tags,
        "collections": collections,
        "source_label": source_label,
        "item_version": item_version,
    }


def paper_from_normalized(
    normalized: dict[str, Any],
    *,
    paper_id: Optional[str] = None,
    status: PaperStatus = PaperStatus.TO_READ,
    summary: Optional[str] = None,
    relevance: Optional[str] = None,
    related_tasks: Optional[list[str]] = None,
    related_experiments: Optional[list[str]] = None,
    source: str = "zotero",
    obsidian_note: Optional[str] = None,
) -> Paper:
    if not isinstance(normalized, dict):
        raise PaperServiceError("normalized payload must be a dict")
    item_key = _clean(normalized.get("item_key"))
    requested_id = paper_id or (f"zotero:{item_key}" if item_key else "")
    resolved_id = canonical_paper_id(requested_id)
    if not resolved_id:
        raise PaperServiceError("zotero item is missing a key; cannot derive paper_id")
    return Paper(
        paper_id=resolved_id,
        title=str(normalized.get("title") or resolved_id),
        authors=list(normalized.get("authors") or []),
        year=normalized.get("year"),
        source=source if source else "zotero",
        external_ids={
            key: value
            for key, value in {
                "zotero": item_key,
                "doi": _clean(normalized.get("doi")),
            }.items()
            if value
        },
        status=status,
        summary=summary or (normalized.get("abstract") or None),
        relevance=relevance,
        related_tasks=list(related_tasks or []),
        related_experiments=list(related_experiments or []),
        obsidian_note=obsidian_note,
    )


def paper_from_zotero(
    item: dict[str, Any],
    *,
    paper_id: Optional[str] = None,
    status: PaperStatus = PaperStatus.TO_READ,
    related_tasks: Optional[list[str]] = None,
    related_experiments: Optional[list[str]] = None,
    source: str = "zotero",
) -> Paper:
    normalized = normalize_zotero_item(item)
    return paper_from_normalized(
        normalized,
        paper_id=paper_id,
        status=status,
        related_tasks=related_tasks,
        related_experiments=related_experiments,
        source=source,
    )


def summarize_collection(normalized_items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = [item for item in normalized_items if isinstance(item, dict)]
    year_counter: dict[str, int] = {}
    tag_counter: dict[str, int] = {}
    type_counter: dict[str, int] = {}
    author_counter: dict[str, int] = {}
    for entry in items:
        year = entry.get("year")
        if year is not None:
            year_counter[str(year)] = year_counter.get(str(year), 0) + 1
        for tag in entry.get("tags") or []:
            tag_counter[str(tag)] = tag_counter.get(str(tag), 0) + 1
        item_type = str(entry.get("item_type") or "unknown")
        type_counter[item_type] = type_counter.get(item_type, 0) + 1
        for author in entry.get("authors") or []:
            author_counter[str(author)] = author_counter.get(str(author), 0) + 1

    def _top(counter: dict[str, int], *, limit: int = 5) -> list[dict[str, Any]]:
        ranked = sorted(counter.items(), key=lambda item: item[1], reverse=True)
        return [{"name": key, "count": value} for key, value in ranked[:limit]]

    return {
        "count": len(items),
        "top_years": _top(year_counter),
        "top_tags": _top(tag_counter),
        "top_types": _top(type_counter),
        "top_authors": _top(author_counter),
    }


class PaperService:
    """High-level operations combining a Zotero client and the local store."""

    def __init__(self, store: ScholarFlowStore, *, zotero_client: ZoteroClient) -> None:
        self.store = store
        self.zotero_client = zotero_client

    def list_zotero_collections(self) -> ZoteroClientResult:
        return self.zotero_client.list_collections()

    def list_zotero_items(
        self,
        *,
        collection_key: Optional[str] = None,
        limit: int = 25,
    ) -> ZoteroClientResult:
        return self.zotero_client.list_items(collection_key=collection_key, limit=limit)

    def import_zotero_items(
        self,
        *,
        collection_key: Optional[str] = None,
        limit: int = 25,
        related_task_ids: Optional[list[str]] = None,
        related_experiment_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        response = self.zotero_client.list_items(collection_key=collection_key, limit=limit)
        if response.status != "ok":
            return {
                "status": response.status,
                "backend": response.backend,
                "imported": 0,
                "skipped": 0,
                "error": response.error,
                "items": [],
            }
        items = response.data if isinstance(response.data, list) else []
        imported = 0
        skipped = 0
        envelopes: list[StorageEnvelope] = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                skipped += 1
                continue
            try:
                envelope = self.upsert_paper(
                    raw_item,
                    related_task_ids=related_task_ids,
                    related_experiment_ids=related_experiment_ids,
                )
            except PaperServiceError:
                skipped += 1
                continue
            envelopes.append(envelope)
            if envelope.operation == "create":
                imported += 1
            else:
                skipped += 1
        return {
            "status": "ok",
            "backend": response.backend,
            "imported": imported,
            "skipped": skipped,
            "count": len(items),
            "items": [envelope.to_dict() for envelope in envelopes],
        }

    def upsert_paper(
        self,
        raw_item: dict[str, Any],
        *,
        paper_id: Optional[str] = None,
        status: PaperStatus = PaperStatus.TO_READ,
        related_task_ids: Optional[list[str]] = None,
        related_experiment_ids: Optional[list[str]] = None,
    ) -> StorageEnvelope:
        try:
            paper = paper_from_zotero(
                raw_item,
                paper_id=paper_id,
                status=status,
                related_tasks=related_task_ids,
                related_experiments=related_experiment_ids,
            )
        except PaperServiceError:
            raise
        existing = self.store.get_paper(paper.paper_id)
        if existing is None:
            envelope = self.store.create_paper(paper)
        else:
            merged = _merge_paper(existing, paper)
            envelope = self.store.update_paper(paper.paper_id, **merged)
        self._propagate_links(paper.paper_id, related_task_ids=related_task_ids, related_experiment_ids=related_experiment_ids)
        return envelope

    def attach_to_task(self, paper_id: str, task_id: str) -> StorageEnvelope:
        paper = self.store.get_paper(paper_id)
        if paper is None:
            raise PaperServiceError(f"paper '{paper_id}' was not found")
        task = self.store.get_task(task_id)
        if task is None:
            raise PaperServiceError(f"task '{task_id}' was not found")
        new_related = _merge_unique(paper.related_tasks, [task_id])
        canonical_id = paper.paper_id
        paper_envelope = self.store.update_paper(canonical_id, related_tasks=new_related)
        new_task_papers = _merge_unique(task.related_papers, [canonical_id])
        task_envelope = self.store.update_task(task_id, related_papers=new_task_papers)
        return StorageEnvelope(
            object_type="paper_task_link",
            operation="attach",
            object_id=f"{canonical_id}->{task_id}",
            record={
                "paper": paper_envelope.to_dict(),
                "task": task_envelope.to_dict(),
            },
        )

    def attach_to_experiment(self, paper_id: str, experiment_id: str) -> StorageEnvelope:
        paper = self.store.get_paper(paper_id)
        if paper is None:
            raise PaperServiceError(f"paper '{paper_id}' was not found")
        experiment = self.store.get_experiment(experiment_id)
        if experiment is None:
            raise PaperServiceError(f"experiment '{experiment_id}' was not found")
        new_related = _merge_unique(paper.related_experiments, [experiment_id])
        canonical_id = paper.paper_id
        paper_envelope = self.store.update_paper(canonical_id, related_experiments=new_related)
        return StorageEnvelope(
            object_type="paper_experiment_link",
            operation="attach",
            object_id=f"{canonical_id}->{experiment_id}",
            record={
                "paper": paper_envelope.to_dict(),
                "experiment": experiment_id,
            },
        )

    def add_tags(self, paper_id: str, tags: Iterable[str]) -> dict[str, Any]:
        paper = self.store.get_paper(paper_id)
        if paper is None:
            raise PaperServiceError(f"paper '{paper_id}' was not found")
        cleaned_tags = [_clean(tag) for tag in tags if _clean(tag)]
        # The Paper dataclass stores tags inlined into source metadata; we
        # also forward them to Zotero so the change is mirrored in the source
        # of truth when a real backend is configured.
        upstream = self.zotero_client.add_tags(_extract_item_key(paper), cleaned_tags)
        new_tags = _merge_unique(_tags_from_paper(paper), cleaned_tags)
        new_source = _append_tags_to_source(paper.source, new_tags)
        envelope = self.store.update_paper(paper_id, source=new_source)
        return {
            "status": "ok",
            "paper_envelope": envelope.to_dict(),
            "upstream": upstream.to_dict(),
            "tags": new_tags,
        }

    def summarize_papers(self, paper_ids: Optional[list[str]] = None) -> dict[str, Any]:
        if paper_ids:
            records = [self.store.get_paper(paper_id) for paper_id in paper_ids]
        else:
            records = self.store.list_papers()
        normalized_items = []
        for record in records:
            if record is None:
                continue
            normalized_items.append(
                {
                    "item_key": _extract_item_key(record),
                    "item_type": "paper",
                    "title": record.title,
                    "year": record.year,
                    "authors": list(record.authors),
                    "tags": _tags_from_paper(record),
                }
            )
        summary = summarize_collection(normalized_items)
        summary["status_counts"] = _status_distribution(records)
        summary["count"] = len(records)
        return summary

    def _propagate_links(
        self,
        paper_id: str,
        *,
        related_task_ids: Optional[list[str]] = None,
        related_experiment_ids: Optional[list[str]] = None,
    ) -> None:
        for task_id in related_task_ids or []:
            task = self.store.get_task(task_id)
            if task is None:
                continue
            if paper_id in task.related_papers:
                continue
            self.store.update_task(task_id, related_papers=_merge_unique(task.related_papers, [paper_id]))


def _merge_paper(existing: Paper, incoming: Paper) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if (not existing.title or existing.title.startswith("untitled-")) and incoming.title:
        changes["title"] = incoming.title
    if not existing.authors and incoming.authors:
        changes["authors"] = list(incoming.authors)
    if existing.year is None and incoming.year is not None:
        changes["year"] = incoming.year
    if (not existing.source or existing.source == "zotero") and incoming.source and incoming.source != "zotero":
        changes["source"] = incoming.source
    if not existing.summary and incoming.summary:
        changes["summary"] = incoming.summary
    merged_tasks = _merge_unique(existing.related_tasks, incoming.related_tasks)
    if merged_tasks != list(existing.related_tasks):
        changes["related_tasks"] = merged_tasks
    merged_experiments = _merge_unique(existing.related_experiments, incoming.related_experiments)
    if merged_experiments != list(existing.related_experiments):
        changes["related_experiments"] = merged_experiments
    return changes


def _merge_unique(existing: Iterable[str], incoming: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for value in list(existing or []) + list(incoming or []):
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        merged.append(text)
    return merged


def _tags_from_paper(paper: Paper) -> list[str]:
    source = paper.source or ""
    if "#tags=" not in source:
        return []
    tail = source.split("#tags=", 1)[1]
    return [tag for tag in tail.split(",") if tag]


def _append_tags_to_source(source: Optional[str], tags: list[str]) -> str:
    base_source = (source or "zotero").split("#tags=", 1)[0]
    if not tags:
        return base_source
    return f"{base_source}#tags={','.join(tags)}"


def _extract_item_key(paper: Paper) -> str:
    item_key = paper.external_ids.get("zotero")
    if item_key:
        return item_key
    if paper.paper_id.startswith("paper-zotero-"):
        return paper.paper_id.removeprefix("paper-zotero-")
    if paper.paper_id.startswith("zotero:"):
        return paper.paper_id.split(":", 1)[1]
    return paper.paper_id


def _status_distribution(records: Iterable[Paper]) -> dict[str, int]:
    counter: dict[str, int] = {}
    for record in records:
        if record is None:
            continue
        status_value = str(record.status.value)
        counter[status_value] = counter.get(status_value, 0) + 1
    return counter


def coerce_paper_for_update(existing: Paper, **changes: Any) -> Paper:
    """Helper used by tests / CLI to build a frozen Paper update payload."""

    return replace(existing, **changes)
