"""Status enumerations for ScholarFlow core objects."""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """`str`-backed enum compatible with Python 3.9."""

    def __str__(self) -> str:
        return self.value


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class ExperimentStatus(StrEnum):
    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    ARCHIVED = "archived"


class ProjectStageStatus(StrEnum):
    NOT_STARTED = "not_started"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    PAUSED = "paused"


class PaperStatus(StrEnum):
    TO_READ = "to_read"
    READING = "reading"
    DONE = "done"
    ARCHIVED = "archived"


class RiskItemStatus(StrEnum):
    OPEN = "open"
    WATCHING = "watching"
    MITIGATED = "mitigated"
    CLOSED = "closed"


class ResearchArtifactKind(StrEnum):
    RESEARCH_PLAN = "research_plan"
    DAILY_REPORT = "daily_report"
    WEEKLY_REPORT = "weekly_report"
    LITERATURE_REVIEW = "literature_review"
    SIMULATION_ANALYSIS = "simulation_analysis"
    MIND_MAP = "mind_map"
    MANUSCRIPT_DRAFT = "manuscript_draft"
    SYNTHESIS = "synthesis"


class ResearchArtifactStatus(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    FINAL = "final"
    ARCHIVED = "archived"


class KnowledgeRelationType(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    DEPENDS_ON = "depends_on"
    DERIVED_FROM = "derived_from"
    RELATED_TO = "related_to"
