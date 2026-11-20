"""Deterministic intake subsystem for Pi-native research entry."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import replace
from typing import Any, Iterable

from .domain_queries import DomainQueryError
from .models import Experiment, IntakeItem, Paper, ProjectStage, Task, utc_now
from .mutations import TaskMutationService
from .research_mutations import ResearchMutationService
from .storage import ScholarFlowStore

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "for",
    "from",
    "idea",
    "into",
    "new",
    "of",
    "on",
    "or",
    "please",
    "prepare",
    "proposal",
    "proposals",
    "state",
    "the",
    "then",
    "this",
    "to",
    "with",
}
_ALLOWED_ROUTES = {"task_only", "task_and_experiment", "paper_intake"}
_ROUTE_INTENTS = {
    "task_only": "task_idea",
    "task_and_experiment": "experiment_idea",
    "paper_intake": "paper_intake",
}


class IntakeService:
    def __init__(self, store: ScholarFlowStore):
        self.store = store
        self.task_mutations = TaskMutationService(store)
        self.research_mutations = ResearchMutationService(store)

    def create_item(self, *, raw_input: str, source: str = "chat") -> dict[str, Any]:
        normalized_input = _required_text(raw_input, "raw_input")
        normalized_source = _required_text(source, "source")
        intake_id = _build_intake_id(normalized_input)
        existing = self.store.get_intake_item(intake_id)
        if existing is not None:
            return {
                "intake": existing.to_dict(),
                "created": False,
            }
        item = IntakeItem(
            intake_id=intake_id,
            source=normalized_source,
            raw_input=normalized_input,
            status="pending",
        )
        self.store.create_intake_item(item)
        return {"intake": item.to_dict(), "created": True}

    def get_item(self, intake_id: str) -> dict[str, Any]:
        item = self.store.get_intake_item(_required_text(intake_id, "intake_id"))
        if item is None:
            raise DomainQueryError("not_found", f"intake_item '{intake_id}' was not found", details={"object_type": "intake_item", "object_id": intake_id})
        return item.to_dict()

    def anchor_item(self, intake_id: str) -> dict[str, Any]:
        item = self._get_item(intake_id)
        anchor = _resolve_anchor(self.store, item.raw_input)
        status = "anchored" if anchor["anchor_candidates"] else "clarify_required"
        envelope = self.store.update_intake_item(
            item.intake_id,
            status=status,
            intent_class=None,
            proposed_route=None,
            resolved_stage_id=None,
            anchor_candidates=anchor["anchor_candidates"],
            related_task_ids=[],
            related_experiment_ids=[],
            related_paper_ids=[],
            missing_fields=anchor["missing_fields"],
            summary=anchor["summary"],
        )
        updated = IntakeItem.from_dict(envelope.record)
        return {
            "intake": updated.to_dict(),
            "anchor": anchor,
        }

    def prepare_item(
        self,
        intake_id: str,
        *,
        route: str,
        stage_id: str,
        related_task_ids: list[str] | None = None,
        related_experiment_ids: list[str] | None = None,
        related_paper_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        item = self._get_item(intake_id)
        normalized_route = _required_text(route, "route")
        if normalized_route not in _ALLOWED_ROUTES:
            raise DomainQueryError(
                "invalid_argument",
                f"unsupported intake route '{normalized_route}'",
                details={"field": "route", "allowed_values": sorted(_ALLOWED_ROUTES)},
            )
        selected_stage_id = _required_text(stage_id, "stage_id")
        try:
            stage = self.store.require_project_stage(selected_stage_id)
        except ValueError as exc:
            raise DomainQueryError(
                "not_found",
                str(exc),
                details={"object_type": "project_stage", "object_id": selected_stage_id},
            ) from exc
        selected_item = replace(
            item,
            intent_class=_ROUTE_INTENTS[normalized_route],
            proposed_route=normalized_route,
            resolved_stage_id=selected_stage_id,
            related_task_ids=list(related_task_ids or []),
            related_experiment_ids=list(related_experiment_ids or []),
            related_paper_ids=list(related_paper_ids or []),
        )
        # The store validator checks that every Pi-selected id exists. Python
        # does not infer or silently substitute a route or related record.
        self.store.validate_intake_item(selected_item)
        prepared = _prepare_lane(selected_item, stage, self.store, normalized_route)
        envelope = self.store.update_intake_item(
            item.intake_id,
            status="proposal_ready",
            intent_class=selected_item.intent_class,
            proposed_route=prepared["route"],
            resolved_stage_id=selected_stage_id,
            related_task_ids=selected_item.related_task_ids,
            related_experiment_ids=selected_item.related_experiment_ids,
            related_paper_ids=selected_item.related_paper_ids,
            summary=prepared["summary"],
            missing_fields=prepared["missing_fields"],
        )
        updated = IntakeItem.from_dict(envelope.record)
        return {
            "intake": updated.to_dict(),
            "prepared": prepared,
        }

    def propose_item(self, intake_id: str, *, request_id: str | None = None) -> dict[str, Any]:
        item = self._get_item(intake_id)
        if not item.proposed_route or not item.resolved_stage_id:
            raise DomainQueryError(
                "clarification_required",
                "intake item must be explicitly prepared by Pi before proposals are emitted",
                details={"intake_id": item.intake_id, "missing_fields": ["route", "stage_id"]},
            )
        prepared_result = self.prepare_item(
            intake_id,
            route=item.proposed_route,
            stage_id=item.resolved_stage_id,
            related_task_ids=item.related_task_ids,
            related_experiment_ids=item.related_experiment_ids,
            related_paper_ids=item.related_paper_ids,
        )
        prepared = prepared_result["prepared"]
        proposals: list[dict[str, Any]] = []

        task_payload = prepared.get("task_proposal")
        if task_payload is not None:
            proposals.append(
                {
                    "tool": "sf_task_propose_create",
                    "proposal": self.task_mutations.propose_create(
                        task_payload["task_id"],
                        {key: value for key, value in task_payload.items() if key != "task_id"},
                        request_id=request_id,
                    ),
                }
            )

        experiment_payload = prepared.get("experiment_proposal")
        if experiment_payload is not None:
            proposals.append(
                {
                    "tool": "sf_experiment_propose_log",
                    "proposal": self.research_mutations.propose_experiment_log(
                        experiment_payload["experiment_id"],
                        {key: value for key, value in experiment_payload.items() if key != "experiment_id"},
                        request_id=request_id,
                    ),
                }
            )

        paper_payload = prepared.get("paper_proposal")
        if paper_payload is not None:
            proposals.append(
                {
                    "tool": "sf_paper_propose_intake",
                    "proposal": self.research_mutations.propose_paper_intake(
                        paper_payload["paper_id"],
                        {key: value for key, value in paper_payload.items() if key != "paper_id"},
                        request_id=request_id,
                    ),
                }
            )

        envelope = self.store.update_intake_item(
            item.intake_id,
            status="proposal_ready",
            summary=f"{prepared['summary']}; proposals={len(proposals)}",
        )
        updated = IntakeItem.from_dict(envelope.record)
        return {
            "intake": updated.to_dict(),
            "prepared": prepared,
            "proposals": proposals,
        }

    def _get_item(self, intake_id: str) -> IntakeItem:
        item = self.store.get_intake_item(_required_text(intake_id, "intake_id"))
        if item is None:
            raise DomainQueryError("not_found", f"intake_item '{intake_id}' was not found", details={"object_type": "intake_item", "object_id": intake_id})
        return item


def _resolve_anchor(store: ScholarFlowStore, raw_input: str) -> dict[str, Any]:
    terms = _keywords(raw_input)
    stage_scores: dict[str, float] = {}
    task_hits = _rank_records(store.list_tasks(), _task_text, terms)
    experiment_hits = _rank_records(store.list_experiments(), _experiment_text, terms)
    paper_hits = _rank_records(store.list_papers(), _paper_text, terms)
    stage_hits = _rank_records(store.list_project_stages(), _stage_text, terms)

    for score, stage in stage_hits:
        stage_scores[stage.stage_id] = stage_scores.get(stage.stage_id, 0.0) + score * 2.0
    for score, task in task_hits:
        if task.project_stage_id:
            stage_scores[task.project_stage_id] = stage_scores.get(task.project_stage_id, 0.0) + score * 1.4
    for score, experiment in experiment_hits:
        if experiment.project_stage_id:
            stage_scores[experiment.project_stage_id] = stage_scores.get(experiment.project_stage_id, 0.0) + score * 1.6

    ranked_stage_scores = sorted(stage_scores.items(), key=lambda item: (-item[1], item[0]))[:3]
    anchor_candidates = [stage_id for stage_id, _ in ranked_stage_scores]
    stages_by_id = {stage.stage_id: stage for stage in store.list_project_stages()}
    stage_candidates = [
        {
            "stage_id": stage_id,
            "project_name": stages_by_id[stage_id].project_name,
            "title": stages_by_id[stage_id].title,
            "score": round(score, 3),
        }
        for stage_id, score in ranked_stage_scores
        if stage_id in stages_by_id
    ]
    related_tasks = [task.task_id for _, task in task_hits[:3]]
    related_experiments = [
        experiment.experiment_id
        for _, experiment in experiment_hits[:3]
        if experiment.project_stage_id in anchor_candidates
    ]
    related_papers = [paper.paper_id for _, paper in paper_hits[:3]]
    missing_fields = ["route", "project_stage_id"] if anchor_candidates else ["project_stage_id"]
    summary = _build_anchor_summary(
        raw_input=raw_input,
        anchor_candidates=anchor_candidates,
        related_tasks=related_tasks,
        related_experiments=related_experiments,
        related_papers=related_papers,
    )
    return {
        "anchor_candidates": anchor_candidates,
        "stage_candidates": stage_candidates,
        "related_task_ids": related_tasks,
        "related_experiment_ids": related_experiments,
        "related_paper_ids": related_papers,
        "missing_fields": missing_fields,
        "summary": summary,
    }


def _prepare_lane(item: IntakeItem, stage: ProjectStage, store: ScholarFlowStore, route: str) -> dict[str, Any]:
    task = store.get_task(item.related_task_ids[0]) if item.related_task_ids else None
    experiment = store.get_experiment(item.related_experiment_ids[0]) if item.related_experiment_ids else None
    paper = store.get_paper(item.related_paper_ids[0]) if item.related_paper_ids else None
    task_id = store.build_intake_object_id("task", item.intake_id)
    experiment_id = store.build_intake_object_id("experiment", item.intake_id)
    task_title = _normalize_title(item.raw_input)
    experiment_title = _normalize_experiment_title(task_title)
    base_summary = f"Anchored to {stage.stage_id} ({stage.title})"
    task_payload = {
        "task_id": task_id,
        "title": task_title,
        "project_stage_id": stage.stage_id,
        "priority": "high",
        "status": "pending",
    }
    description_parts = [item.raw_input.strip()]
    if experiment is not None:
        description_parts.append(
            f"Extends existing experiment {experiment.experiment_id} ({experiment.title}) and its next actions."
        )
        task_payload["related_experiments"] = [experiment.experiment_id]
    if paper is not None:
        task_payload["related_papers"] = [paper.paper_id]
    task_payload["description"] = " ".join(description_parts)

    experiment_payload = None
    if route == "task_and_experiment":
        experiment_payload = {
            "experiment_id": experiment_id,
            "title": experiment_title,
            "goal": _normalize_goal(item.raw_input),
            "project_stage_id": stage.stage_id,
            "related_tasks": [task.task_id] if task is not None else [],
            "status": "planned",
        }

    paper_payload = None
    if route == "paper_intake":
        paper_payload = {
            "paper_id": store.build_intake_object_id("paper", item.intake_id),
            "title": task_title,
            "summary": item.raw_input.strip(),
            "related_tasks": [task.task_id] if task is not None else [],
        }

    return {
        "route": route,
        "resolved_stage": stage.to_dict(),
        "related_records": {
            "task": task.to_dict() if task is not None else None,
            "experiment": experiment.to_dict() if experiment is not None else None,
            "paper": paper.to_dict() if paper is not None else None,
        },
        "task_proposal": task_payload,
        "experiment_proposal": experiment_payload,
        "paper_proposal": paper_payload,
        "missing_fields": [],
        "summary": f"{base_summary}; route={route}",
    }


def _required_text(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise DomainQueryError("invalid_argument", f"{field_name} is required", details={"field": field_name})
    return normalized

def _build_intake_id(raw_input: str) -> str:
    # Intake identity is content-addressed so retries remain idempotent across
    # second boundaries, API restarts, and slower machines. Cosmetic Unicode,
    # whitespace, or case differences do not create duplicate research input.
    canonical = unicodedata.normalize("NFKC", raw_input).strip()
    canonical = re.sub(r"\s+", " ", canonical).casefold()
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return f"intake-{digest}"


def _keywords(raw_input: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+", raw_input.casefold())
    return [word for word in words if len(word) >= 3 and word not in _STOPWORDS]


def _rank_records(records: Iterable[Any], text_fn: Any, terms: list[str]) -> list[tuple[float, Any]]:
    scored: list[tuple[float, Any]] = []
    for record in records:
        text = " ".join(text_fn(record)).casefold()
        if not text:
            continue
        score = sum(1.0 for term in terms if term in text)
        if score <= 0:
            continue
        scored.append((score, record))
    scored.sort(key=lambda item: (-item[0], getattr(item[1], item[1].id_field)))
    return scored


def _build_anchor_summary(
    *,
    raw_input: str,
    anchor_candidates: list[str],
    related_tasks: list[str],
    related_experiments: list[str],
    related_papers: list[str],
) -> str:
    stage_text = ",".join(anchor_candidates) or "missing_stage"
    return (
        f"stage_candidates={stage_text}; "
        f"tasks={','.join(related_tasks[:2]) or 'none'}; "
        f"experiments={','.join(related_experiments[:2]) or 'none'}; "
        f"papers={','.join(related_papers[:2]) or 'none'}; "
        f"input={raw_input.strip()}"
    )


def _normalize_title(raw_input: str) -> str:
    cleaned = raw_input.strip().rstrip(".")
    prefixes = [
        "add an idea to ",
        "add idea to ",
        "please add ",
        "please create ",
        "create an idea to ",
    ]
    lowered = cleaned.casefold()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
            break
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:1].upper() + cleaned[1:]


def _normalize_experiment_title(task_title: str) -> str:
    lower = task_title.casefold()
    if lower.startswith("compare "):
        return task_title[8:].strip().capitalize() + " sweep"
    return task_title + " experiment"


def _normalize_goal(raw_input: str) -> str:
    cleaned = raw_input.strip().rstrip(".")
    if cleaned[:1].islower():
        cleaned = cleaned[:1].upper() + cleaned[1:]
    return cleaned


def _stage_text(item: ProjectStage) -> list[str]:
    return [item.stage_id, item.project_name, item.title, item.goal, item.progress_note or "", *item.risks, *item.next_actions]


def _task_text(item: Task) -> list[str]:
    return [item.task_id, item.title, item.description or "", item.project_stage_id or "", item.source or "", *(item.related_experiments or []), *(item.related_papers or [])]


def _experiment_text(item: Experiment) -> list[str]:
    return [item.experiment_id, item.title, item.goal, item.project_stage_id or "", item.result_summary or "", item.failure_reason or "", *item.related_tasks, *item.next_actions]


def _paper_text(item: Paper) -> list[str]:
    return [item.paper_id, item.title, item.summary or "", item.relevance or "", *(item.authors or []), item.source or "", *item.related_tasks, *item.related_experiments]
