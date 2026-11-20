"""Read-only evidence bundle helpers for Pi-native research workflows."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import json
import re
from typing import Any

from .domain_queries import DomainQueryError, ScholarFlowQueryService
from .retrieval import EvidenceRetriever, LexicalEvidenceRetriever
from .storage import ScholarFlowStore

READ_ONLY_RULE = (
    "Evidence bundles are read-only: they may retrieve, summarize, and cite, "
    "but they must not emit ScholarFlow proposals or commits directly."
)


@dataclass(frozen=True)
class EvidenceSource:
    """One citation-ready source surfaced by the evidence layer."""

    ref: str
    source_type: str
    title: str
    detail: str = ""
    snippet: str = ""
    record_json: str = ""
    retrieval_score: float = 0.0
    retrieval_channels: tuple[str, ...] = field(default_factory=tuple)
    retrieval_provenance: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EvidenceBundle:
    """Typed, immutable evidence payload consumed by research-output workflows."""

    query: str
    summary: str = ""
    citations: tuple[str, ...] = field(default_factory=tuple)
    sources: tuple[EvidenceSource, ...] = field(default_factory=tuple)
    read_only: bool = True
    governance: str = READ_ONLY_RULE
    retriever: str = "lexical"
    retrieval_trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    unresolved_refs: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "summary": self.summary,
            "citations": list(self.citations),
            "sources": [
                {
                    **asdict(item),
                    "retrieval_channels": list(item.retrieval_channels),
                    "retrieval_provenance": list(item.retrieval_provenance),
                }
                for item in self.sources
            ],
            "read_only": self.read_only,
            "governance": self.governance,
            "retriever": self.retriever,
            "retrieval_trace": [dict(item) for item in self.retrieval_trace],
            "count": len(self.sources),
            "unresolved_refs": list(self.unresolved_refs),
        }


def prepare_evidence_bundle(
    store: ScholarFlowStore,
    *,
    query: str,
    limit: int = 5,
    project_stage_id: str | None = None,
    retriever: EvidenceRetriever | None = None,
) -> EvidenceBundle:
    """Collect a bounded, citation-ready, read-only evidence bundle."""

    normalized_stage_id = str(project_stage_id or "").strip() or None
    normalized_query = str(query or "").strip() or normalized_stage_id or ""
    if not normalized_query:
        raise DomainQueryError(
            "invalid_argument",
            "query is required",
            details={"field": "query"},
        )
    normalized_limit = _normalize_limit(limit)
    queries = ScholarFlowQueryService(store)
    unresolved_refs: list[str] = []

    if normalized_stage_id:
        stage = queries.get_project_stage(normalized_stage_id)
        stages = [stage]
        tasks = queries.search_tasks(project_stage_id=normalized_stage_id, limit=100)["items"]
        experiments = queries.search_experiments(project_stage_id=normalized_stage_id, limit=100)["items"]
        artifacts = queries.search_research_artifacts(project_stage_id=normalized_stage_id, limit=100)["items"]
        task_ids = {item["task_id"] for item in tasks}
        experiment_ids = {item["experiment_id"] for item in experiments}
        linked_paper_ids = {
            paper_id
            for item in tasks
            for paper_id in item.get("related_papers", [])
        }
        papers = [
            item
            for item in queries.search_papers(limit=100)["items"]
            if item["paper_id"] in linked_paper_ids
            or task_ids.intersection(item.get("related_tasks", []))
            or experiment_ids.intersection(item.get("related_experiments", []))
        ]
        # Explicit stage links are authoritative scope, not merely search text.
        # Resolve them by ID so a library's first-page limit cannot hide them.
        by_paper_id = {item["paper_id"]: item for item in papers}
        for ref in stage.get("evidence_refs", []):
            if not isinstance(ref, str) or not ref.startswith("paper:"):
                continue
            paper_id = ref.split(":", 1)[1]
            paper = store.get_paper(paper_id)
            if paper is None:
                unresolved_refs.append(ref)
            else:
                by_paper_id[paper_id] = paper.to_dict()
        papers = list(by_paper_id.values())
        scoped_refs = {
            f"project_stage:{normalized_stage_id}",
            *(f"task:{item_id}" for item_id in task_ids),
            *(f"experiment:{item_id}" for item_id in experiment_ids),
            *(f"paper:{item['paper_id']}" for item in papers),
            *(f"research_artifact:{item['artifact_id']}" for item in artifacts),
        }
        relations = [
            item
            for item in queries.search_knowledge_relations(limit=100)["items"]
            if item.get("subject_ref") in scoped_refs or item.get("object_ref") in scoped_refs
        ]
    else:
        papers = queries.search_papers(query=normalized_query, limit=normalized_limit)["items"]
        artifacts = queries.search_research_artifacts(query=normalized_query, limit=normalized_limit)["items"]
        experiments = queries.search_experiments(query=normalized_query, limit=normalized_limit)["items"]
        tasks = queries.search_tasks(query=normalized_query, limit=normalized_limit)["items"]
        stages = queries.search_project_stages(query=normalized_query, limit=normalized_limit)["items"]
        relations = queries.search_knowledge_relations(query=normalized_query, limit=normalized_limit)["items"]

    candidates = [
        *(_paper_source(item) for item in papers),
        *(_artifact_source(item) for item in artifacts),
        *(_experiment_source(item) for item in experiments),
        *(_task_source(item) for item in tasks),
        *(_stage_source(item) for item in stages),
        *(_relation_source(item) for item in relations),
    ]
    effective_retriever = retriever or LexicalEvidenceRetriever()
    ranked = effective_retriever.retrieve(
        normalized_query,
        candidates,
        limit=normalized_limit,
    )
    sources = tuple(
        replace(
            item.source,
            retrieval_score=float(item.score),
            retrieval_channels=tuple(item.channels),
            retrieval_provenance=tuple(item.provenance),
        )
        for item in ranked
    )
    citations = tuple(source.ref for source in sources)
    summary = _build_summary(
        query=normalized_query,
        selected_count=len(sources),
        total_counts={
            "papers": len(papers),
            "artifacts": len(artifacts),
            "experiments": len(experiments),
            "tasks": len(tasks),
            "project_stages": len(stages),
            "relations": len(relations),
        },
    )
    return EvidenceBundle(
        query=normalized_query,
        summary=summary,
        citations=citations,
        sources=sources,
        retriever=str(getattr(effective_retriever, "name", "custom")),
        retrieval_trace=tuple(item.to_trace() for item in ranked),
        unresolved_refs=tuple(dict.fromkeys(unresolved_refs)),
    )


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


def _diversified_rank(candidates: list[EvidenceSource], query: str, limit: int) -> list[EvidenceSource]:
    """Rank across all object types while reducing single-type crowd-out."""

    terms = set(re.findall(r"[A-Za-z0-9]+", query.casefold()))
    scored = [(source, _source_score(source, terms, query)) for source in candidates]
    selected: list[EvidenceSource] = []
    type_counts: dict[str, int] = {}
    while scored and len(selected) < limit:
        source, _ = max(
            scored,
            key=lambda item: (
                item[1] / (1.0 + type_counts.get(item[0].source_type, 0)),
                item[1],
                item[0].ref,
            ),
        )
        scored = [item for item in scored if item[0].ref != source.ref]
        selected.append(source)
        type_counts[source.source_type] = type_counts.get(source.source_type, 0) + 1
    return selected


def _source_score(source: EvidenceSource, terms: set[str], query: str) -> float:
    title = source.title.casefold()
    detail = source.detail.casefold()
    snippet = source.snippet.casefold()
    score = sum(3.0 for term in terms if term in title)
    score += sum(1.5 for term in terms if term in detail)
    score += sum(1.0 for term in terms if term in snippet)
    if query.casefold() in f"{title} {detail} {snippet}":
        score += 3.0
    return score


def _paper_source(item: dict[str, Any]) -> EvidenceSource:
    authors = ", ".join(item.get("authors") or [])
    detail = " | ".join(
        part
        for part in [item.get("paper_id"), item.get("status"), str(item.get("year") or "unknown")]
        if part
    )
    snippet = item.get("summary") or item.get("relevance") or authors
    return EvidenceSource(
        ref=f"paper:{item['paper_id']}",
        source_type="paper",
        title=str(item.get("title") or item["paper_id"]),
        detail=detail,
        snippet=str(snippet or ""),
        record_json=_record_json(item),
    )


def _artifact_source(item: dict[str, Any]) -> EvidenceSource:
    detail = " | ".join(
        part for part in [item.get("artifact_id"), item.get("kind"), item.get("status")] if part
    )
    return EvidenceSource(
        ref=f"research_artifact:{item['artifact_id']}",
        source_type="research_artifact",
        title=str(item.get("title") or item["artifact_id"]),
        detail=detail,
        snippet=_truncate(str(item.get("content") or ""), 160),
        record_json=_record_json(item),
    )


def _experiment_source(item: dict[str, Any]) -> EvidenceSource:
    detail = " | ".join(
        part for part in [item.get("experiment_id"), item.get("status"), item.get("project_stage_id")] if part
    )
    return EvidenceSource(
        ref=f"experiment:{item['experiment_id']}",
        source_type="experiment",
        title=str(item.get("title") or item["experiment_id"]),
        detail=detail,
        snippet=str(item.get("result_summary") or item.get("goal") or ""),
        record_json=_record_json(item),
    )


def _task_source(item: dict[str, Any]) -> EvidenceSource:
    detail = " | ".join(
        part for part in [item.get("task_id"), item.get("status"), item.get("project_stage_id")] if part
    )
    return EvidenceSource(
        ref=f"task:{item['task_id']}",
        source_type="task",
        title=str(item.get("title") or item["task_id"]),
        detail=detail,
        snippet=str(item.get("description") or ""),
        record_json=_record_json(item),
    )


def _stage_source(item: dict[str, Any]) -> EvidenceSource:
    detail = " | ".join(part for part in [item.get("stage_id"), item.get("status")] if part)
    return EvidenceSource(
        ref=f"project_stage:{item['stage_id']}",
        source_type="project_stage",
        title=str(item.get("title") or item["stage_id"]),
        detail=detail,
        snippet=str(item.get("goal") or item.get("progress_note") or ""),
        record_json=_record_json(item),
    )


def _relation_source(item: dict[str, Any]) -> EvidenceSource:
    detail = " | ".join(
        part for part in [item.get("relation_id"), item.get("predicate"), item.get("subject_ref"), item.get("object_ref")] if part
    )
    return EvidenceSource(
        ref=f"knowledge_relation:{item['relation_id']}",
        source_type="knowledge_relation",
        title=str(item.get("relation_id") or "relation"),
        detail=detail,
        snippet=str(item.get("note") or ""),
        record_json=_record_json(item),
    )


def _record_json(item: dict[str, Any]) -> str:
    """Return a stable, immutable-by-value snapshot for model grounding."""

    return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _build_summary(
    *,
    query: str,
    selected_count: int,
    total_counts: dict[str, int],
) -> str:
    category_bits = [
        f"{label}={count}"
        for label, count in total_counts.items()
        if count > 0
    ]
    if not category_bits:
        return (
            f'No stored ScholarFlow evidence matched "{query}". '
            "The evidence layer remains read-only and cannot create missing records."
        )
    return (
        f'Prepared {selected_count} read-only evidence sources for "{query}". '
        f"Matches: {', '.join(category_bits)}. "
        "Use these citations to ground synthesis or planning before any separate proposal/apply step."
    )


def _truncate(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
