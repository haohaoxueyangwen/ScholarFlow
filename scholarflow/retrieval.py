"""Pluggable retrieval seams for citation-grounded ScholarFlow evidence.

The domain store remains the source of truth. This module only ranks already
materialized ``EvidenceSource``-like documents and carries provenance for each
ranked result. A vector index or reranker is optional; without one the lexical
retriever is the stable fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
from typing import Any, Callable, Protocol, Sequence


@dataclass(frozen=True)
class VectorHit:
    """One result returned by a vector index adapter."""

    ref: str
    score: float
    provenance: str = "vector_index"


@dataclass(frozen=True)
class RetrievalCandidate:
    """Ranked document plus channel scores and citation provenance."""

    source: Any
    score: float
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    channels: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()

    def to_trace(self) -> dict[str, Any]:
        return {
            "ref": str(getattr(self.source, "ref", "")),
            "score": round(float(self.score), 6),
            "lexical_score": round(float(self.lexical_score), 6),
            "semantic_score": round(float(self.semantic_score), 6),
            "channels": list(self.channels),
            "provenance": list(self.provenance),
        }


class EvidenceRetriever(Protocol):
    """Common interface for lexical, hybrid, or future hosted retrievers."""

    name: str

    def retrieve(
        self,
        query: str,
        documents: Sequence[Any],
        *,
        limit: int,
    ) -> tuple[RetrievalCandidate, ...]: ...


class VectorIndex(Protocol):
    """Optional semantic channel; implementations must return stable refs."""

    name: str

    def search(
        self,
        query: str,
        documents: Sequence[Any],
        *,
        limit: int,
    ) -> Sequence[VectorHit]: ...


class EvidenceReranker(Protocol):
    """Optional final reranking hook over channel-combined candidates."""

    name: str

    def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievalCandidate],
        *,
        limit: int,
    ) -> Sequence[RetrievalCandidate]: ...


def _terms(query: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9]+", str(query or "").casefold()))


def _document_text(document: Any) -> tuple[str, str, str, str]:
    return (
        str(getattr(document, "title", "") or ""),
        str(getattr(document, "detail", "") or ""),
        str(getattr(document, "snippet", "") or ""),
        str(getattr(document, "ref", "") or ""),
    )


def lexical_score(document: Any, query: str) -> float:
    """Score a document using the stable ScholarFlow lexical baseline."""

    title, detail, snippet, _ref = _document_text(document)
    terms = _terms(query)
    title_folded = title.casefold()
    detail_folded = detail.casefold()
    snippet_folded = snippet.casefold()
    score = sum(3.0 for term in terms if term in title_folded)
    score += sum(1.5 for term in terms if term in detail_folded)
    score += sum(1.0 for term in terms if term in snippet_folded)
    if str(query or "").casefold() in f"{title_folded} {detail_folded} {snippet_folded}":
        score += 3.0
    return score


def _diversify(candidates: Sequence[RetrievalCandidate], limit: int) -> tuple[RetrievalCandidate, ...]:
    if int(limit) <= 0:
        return ()
    remaining = list(candidates)
    selected: list[RetrievalCandidate] = []
    type_counts: dict[str, int] = {}
    while remaining and len(selected) < max(1, int(limit)):
        chosen = max(
            remaining,
            key=lambda item: (
                item.score / (1.0 + type_counts.get(str(getattr(item.source, "source_type", "")), 0)),
                item.score,
                str(getattr(item.source, "ref", "")),
            ),
        )
        remaining.remove(chosen)
        selected.append(chosen)
        source_type = str(getattr(chosen.source, "source_type", ""))
        type_counts[source_type] = type_counts.get(source_type, 0) + 1
    return tuple(selected)


class LexicalEvidenceRetriever:
    """Deterministic lexical retriever retained as the no-dependency fallback."""

    name = "lexical"

    def retrieve(
        self,
        query: str,
        documents: Sequence[Any],
        *,
        limit: int,
    ) -> tuple[RetrievalCandidate, ...]:
        candidates = [
            self._candidate(document, query)
            for document in documents
        ]
        return _diversify(candidates, limit)

    @staticmethod
    def _candidate(document: Any, query: str) -> RetrievalCandidate:
        score = lexical_score(document, query)
        return RetrievalCandidate(
            source=document,
            score=score,
            lexical_score=score,
            channels=("lexical",),
            provenance=("lexical",),
        )


class WeightedScoreReranker:
    """Small default reranker that keeps the channel score auditable."""

    name = "weighted-score"

    def __init__(self, *, exact_match_bonus: float = 0.0) -> None:
        self.exact_match_bonus = float(exact_match_bonus)

    def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievalCandidate],
        *,
        limit: int,
    ) -> Sequence[RetrievalCandidate]:
        query_folded = str(query or "").casefold()
        reranked: list[RetrievalCandidate] = []
        for candidate in candidates:
            title, detail, snippet, _ref = _document_text(candidate.source)
            bonus = self.exact_match_bonus if query_folded and query_folded in f"{title} {detail} {snippet}".casefold() else 0.0
            if not bonus:
                reranked.append(candidate)
                continue
            reranked.append(
                RetrievalCandidate(
                    source=candidate.source,
                    score=candidate.score + bonus,
                    lexical_score=candidate.lexical_score,
                    semantic_score=candidate.semantic_score,
                    channels=tuple(dict.fromkeys((*candidate.channels, self.name))),
                    provenance=tuple(dict.fromkeys((*candidate.provenance, self.name))),
                )
            )
        return sorted(
            reranked,
            key=lambda item: (item.score, str(getattr(item.source, "ref", ""))),
            reverse=True,
        )[: max(1, int(limit))]


class HybridEvidenceRetriever:
    """Combine lexical evidence with an optional vector index and reranker."""

    def __init__(
        self,
        *,
        vector_index: VectorIndex | None = None,
        reranker: EvidenceReranker | None = None,
        lexical_weight: float = 0.65,
        semantic_weight: float = 0.35,
    ) -> None:
        if lexical_weight < 0 or semantic_weight < 0 or lexical_weight + semantic_weight <= 0:
            raise ValueError("retrieval weights must be non-negative and not both zero")
        self.vector_index = vector_index
        self.reranker = reranker
        self.lexical_weight = float(lexical_weight)
        self.semantic_weight = float(semantic_weight)
        self.name = "hybrid" if vector_index is not None else "hybrid-lexical-fallback"

    def retrieve(
        self,
        query: str,
        documents: Sequence[Any],
        *,
        limit: int,
    ) -> tuple[RetrievalCandidate, ...]:
        if not documents:
            return ()
        lexical = LexicalEvidenceRetriever().retrieve(query, documents, limit=len(documents))
        lexical_by_ref = {str(getattr(item.source, "ref", "")): item for item in lexical}
        vector_hits: Sequence[VectorHit] = ()
        if self.vector_index is not None:
            vector_hits = self.vector_index.search(query, documents, limit=max(int(limit), 1) * 3)
        semantic_by_ref = {
            str(hit.ref): hit
            for hit in vector_hits
            if str(hit.ref) in lexical_by_ref
        }
        max_lexical = max((item.lexical_score for item in lexical), default=0.0)
        max_semantic = max((float(hit.score) for hit in semantic_by_ref.values()), default=0.0)
        semantic_enabled = bool(semantic_by_ref)
        total_weight = self.lexical_weight + (self.semantic_weight if semantic_enabled else 0.0)
        candidates: list[RetrievalCandidate] = []
        for item in lexical:
            ref = str(getattr(item.source, "ref", ""))
            vector_hit = semantic_by_ref.get(ref)
            semantic_score = float(vector_hit.score) if vector_hit else 0.0
            lexical_norm = item.lexical_score / max_lexical if max_lexical else 0.0
            semantic_norm = semantic_score / max_semantic if max_semantic else 0.0
            combined = (
                self.lexical_weight * lexical_norm
                + (self.semantic_weight * semantic_norm if semantic_enabled else 0.0)
            ) / total_weight
            channels = list(item.channels)
            provenance = list(item.provenance)
            if vector_hit:
                channels.append("vector")
                provenance.append(vector_hit.provenance)
            candidates.append(
                RetrievalCandidate(
                    source=item.source,
                    score=combined,
                    lexical_score=item.lexical_score,
                    semantic_score=semantic_score,
                    channels=tuple(dict.fromkeys(channels)),
                    provenance=tuple(dict.fromkeys(provenance)),
                )
            )
        if self.reranker is not None:
            candidates = list(self.reranker.rerank(query, candidates, limit=max(int(limit), 1) * 2))
        return _diversify(candidates, limit)


EmbeddingFn = Callable[[str], Sequence[float]]


class HashingEmbeddingProvider:
    """Deterministic, dependency-free embedding seam for tests and canaries.

    It is intentionally not presented as a production semantic model. A real
    embedding provider can implement the same callable boundary without
    changing the evidence or Pi layers.
    """

    name = "hashing-fixture"

    def __init__(self, *, dimensions: int = 64) -> None:
        self.dimensions = max(8, int(dimensions))

    def __call__(self, text: str) -> Sequence[float]:
        vector = [0.0] * self.dimensions
        for token in re.findall(r"[A-Za-z0-9]+", str(text or "").casefold()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] += 1.0
        return vector


class InMemoryEmbeddingIndex:
    """Optional vector index backed by an injected embedding function."""

    name = "in-memory-embedding"

    def __init__(self, embedder: EmbeddingFn, *, name: str | None = None) -> None:
        self.embedder = embedder
        if name:
            self.name = name
        self._vectors: dict[str, tuple[float, ...]] = {}

    def search(
        self,
        query: str,
        documents: Sequence[Any],
        *,
        limit: int,
    ) -> Sequence[VectorHit]:
        query_vector = tuple(float(value) for value in self.embedder(query))
        scores: list[VectorHit] = []
        for document in documents:
            ref = str(getattr(document, "ref", ""))
            text = " ".join(_document_text(document)[:3])
            vector = self._vectors.setdefault(ref, tuple(float(value) for value in self.embedder(text)))
            score = _cosine(query_vector, vector)
            if score > 0:
                scores.append(VectorHit(ref=ref, score=score, provenance=self.name))
        return sorted(scores, key=lambda item: (item.score, item.ref), reverse=True)[: max(1, int(limit))]


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    denominator = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return numerator / denominator if denominator else 0.0
