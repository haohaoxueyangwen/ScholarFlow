"""Small, dependency-free retrieval evaluation harness."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from .retrieval import EvidenceRetriever


@dataclass(frozen=True)
class RetrievalCase:
    query: str
    relevant_refs: tuple[str, ...]
    required_markers: tuple[str, ...] = ()
    forbidden_refs: tuple[str, ...] = ()
    language: str = ""
    category: str = ""


def evaluate_retriever(
    retriever: EvidenceRetriever,
    documents: Sequence[Any],
    cases: Sequence[RetrievalCase],
    *,
    k: int = 5,
) -> dict[str, Any]:
    """Return ranked retrieval, provenance, and source-span quality metrics."""

    normalized_k = max(1, int(k))
    per_case: list[dict[str, Any]] = []
    for case in cases:
        hits = retriever.retrieve(case.query, documents, limit=normalized_k)
        refs = [str(getattr(hit.source, "ref", "")) for hit in hits]
        relevant = set(case.relevant_refs)
        forbidden = set(case.forbidden_refs)
        hit_count = sum(1 for ref in refs if ref in relevant)
        first_relevant_rank = next(
            (index + 1 for index, ref in enumerate(refs) if ref in relevant),
            None,
        )
        competing_refs = (
            refs[: first_relevant_rank - 1]
            if first_relevant_rank is not None
            else refs
        )
        ideal_count = min(len(relevant), normalized_k)
        dcg = sum(
            1.0 / math.log2(index + 2)
            for index, ref in enumerate(refs)
            if ref in relevant
        )
        idcg = sum(1.0 / math.log2(index + 2) for index in range(ideal_count))
        traceable = all(
            bool(ref)
            and bool(getattr(hit.source, "record_json", ""))
            and bool(hit.provenance)
            for ref, hit in zip(refs, hits)
        )
        relevant_text = "\n".join(
            " ".join(
                str(getattr(hit.source, field, "") or "")
                for field in ("title", "detail", "snippet", "record_json")
            )
            for ref, hit in zip(refs, hits)
            if ref in relevant
        ).casefold()
        marker_matches = [
            marker
            for marker in case.required_markers
            if marker.casefold() in relevant_text
        ]
        per_case.append({
            "query": case.query,
            "language": case.language,
            "category": case.category,
            "retrieved_refs": refs,
            "relevant_refs": list(case.relevant_refs),
            "hit_count": hit_count,
            "recall_at_k": hit_count / len(relevant) if relevant else 1.0,
            "precision_at_k": hit_count / normalized_k,
            "ndcg_at_k": dcg / idcg if idcg else 1.0,
            "reciprocal_rank": (
                1.0 / first_relevant_rank if first_relevant_rank is not None else 0.0
            ),
            "forbidden_hit_count": sum(
                1 for ref in competing_refs if ref in forbidden
            ),
            "source_span_validity": (
                len(marker_matches) / len(case.required_markers)
                if case.required_markers
                else 1.0
            ),
            "citation_traceability": traceable,
        })
    count = len(per_case)
    return {
        "retriever": str(getattr(retriever, "name", "custom")),
        "k": normalized_k,
        "case_count": count,
        "recall_at_k": round(sum(item["recall_at_k"] for item in per_case) / count, 6) if count else 0.0,
        "precision_at_k": round(sum(item["precision_at_k"] for item in per_case) / count, 6) if count else 0.0,
        "ndcg_at_k": round(sum(item["ndcg_at_k"] for item in per_case) / count, 6) if count else 0.0,
        "mrr": round(sum(item["reciprocal_rank"] for item in per_case) / count, 6) if count else 0.0,
        "forbidden_hit_rate": round(
            sum(1 for item in per_case if item["forbidden_hit_count"] > 0) / count,
            6,
        ) if count else 0.0,
        "source_span_validity": round(
            sum(item["source_span_validity"] for item in per_case) / count,
            6,
        ) if count else 0.0,
        "citation_traceability": round(
            sum(1 for item in per_case if item["citation_traceability"]) / count,
            6,
        ) if count else 0.0,
        "cases": per_case,
    }
