"""JSON-backed storage for ScholarFlow Wave 1 objects."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, Generic, Mapping, Optional, Type, TypeVar

from .contracts import StorageEnvelope
from .models import (
    Experiment,
    IntakeItem,
    KnowledgeRelation,
    Paper,
    ProjectStage,
    ResearchArtifact,
    SerializableModel,
    Task,
    build_intake_derived_object_id,
    canonical_paper_id,
    utc_now,
    validate_object_id,
)
from .persistence import atomic_write_json, file_lock

ModelT = TypeVar("ModelT", bound=SerializableModel)


class JsonEntityStore(Generic[ModelT]):
    """Persists one object type into a single JSON file keyed by object id."""

    def __init__(self, root_dir: Path, model_cls: Type[ModelT]):
        self.root_dir = Path(root_dir)
        self.model_cls = model_cls
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.root_dir / f"{model_cls.object_type}s.json"

    def create(self, model: ModelT) -> StorageEnvelope:
        with file_lock(self.file_path):
            data = self._load()
            object_id = model.get_id()
            if object_id in data["items"]:
                raise ValueError(f"{self.model_cls.object_type} '{object_id}' already exists")
            data["items"][object_id] = model.to_dict()
            self._save(data)
        return StorageEnvelope(
            object_type=self.model_cls.object_type,
            operation="create",
            object_id=object_id,
            record=model.to_dict(),
        )

    def update(self, object_id: str, **changes: Any) -> StorageEnvelope:
        with file_lock(self.file_path):
            data = self._load()
            if object_id not in data["items"]:
                raise KeyError(f"{self.model_cls.object_type} '{object_id}' was not found")
            current = self.model_cls.from_dict(data["items"][object_id])
            if "updated_at" in getattr(current, "__dataclass_fields__", {}):
                changes.setdefault("updated_at", utc_now())
            updated = replace(current, **changes)
            data["items"][object_id] = updated.to_dict()
            self._save(data)
        return StorageEnvelope(
            object_type=self.model_cls.object_type,
            operation="update",
            object_id=object_id,
            record=updated.to_dict(),
        )

    def get_by_id(self, object_id: str) -> Optional[ModelT]:
        data = self._load()
        record = data["items"].get(object_id)
        if record is None:
            return None
        return self.model_cls.from_dict(record)

    def proposal_receipt(self, proposal_id: str) -> dict[str, Any] | None:
        receipt = self._load().get("mutation_receipts", {}).get(proposal_id)
        return dict(receipt) if isinstance(receipt, dict) else None

    def apply_proposal(
        self, proposal: Mapping[str, Any], validator: Callable[[ModelT], None],
    ) -> dict[str, Any]:
        """CAS and its durable receipt share one atomic entity-file replacement.

        Proposal/audit files are projections of this receipt. A failure while
        publishing either projection must never cause the domain write to run
        twice or become an unrecoverable stale proposal.
        """
        from .mutations import MutationError, record_fingerprint

        proposal_id = str(proposal["proposal_id"])
        object_id = str(next(iter(proposal["target"].values())))
        with file_lock(self.file_path):
            data = self._load()
            receipts = data.setdefault("mutation_receipts", {})
            if proposal_id in receipts:
                return {**receipts[proposal_id], "idempotent_replay": True}
            raw = data["items"].get(object_id)
            current = self.model_cls.from_dict(raw) if raw is not None else None
            if record_fingerprint(current.to_dict() if current else None) != proposal["base_fingerprint"]:
                raise MutationError("stale_proposal", "the target changed after this proposal was created")
            changes = dict(proposal["normalized_changes"])
            if "updated_at" in self.model_cls.__dataclass_fields__:
                changes.setdefault("updated_at", utc_now())
            candidate = self.model_cls.from_dict({
                **(current.to_dict() if current else {}),
                self.model_cls.id_field: object_id,
                **changes,
            })
            validator(candidate)
            record = candidate.to_dict()
            receipt = {
                "proposal_id": proposal_id,
                "operation": str(proposal["operation"]),
                "record": record,
                "audit_event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"scholarflow:commit:{proposal_id}")),
                "committed_at": utc_now(),
                "idempotent_replay": False,
            }
            data["items"][object_id] = record
            receipts[proposal_id] = receipt
            self._save(data)
            return dict(receipt)

    def list_ids(self) -> list[str]:
        data = self._load()
        return sorted(data["items"].keys())

    def list_records(self) -> list[ModelT]:
        data = self._load()
        return [self.model_cls.from_dict(data["items"][object_id]) for object_id in sorted(data["items"].keys())]

    def _load(self) -> Dict[str, Any]:
        if not self.file_path.exists():
            return {
                "version": 2,
                "revision": 0,
                "object_type": self.model_cls.object_type,
                "items": {},
            }
        with self.file_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _save(self, data: Dict[str, Any]) -> None:
        data["version"] = 2
        data["revision"] = int(data.get("revision", 0)) + 1
        atomic_write_json(self.file_path, data)


class ScholarFlowStore:
    """Top-level storage facade for downstream routing/experiment/project modules."""

    def __init__(self, root_dir: Path):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.experiments = JsonEntityStore(self.root_dir, Experiment)
        self.tasks = JsonEntityStore(self.root_dir, Task)
        self.project_stages = JsonEntityStore(self.root_dir, ProjectStage)
        self.papers = JsonEntityStore(self.root_dir, Paper)
        self.research_artifacts = JsonEntityStore(self.root_dir, ResearchArtifact)
        self.knowledge_relations = JsonEntityStore(self.root_dir, KnowledgeRelation)
        self.intake_items = JsonEntityStore(self.root_dir, IntakeItem)

    def _proposal_store(self, proposal: Mapping[str, Any]):
        kind = str(proposal.get("operation") or "").split(".", 1)[0]
        entries = {
            "project_stage": (self.project_stages, self._validate_project_stage),
            "task": (self.tasks, self._validate_task),
            "experiment": (self.experiments, self._validate_experiment),
            "paper": (self.papers, self._validate_paper),
            "research_artifact": (self.research_artifacts, self._validate_research_artifact),
            "knowledge_relation": (self.knowledge_relations, self._validate_knowledge_relation),
        }
        return entries.get(kind)

    def domain_proposal_receipt(self, proposal: Mapping[str, Any]) -> dict[str, Any] | None:
        entry = self._proposal_store(proposal)
        return entry[0].proposal_receipt(str(proposal["proposal_id"])) if entry else None

    def apply_domain_proposal(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        if proposal.get("operation") not in {
            "project_stage.create", "project_stage.update", "task.create", "task.update",
            "experiment.log", "paper.intake", "research_artifact.publish", "knowledge_relation.upsert",
        }:
            from .mutations import MutationError
            raise MutationError("invalid_proposal", "unsupported domain proposal operation")
        entry = self._proposal_store(proposal)
        if entry is None:
            raise ValueError("unsupported domain proposal operation")
        if proposal.get("operation") == "paper.intake":
            paper_id = str(proposal["target"]["paper_id"])
            canonical = canonical_paper_id(paper_id)
            if canonical != paper_id:
                changes = dict(proposal["normalized_changes"])
                changes["external_ids"] = {"zotero": paper_id.split(":", 1)[1], **changes.get("external_ids", {})}
                proposal = {**proposal, "target": {"paper_id": canonical}, "normalized_changes": changes}
        return entry[0].apply_proposal(proposal, entry[1])

    def create_experiment(self, experiment: Experiment) -> StorageEnvelope:
        self._validate_experiment(experiment)
        return self.experiments.create(experiment)

    def update_experiment(self, experiment_id: str, **changes: Any) -> StorageEnvelope:
        self._validate_existing_update(
            object_id=experiment_id,
            getter=self.get_experiment,
            model_cls=Experiment,
            validator=self._validate_experiment,
            changes=changes,
        )
        return self.experiments.update(experiment_id, **changes)

    def get_experiment(self, experiment_id: str) -> Optional[Experiment]:
        return self.experiments.get_by_id(experiment_id)

    def list_experiment_ids(self) -> list[str]:
        return self.experiments.list_ids()

    def list_experiments(self) -> list[Experiment]:
        return self.experiments.list_records()

    def create_task(self, task: Task) -> StorageEnvelope:
        self._validate_task(task)
        return self.tasks.create(task)

    def update_task(self, task_id: str, **changes: Any) -> StorageEnvelope:
        self._validate_existing_update(
            object_id=task_id,
            getter=self.get_task,
            model_cls=Task,
            validator=self._validate_task,
            changes=changes,
        )
        return self.tasks.update(task_id, **changes)

    def get_task(self, task_id: str) -> Optional[Task]:
        return self.tasks.get_by_id(task_id)

    def list_tasks(self) -> list[Task]:
        return self.tasks.list_records()

    def create_project_stage(self, project_stage: ProjectStage) -> StorageEnvelope:
        self._validate_project_stage(project_stage)
        return self.project_stages.create(project_stage)

    def update_project_stage(self, stage_id: str, **changes: Any) -> StorageEnvelope:
        self._validate_existing_update(
            object_id=stage_id,
            getter=self.get_project_stage,
            model_cls=ProjectStage,
            validator=self._validate_project_stage,
            changes=changes,
        )
        return self.project_stages.update(stage_id, **changes)

    def get_project_stage(self, stage_id: str) -> Optional[ProjectStage]:
        return self.project_stages.get_by_id(stage_id)

    def list_project_stages(self) -> list[ProjectStage]:
        return self.project_stages.list_records()

    def create_paper(self, paper: Paper) -> StorageEnvelope:
        canonical_id = canonical_paper_id(paper.paper_id)
        external_ids = dict(paper.external_ids)
        if paper.paper_id.startswith("zotero:"):
            external_ids.setdefault("zotero", paper.paper_id.split(":", 1)[1])
        canonical = replace(paper, paper_id=canonical_id, external_ids=external_ids)
        self._validate_paper(canonical)
        return self.papers.create(canonical)

    def update_paper(self, paper_id: str, **changes: Any) -> StorageEnvelope:
        paper_id = canonical_paper_id(paper_id)
        self._validate_existing_update(
            object_id=paper_id,
            getter=self.get_paper,
            model_cls=Paper,
            validator=self._validate_paper,
            changes=changes,
        )
        return self.papers.update(paper_id, **changes)

    def get_paper(self, paper_id: str) -> Optional[Paper]:
        canonical_id = canonical_paper_id(paper_id)
        paper = self.papers.get_by_id(canonical_id)
        if paper is not None:
            return paper
        # Read compatibility for stores written before internal Paper ids were
        # separated from provider identifiers.
        legacy = self.papers.get_by_id(paper_id)
        if legacy is not None:
            return legacy
        if paper_id.startswith("zotero:"):
            item_key = paper_id.split(":", 1)[1]
            return next(
                (item for item in self.papers.list_records() if item.external_ids.get("zotero") == item_key),
                None,
            )
        return None

    def list_paper_ids(self) -> list[str]:
        return self.papers.list_ids()

    def list_papers(self) -> list[Paper]:
        return self.papers.list_records()

    def create_research_artifact(self, artifact: ResearchArtifact) -> StorageEnvelope:
        self._validate_research_artifact(artifact)
        return self.research_artifacts.create(artifact)

    def update_research_artifact(self, artifact_id: str, **changes: Any) -> StorageEnvelope:
        self._validate_existing_update(
            object_id=artifact_id,
            getter=self.get_research_artifact,
            model_cls=ResearchArtifact,
            validator=self._validate_research_artifact,
            changes=changes,
        )
        return self.research_artifacts.update(artifact_id, **changes)

    def get_research_artifact(self, artifact_id: str) -> Optional[ResearchArtifact]:
        return self.research_artifacts.get_by_id(artifact_id)

    def list_research_artifacts(self) -> list[ResearchArtifact]:
        return self.research_artifacts.list_records()

    def create_knowledge_relation(self, relation: KnowledgeRelation) -> StorageEnvelope:
        self._validate_knowledge_relation(relation)
        return self.knowledge_relations.create(relation)

    def update_knowledge_relation(self, relation_id: str, **changes: Any) -> StorageEnvelope:
        self._validate_existing_update(
            object_id=relation_id,
            getter=self.get_knowledge_relation,
            model_cls=KnowledgeRelation,
            validator=self._validate_knowledge_relation,
            changes=changes,
        )
        return self.knowledge_relations.update(relation_id, **changes)

    def get_knowledge_relation(self, relation_id: str) -> Optional[KnowledgeRelation]:
        return self.knowledge_relations.get_by_id(relation_id)

    def list_knowledge_relations(self) -> list[KnowledgeRelation]:
        return self.knowledge_relations.list_records()

    def create_intake_item(self, intake_item: IntakeItem) -> StorageEnvelope:
        self._validate_intake_item(intake_item)
        return self.intake_items.create(intake_item)

    def update_intake_item(self, intake_id: str, **changes: Any) -> StorageEnvelope:
        self._validate_existing_update(
            object_id=intake_id,
            getter=self.get_intake_item,
            model_cls=IntakeItem,
            validator=self._validate_intake_item,
            changes=changes,
        )
        return self.intake_items.update(intake_id, **changes)

    def get_intake_item(self, intake_id: str) -> Optional[IntakeItem]:
        return self.intake_items.get_by_id(intake_id)

    def list_intake_items(self) -> list[IntakeItem]:
        return self.intake_items.list_records()

    def require_project_stage(self, stage_id: str) -> ProjectStage:
        normalized = validate_object_id("project_stage", stage_id)
        stage = self.get_project_stage(normalized)
        if stage is None:
            raise ValueError(f"project_stage '{normalized}' was not found")
        return stage

    def build_intake_object_id(self, object_type: str, intake_id: str) -> str:
        return build_intake_derived_object_id(object_type, intake_id)

    def validate_intake_item(self, intake_item: IntakeItem) -> None:
        """Validate a Pi-selected intake route without persisting it."""

        self._validate_intake_item(intake_item)

    def _validate_existing_update(
        self,
        *,
        object_id: str,
        getter: Any,
        model_cls: Type[ModelT],
        validator: Any,
        changes: Dict[str, Any],
    ) -> None:
        current = getter(object_id)
        if current is None:
            return
        candidate = model_cls.from_dict({**current.to_dict(), **changes})
        validator(candidate)

    def _validate_project_stage(self, project_stage: ProjectStage) -> None:
        validate_object_id(ProjectStage.object_type, project_stage.stage_id)
        origin = project_stage.creation_origin
        if origin is not None:
            if (not isinstance(origin, dict) or set(origin) != {"kind", "request_id"}
                    or origin["kind"] != "user_goal" or not isinstance(origin["request_id"], str)
                    or not origin["request_id"].strip() or origin["request_id"] != origin["request_id"].strip()
                    or len(origin["request_id"]) > 200):
                raise ValueError("invalid project creation origin")

    def _validate_task(self, task: Task) -> None:
        validate_object_id(Task.object_type, task.task_id)
        if task.project_stage_id:
            self.require_project_stage(task.project_stage_id)
        self._require_records("experiment", task.related_experiments, self.get_experiment)
        self._require_records("paper", task.related_papers, self.get_paper)

    def _validate_experiment(self, experiment: Experiment) -> None:
        validate_object_id(Experiment.object_type, experiment.experiment_id)
        if experiment.project_stage_id:
            self.require_project_stage(experiment.project_stage_id)
        self._require_records("task", experiment.related_tasks, self.get_task)

    def _validate_paper(self, paper: Paper) -> None:
        validate_object_id(Paper.object_type, paper.paper_id)
        self._require_records("task", paper.related_tasks, self.get_task)
        self._require_records("experiment", paper.related_experiments, self.get_experiment)

    def _validate_research_artifact(self, artifact: ResearchArtifact) -> None:
        validate_object_id(ResearchArtifact.object_type, artifact.artifact_id)
        if artifact.source_snapshot is not None:
            from .source_notes import validate_source_snapshot
            validate_source_snapshot(artifact.source_snapshot)
            if artifact.source_snapshot["stage_id"] != artifact.project_stage_id:
                raise ValueError("source snapshot project must match the artifact")
            from .source_notes import snapshot_references
            papers, refs = snapshot_references(artifact.source_snapshot)
            if (artifact.kind.value != "synthesis" or artifact.related_papers != papers
                    or artifact.evidence_refs != refs):
                raise ValueError("source snapshot must match synthesis note references")
        if artifact.project_stage_id:
            self.require_project_stage(artifact.project_stage_id)
        self._require_records("task", artifact.related_tasks, self.get_task)
        self._require_records("experiment", artifact.related_experiments, self.get_experiment)
        self._require_records("paper", artifact.related_papers, self.get_paper)

    def _validate_knowledge_relation(self, relation: KnowledgeRelation) -> None:
        validate_object_id(KnowledgeRelation.object_type, relation.relation_id)

    def _validate_intake_item(self, intake_item: IntakeItem) -> None:
        validate_object_id(IntakeItem.object_type, intake_item.intake_id)
        if intake_item.resolved_stage_id:
            self.require_project_stage(intake_item.resolved_stage_id)
        self._require_records("project_stage", intake_item.anchor_candidates, self.get_project_stage)
        self._require_records("task", intake_item.related_task_ids, self.get_task)
        self._require_records("experiment", intake_item.related_experiment_ids, self.get_experiment)
        self._require_records("paper", intake_item.related_paper_ids, self.get_paper)

    def _require_records(self, object_type: str, object_ids: list[str], getter: Any) -> None:
        for object_id in object_ids:
            normalized = validate_object_id(object_type, object_id)
            if getter(normalized) is None:
                raise ValueError(f"{object_type} '{normalized}' was not found")
