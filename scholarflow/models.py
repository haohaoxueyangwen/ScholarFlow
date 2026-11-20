"""Dataclass models for the Wave 1 ScholarFlow object layer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, List, Optional, Type, TypeVar

from .enums import (
    ExperimentStatus,
    KnowledgeRelationType,
    PaperStatus,
    ProjectStageStatus,
    ResearchArtifactKind,
    ResearchArtifactStatus,
    RiskItemStatus,
    TaskStatus,
)

ISO_8601_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ModelT = TypeVar("ModelT", bound="SerializableModel")
OBJECT_ID_PREFIXES: Dict[str, str] = {
    "paper": "paper-",
    "experiment": "exp-",
    "task": "task-",
    "project_stage": "stage-",
    "daily_summary": "summary-",
    "research_artifact": "artifact-",
    "knowledge_relation": "relation-",
    "risk_item": "risk-",
    "intake_item": "intake-",
}
INTAKE_DERIVED_OBJECT_PREFIXES: Dict[str, str] = {
    "task": "task-",
    "experiment": "exp-",
    "paper": "paper-",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(ISO_8601_FORMAT)


def validate_object_id(object_type: str, object_id: str) -> str:
    normalized = str(object_id or "").strip()
    if not normalized:
        raise ValueError(f"{object_type} id is required")
    if object_type == "paper" and normalized.startswith("zotero:"):
        normalized = canonical_paper_id(normalized)
    expected_prefix = OBJECT_ID_PREFIXES.get(object_type)
    if expected_prefix and not normalized.startswith(expected_prefix):
        raise ValueError(f"{object_type} id '{normalized}' must start with '{expected_prefix}'")
    return normalized


def canonical_paper_id(paper_id: str) -> str:
    """Return the internal Paper id while accepting legacy Zotero aliases."""

    normalized = str(paper_id or "").strip()
    if normalized.startswith("zotero:"):
        item_key = normalized.split(":", 1)[1].strip()
        if not item_key:
            raise ValueError("zotero paper id is missing an item key")
        return f"paper-zotero-{item_key}"
    return normalized


def build_intake_derived_object_id(object_type: str, intake_id: str) -> str:
    intake_suffix = validate_object_id("intake_item", intake_id).removeprefix("intake-")
    object_prefix = INTAKE_DERIVED_OBJECT_PREFIXES.get(object_type)
    if object_prefix is None:
        raise ValueError(f"unsupported intake-derived object type '{object_type}'")
    return f"{object_prefix}intake-{intake_suffix}"


class SerializableModel:
    object_type: ClassVar[str]
    id_field: ClassVar[str]

    def get_id(self) -> str:
        return getattr(self, self.id_field)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if hasattr(value, "value"):
                payload[key] = value.value
        payload["object_type"] = self.object_type
        return payload

    @classmethod
    def from_dict(cls: Type[ModelT], payload: Dict[str, Any]) -> ModelT:
        normalized = dict(payload)
        normalized.pop("object_type", None)
        enum_fields = getattr(cls, "ENUM_FIELDS", {})
        for field_name, enum_cls in enum_fields.items():
            if field_name in normalized and normalized[field_name] is not None:
                normalized[field_name] = enum_cls(normalized[field_name])
        allowed = {item.name for item in fields(cls)}
        filtered = {key: value for key, value in normalized.items() if key in allowed}
        return cls(**filtered)


@dataclass(frozen=True)
class Paper(SerializableModel):
    object_type: ClassVar[str] = "paper"
    id_field: ClassVar[str] = "paper_id"
    ENUM_FIELDS: ClassVar[Dict[str, Any]] = {"status": PaperStatus}

    paper_id: str
    title: str
    authors: List[str] = field(default_factory=list)
    year: Optional[int] = None
    source: Optional[str] = None
    external_ids: Dict[str, str] = field(default_factory=dict)
    status: PaperStatus = PaperStatus.TO_READ
    summary: Optional[str] = None
    relevance: Optional[str] = None
    related_tasks: List[str] = field(default_factory=list)
    related_experiments: List[str] = field(default_factory=list)
    obsidian_note: Optional[str] = None


@dataclass(frozen=True)
class Experiment(SerializableModel):
    object_type: ClassVar[str] = "experiment"
    id_field: ClassVar[str] = "experiment_id"
    ENUM_FIELDS: ClassVar[Dict[str, Any]] = {"status": ExperimentStatus}

    experiment_id: str
    title: str
    goal: str
    project_stage_id: Optional[str] = None
    related_tasks: List[str] = field(default_factory=list)
    variables: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    command: Optional[str] = None
    server: Optional[str] = None
    log_path: Optional[str] = None
    checkpoint_path: Optional[str] = None
    status: ExperimentStatus = ExperimentStatus.PLANNED
    result_summary: Optional[str] = None
    failure_reason: Optional[str] = None
    next_actions: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Task(SerializableModel):
    object_type: ClassVar[str] = "task"
    id_field: ClassVar[str] = "task_id"
    ENUM_FIELDS: ClassVar[Dict[str, Any]] = {"status": TaskStatus}

    task_id: str
    title: str
    description: Optional[str] = None
    project_stage_id: Optional[str] = None
    source: Optional[str] = None
    priority: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    owner: Optional[str] = None
    related_experiments: List[str] = field(default_factory=list)
    related_papers: List[str] = field(default_factory=list)
    due_hint: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class ProjectStage(SerializableModel):
    object_type: ClassVar[str] = "project_stage"
    id_field: ClassVar[str] = "stage_id"
    ENUM_FIELDS: ClassVar[Dict[str, Any]] = {"status": ProjectStageStatus}

    stage_id: str
    project_name: str
    title: str
    goal: str
    status: ProjectStageStatus = ProjectStageStatus.NOT_STARTED
    progress_note: Optional[str] = None
    related_tasks: List[str] = field(default_factory=list)
    related_experiments: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    next_actions: List[str] = field(default_factory=list)
    updated_at: str = field(default_factory=utc_now)
    creation_origin: Optional[Dict[str, str]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        # Legacy proposal fingerprints must not acquire a new null field.
        if self.creation_origin is None:
            payload.pop("creation_origin", None)
        return payload


@dataclass(frozen=True)
class DailySummary(SerializableModel):
    object_type: ClassVar[str] = "daily_summary"
    id_field: ClassVar[str] = "summary_id"
    ENUM_FIELDS: ClassVar[Dict[str, Any]] = {}

    summary_id: str
    date: str
    completed_tasks: List[str] = field(default_factory=list)
    experiment_updates: List[str] = field(default_factory=list)
    paper_updates: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    next_actions: List[str] = field(default_factory=list)
    generated_from: List[str] = field(default_factory=list)
    obsidian_note: Optional[str] = None


@dataclass(frozen=True)
class ResearchArtifact(SerializableModel):
    """Durable, evidence-linked output produced by a Pi research workflow."""

    object_type: ClassVar[str] = "research_artifact"
    id_field: ClassVar[str] = "artifact_id"
    ENUM_FIELDS: ClassVar[Dict[str, Any]] = {
        "kind": ResearchArtifactKind,
        "status": ResearchArtifactStatus,
    }

    artifact_id: str
    kind: ResearchArtifactKind
    title: str
    content: str
    status: ResearchArtifactStatus = ResearchArtifactStatus.DRAFT
    project_stage_id: Optional[str] = None
    evidence_refs: List[str] = field(default_factory=list)
    related_papers: List[str] = field(default_factory=list)
    related_tasks: List[str] = field(default_factory=list)
    related_experiments: List[str] = field(default_factory=list)
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    obsidian_note: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    source_snapshot: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        # Preserve legacy record/proposal fingerprints without a new null key.
        if self.source_snapshot is None:
            payload.pop("source_snapshot", None)
        return payload


@dataclass(frozen=True)
class KnowledgeRelation(SerializableModel):
    """A confirmed edge between research objects, claims, or external evidence."""

    object_type: ClassVar[str] = "knowledge_relation"
    id_field: ClassVar[str] = "relation_id"
    ENUM_FIELDS: ClassVar[Dict[str, Any]] = {"predicate": KnowledgeRelationType}

    relation_id: str
    subject_ref: str
    predicate: KnowledgeRelationType
    object_ref: str
    evidence_refs: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    note: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class RiskItem(SerializableModel):
    object_type: ClassVar[str] = "risk_item"
    id_field: ClassVar[str] = "risk_id"
    ENUM_FIELDS: ClassVar[Dict[str, Any]] = {"status": RiskItemStatus}

    risk_id: str
    title: str
    description: Optional[str] = None
    related_stage: Optional[str] = None
    related_experiment: Optional[str] = None
    severity: Optional[str] = None
    status: RiskItemStatus = RiskItemStatus.OPEN
    mitigation: Optional[str] = None
    updated_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class IntakeItem(SerializableModel):
    object_type: ClassVar[str] = "intake_item"
    id_field: ClassVar[str] = "intake_id"
    ENUM_FIELDS: ClassVar[Dict[str, Any]] = {}

    intake_id: str
    source: str
    raw_input: str
    status: str = "pending"
    intent_class: Optional[str] = None
    proposed_route: Optional[str] = None
    resolved_stage_id: Optional[str] = None
    anchor_candidates: List[str] = field(default_factory=list)
    related_task_ids: List[str] = field(default_factory=list)
    related_experiment_ids: List[str] = field(default_factory=list)
    related_paper_ids: List[str] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)
    summary: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


CORE_MODEL_TYPES: Dict[str, Type[SerializableModel]] = {
    Paper.object_type: Paper,
    Experiment.object_type: Experiment,
    Task.object_type: Task,
    ProjectStage.object_type: ProjectStage,
    DailySummary.object_type: DailySummary,
    ResearchArtifact.object_type: ResearchArtifact,
    KnowledgeRelation.object_type: KnowledgeRelation,
    RiskItem.object_type: RiskItem,
    IntakeItem.object_type: IntakeItem,
}
