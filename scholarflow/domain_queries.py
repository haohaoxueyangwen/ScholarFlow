"""Deterministic read-only queries over ScholarFlow domain storage."""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from .enums import ExperimentStatus, KnowledgeRelationType, PaperStatus, ProjectStageStatus, ResearchArtifactKind, ResearchArtifactStatus, TaskStatus
from .models import Experiment, KnowledgeRelation, Paper, ProjectStage, ResearchArtifact, Task
from .storage import ScholarFlowStore

MAX_QUERY_RESULTS = 100


class DomainQueryError(ValueError):
    """Stable error returned by domain queries and transport adapters."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class ScholarFlowQueryService:
    """Read-only query facade shared by Pi and future transports."""

    def __init__(self, store: ScholarFlowStore):
        self.store = store

    def search_project_stages(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        normalized_status = _normalize_status(status, ProjectStageStatus, "project stage")
        matches = [
            item
            for item in self.store.list_project_stages()
            if (not normalized_status or item.status.value == normalized_status)
            and _matches_query(query, _project_stage_text(item))
        ]
        matches.sort(key=lambda item: (item.updated_at, item.stage_id), reverse=True)
        return _search_result(
            matches,
            query=query,
            status=normalized_status,
            limit=limit,
            offset=offset,
        )

    def get_project_stage(self, stage_id: str) -> dict[str, Any]:
        normalized_id = _required_id(stage_id, "stage_id")
        item = self.store.get_project_stage(normalized_id)
        if item is None:
            raise _not_found("project_stage", normalized_id)
        return item.to_dict()

    def search_tasks(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        project_stage_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        normalized_status = _normalize_status(status, TaskStatus, "task")
        normalized_stage_id = _optional_text(project_stage_id)
        matches = [
            item
            for item in self.store.list_tasks()
            if (not normalized_status or item.status.value == normalized_status)
            and (not normalized_stage_id or item.project_stage_id == normalized_stage_id)
            and _matches_query(query, _task_text(item))
        ]
        matches.sort(key=lambda item: (item.updated_at, item.task_id), reverse=True)
        return _search_result(
            matches,
            query=query,
            status=normalized_status,
            project_stage_id=normalized_stage_id,
            limit=limit,
        )

    def get_task(self, task_id: str) -> dict[str, Any]:
        normalized_id = _required_id(task_id, "task_id")
        item = self.store.get_task(normalized_id)
        if item is None:
            raise _not_found("task", normalized_id)
        return item.to_dict()

    def search_papers(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
        author: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
    ) -> dict[str, Any]:
        normalized_status = _normalize_status(status, PaperStatus, "paper")
        author_query = str(author or "").strip().casefold()
        minimum, maximum = int(year_from or 0), int(year_to or 0)
        if any(year and not 1000 <= year <= 2100 for year in (minimum, maximum)) or (minimum and maximum and minimum > maximum):
            raise DomainQueryError("invalid_argument", "paper year range is invalid")
        matches = [
            item
            for item in self.store.list_papers()
            if (not normalized_status or item.status.value == normalized_status)
            and _matches_query(query, _paper_text(item))
            and (not author_query or any(author_query in name.casefold() for name in item.authors))
            and (not minimum or item.year is not None and item.year >= minimum)
            and (not maximum or item.year is not None and item.year <= maximum)
        ]
        matches.sort(key=lambda item: (item.year or 0, item.paper_id), reverse=True)
        return {**_search_result(matches, query=query, status=normalized_status, limit=limit, offset=offset), "filter_scope": "full_local_catalog"}

    def get_paper(self, paper_id: str) -> dict[str, Any]:
        normalized_id = _required_id(paper_id, "paper_id")
        item = self.store.get_paper(normalized_id)
        if item is None:
            raise _not_found("paper", normalized_id)
        return item.to_dict()

    def search_experiments(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        project_stage_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        normalized_status = _normalize_status(status, ExperimentStatus, "experiment")
        normalized_stage_id = _optional_text(project_stage_id)
        matches = [
            item
            for item in self.store.list_experiments()
            if (not normalized_status or item.status.value == normalized_status)
            and (not normalized_stage_id or item.project_stage_id == normalized_stage_id)
            and _matches_query(query, _experiment_text(item))
        ]
        matches.sort(key=lambda item: (item.updated_at, item.experiment_id), reverse=True)
        return _search_result(
            matches,
            query=query,
            status=normalized_status,
            project_stage_id=normalized_stage_id,
            limit=limit,
        )

    def get_experiment(self, experiment_id: str) -> dict[str, Any]:
        normalized_id = _required_id(experiment_id, "experiment_id")
        item = self.store.get_experiment(normalized_id)
        if item is None:
            raise _not_found("experiment", normalized_id)
        return item.to_dict()

    def search_research_artifacts(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        project_stage_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        normalized_status = _normalize_status(status, ResearchArtifactStatus, "research artifact")
        normalized_kind = _normalize_status(kind, ResearchArtifactKind, "research artifact kind")
        normalized_stage_id = _optional_text(project_stage_id)
        matches = [
            item
            for item in self.store.list_research_artifacts()
            if (not normalized_status or item.status.value == normalized_status)
            and (not normalized_kind or item.kind.value == normalized_kind)
            and (not normalized_stage_id or item.project_stage_id == normalized_stage_id)
            and _matches_query(query, _research_artifact_text(item))
        ]
        matches.sort(key=lambda item: (item.updated_at, item.artifact_id), reverse=True)
        result = _search_result(matches, query=query, status=normalized_status, project_stage_id=normalized_stage_id, limit=limit)
        result["kind"] = normalized_kind
        return result

    def get_research_artifact(self, artifact_id: str) -> dict[str, Any]:
        normalized_id = _required_id(artifact_id, "artifact_id")
        item = self.store.get_research_artifact(normalized_id)
        if item is None:
            raise _not_found("research_artifact", normalized_id)
        return item.to_dict()

    def search_knowledge_relations(
        self,
        *,
        query: str | None = None,
        predicate: str | None = None,
        subject_ref: str | None = None,
        object_ref: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        normalized_predicate = _normalize_status(predicate, KnowledgeRelationType, "knowledge relation")
        normalized_subject = _optional_text(subject_ref)
        normalized_object = _optional_text(object_ref)
        matches = [
            item
            for item in self.store.list_knowledge_relations()
            if (not normalized_predicate or item.predicate.value == normalized_predicate)
            and (not normalized_subject or item.subject_ref == normalized_subject)
            and (not normalized_object or item.object_ref == normalized_object)
            and _matches_query(query, _knowledge_relation_text(item))
        ]
        matches.sort(key=lambda item: (item.updated_at, item.relation_id), reverse=True)
        normalized_limit = _normalize_limit(limit)
        selected = matches[:normalized_limit]
        return {
            "query": _optional_text(query) or "",
            "predicate": normalized_predicate,
            "subject_ref": normalized_subject,
            "object_ref": normalized_object,
            "count": len(selected),
            "total_matches": len(matches),
            "items": [item.to_dict() for item in selected],
        }

    def get_knowledge_relation(self, relation_id: str) -> dict[str, Any]:
        normalized_id = _required_id(relation_id, "relation_id")
        item = self.store.get_knowledge_relation(normalized_id)
        if item is None:
            raise _not_found("knowledge_relation", normalized_id)
        return item.to_dict()


def _search_result(
    matches: Sequence[Any],
    *,
    query: str | None,
    status: str | None,
    limit: int,
    project_stage_id: str | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    normalized_limit = _normalize_limit(limit)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise DomainQueryError("invalid_argument", "offset must be a non-negative integer")
    selected = list(matches[offset:offset + normalized_limit])
    result = {
        "query": _optional_text(query) or "",
        "status": status,
        "count": len(selected),
        "total_matches": len(matches),
        "items": [item.to_dict() for item in selected],
        "offset": offset,
        "next_offset": offset + len(selected),
        "has_more": offset + len(selected) < len(matches),
    }
    if project_stage_id is not None:
        result["project_stage_id"] = project_stage_id
    return result


def _normalize_limit(value: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise DomainQueryError("invalid_argument", "limit must be an integer") from exc
    if normalized < 1 or normalized > MAX_QUERY_RESULTS:
        raise DomainQueryError(
            "invalid_argument",
            f"limit must be between 1 and {MAX_QUERY_RESULTS}",
            details={"field": "limit", "minimum": 1, "maximum": MAX_QUERY_RESULTS},
        )
    return normalized


def _normalize_status(value: str | None, enum_type: type, label: str) -> str | None:
    normalized = _optional_text(value)
    if normalized is None:
        return None
    normalized = normalized.casefold()
    try:
        return enum_type(normalized).value
    except ValueError as exc:
        allowed = sorted(item.value for item in enum_type)
        raise DomainQueryError(
            "invalid_argument",
            f"unknown {label} status '{value}'",
            details={"field": "status", "allowed": allowed},
        ) from exc


def _required_id(value: str, field_name: str) -> str:
    normalized = _optional_text(value)
    if normalized is None:
        raise DomainQueryError(
            "invalid_argument",
            f"{field_name} is required",
            details={"field": field_name},
        )
    return normalized


def _not_found(object_type: str, object_id: str) -> DomainQueryError:
    return DomainQueryError(
        "not_found",
        f"{object_type} '{object_id}' was not found",
        details={"object_type": object_type, "object_id": object_id},
    )


def _optional_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _matches_query(query: str | None, values: Iterable[Any]) -> bool:
    terms = [term.casefold() for term in str(query or "").split() if term]
    if not terms:
        return True
    searchable = "\n".join(_search_text(value) for value in values).casefold()
    return all(term in searchable for term in terms)


def _search_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def _project_stage_text(item: ProjectStage) -> list[Any]:
    return [
        item.stage_id,
        item.project_name,
        item.title,
        item.goal,
        item.status.value,
        item.progress_note,
        item.related_tasks,
        item.related_experiments,
        item.risks,
        item.next_actions,
    ]


def _task_text(item: Task) -> list[Any]:
    return [
        item.task_id,
        item.title,
        item.description,
        item.project_stage_id,
        item.source,
        item.priority,
        item.status.value,
        item.owner,
        item.related_experiments,
        item.related_papers,
        item.due_hint,
    ]


def _paper_text(item: Paper) -> list[Any]:
    return [
        item.paper_id,
        item.title,
        item.authors,
        item.year,
        item.source,
        item.status.value,
        item.summary,
        item.relevance,
        item.related_tasks,
        item.related_experiments,
        item.obsidian_note,
    ]


def _experiment_text(item: Experiment) -> list[Any]:
    return [
        item.experiment_id,
        item.title,
        item.goal,
        item.project_stage_id,
        item.related_tasks,
        item.variables,
        item.metrics,
        item.command,
        item.server,
        item.log_path,
        item.checkpoint_path,
        item.status.value,
        item.result_summary,
        item.failure_reason,
        item.next_actions,
    ]


def _research_artifact_text(item: ResearchArtifact) -> list[Any]:
    return [
        item.artifact_id,
        item.kind.value,
        item.title,
        item.content,
        item.status.value,
        item.project_stage_id,
        item.evidence_refs,
        item.related_papers,
        item.related_tasks,
        item.related_experiments,
        item.period_start,
        item.period_end,
    ]


def _knowledge_relation_text(item: KnowledgeRelation) -> list[Any]:
    return [item.relation_id, item.subject_ref, item.predicate.value, item.object_ref, item.evidence_refs, item.note]
