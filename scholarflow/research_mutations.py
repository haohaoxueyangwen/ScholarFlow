"""Governed Pi mutations for project stages, experiments, papers, and KB notes."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

ExistenceMode = Literal["create_only", "update_only", "upsert"]

from .enums import ExperimentStatus, KnowledgeRelationType, PaperStatus, ProjectStageStatus, ResearchArtifactKind, ResearchArtifactStatus
from .event_bus import append_event
from .models import Experiment, KnowledgeRelation, Paper, ProjectStage, ResearchArtifact
from .mutations import (
    MutationError,
    required_text,
    record_fingerprint,
)
from .proposal_state import (
    find_pending_target_proposal as _find_pending_target_proposal,
    load_proposal_state as _load_state,
    raise_if_rejected as _raise_if_rejected,
    save_proposal_state as _save_state,
    valid_confirmation as _valid_confirmation,
    serialized_proposal_apply,
    publish_domain_receipt,
)
from .obsidian_sync import plan_object_sync, sync_object_to_vault
from .storage import ScholarFlowStore

TTL = timedelta(hours=24)
_required_text = required_text


class ResearchMutationService:
    """Proposal/apply boundary for the remaining ScholarFlow domain writes."""

    def __init__(self, store: ScholarFlowStore):
        self.store = store
        self.root_dir = store.root_dir

    def propose_project_update(self, stage_id: str, changes: Mapping[str, Any], **context: Any) -> dict[str, Any]:
        return self._propose(
            "project_stage.update",
            "stage_id",
            stage_id,
            changes,
            getter=self.store.get_project_stage,
            normalizer=self._project_changes,
            existence="update_only",
            **context,
        )

    def propose_project_create(self, stage_id: str, changes: Mapping[str, Any], **context: Any) -> dict[str, Any]:
        return self._propose(
            "project_stage.create",
            "stage_id",
            stage_id,
            changes,
            getter=self.store.get_project_stage,
            normalizer=self._project_changes,
            existence="create_only",
            **context,
        )

    def propose_project_from_goal(
        self, stage_id: str, changes: Mapping[str, Any], *, trusted_goal: str,
        goal_request_id: str, request_id: str | None = None, now: datetime | None = None,
    ) -> dict[str, Any]:
        """Separate trusted-host entry; user goals are not paper evidence."""
        for value, maximum in ((trusted_goal, 4000), (goal_request_id, 200)):
            if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
                raise MutationError("invalid_argument", "invalid trusted goal request")

        def normalize(values: Mapping[str, Any], creating: bool) -> dict[str, Any]:
            del creating
            result = _normalize(values, strings={"project_name", "title", "goal"}, nullable=set(),
                                lists={"evidence_refs", "next_actions", "risks"}, enum=("status", ProjectStageStatus))
            if result.get("goal") != trusted_goal:
                raise MutationError("permission_denied", "project goal differs from the current host request")
            if result.get("evidence_refs"):
                raise MutationError("invalid_argument", "goal creation cannot fabricate paper evidence")
            result["evidence_refs"] = []
            result["creation_origin"] = {"kind": "user_goal", "request_id": goal_request_id}
            return result

        return self._propose("project_stage.create", "stage_id", stage_id, changes,
                             getter=self.store.get_project_stage, normalizer=normalize, existence="create_only",
                             request_id=request_id, now=now)

    def propose_experiment_log(self, experiment_id: str, changes: Mapping[str, Any], **context: Any) -> dict[str, Any]:
        return self._propose(
            "experiment.log",
            "experiment_id",
            experiment_id,
            changes,
            getter=self.store.get_experiment,
            normalizer=self._experiment_changes,
            existence="upsert",
            **context,
        )

    def propose_paper_intake(self, paper_id: str, changes: Mapping[str, Any], **context: Any) -> dict[str, Any]:
        return self._propose(
            "paper.intake",
            "paper_id",
            paper_id,
            changes,
            getter=self.store.get_paper,
            normalizer=self._paper_changes,
            existence="upsert",
            **context,
        )

    def propose_research_artifact(self, artifact_id: str, changes: Mapping[str, Any], **context: Any) -> dict[str, Any]:
        return self._propose(
            "research_artifact.publish",
            "artifact_id",
            artifact_id,
            changes,
            getter=self.store.get_research_artifact,
            normalizer=self._artifact_changes,
            existence="upsert",
            **context,
        )

    def propose_knowledge_relation(self, relation_id: str, changes: Mapping[str, Any], **context: Any) -> dict[str, Any]:
        return self._propose(
            "knowledge_relation.upsert",
            "relation_id",
            relation_id,
            changes,
            getter=self.store.get_knowledge_relation,
            normalizer=self._relation_changes,
            existence="upsert",
            **context,
        )

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        normalized = _required_text(proposal_id, "proposal_id")
        proposal = _load_state(self.root_dir)["proposals"].get(normalized)
        if proposal is None:
            raise MutationError("not_found", f"mutation proposal '{normalized}' was not found")
        return dict(proposal)

    @serialized_proposal_apply
    def apply(self, proposal_id: str, *, confirmation: Mapping[str, Any] | None, request_id: str | None = None) -> dict[str, Any]:
        return self._apply_under_lock(proposal_id, confirmation=confirmation, request_id=request_id)

    def _apply_under_lock(self, proposal_id: str, *, confirmation: Mapping[str, Any] | None, request_id: str | None = None) -> dict[str, Any]:
        """Shared domain CAS/receipt kernel; caller holds application lock."""
        proposal, state, replay = self._prepare_apply(proposal_id, confirmation, request_id)
        if replay is not None:
            return replay
        snapshot = proposal["normalized_changes"].get("source_snapshot")
        if proposal["operation"] == "research_artifact.publish" and snapshot is not None:
            from .source_notes import source_locks, source_version_state
            with source_locks(self.store):
                if not source_version_state(self.store, snapshot)["matches_current"]:
                    raise MutationError("source_version_conflict", "Project or stored source changed; review a new proposal")
                return self._apply_domain_checked(proposal, state, request_id)
        return self._apply_domain_checked(proposal, state, request_id)

    def _apply_domain_checked(self, proposal, state, request_id):
        """Entity CAS under application lock and any required source locks."""
        operation = proposal["operation"]
        object_id = next(iter(proposal["target"].values()))
        current = self._get(operation, object_id)
        current_fingerprint = record_fingerprint(current.to_dict() if current is not None else None)
        if current_fingerprint != proposal["base_fingerprint"]:
            self._event("pi.domain.mutation.stale", proposal, request_id, reason="target_changed")
            raise MutationError("stale_proposal", "the target changed after this proposal was created")

        receipt = self.store.apply_domain_proposal(proposal)
        return self._commit(proposal, state, receipt["record"], request_id)

    def _propose(
        self,
        operation: str,
        id_field: str,
        object_id: str,
        changes: Mapping[str, Any],
        *,
        getter: Callable[[str], Any],
        normalizer: Callable[[Mapping[str, Any], bool], dict[str, Any]],
        existence: ExistenceMode,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_id = _required_text(object_id, id_field)
        current = getter(normalized_id)
        if current is None and existence == "update_only":
            raise MutationError("not_found", f"{operation.split('.')[0]} '{normalized_id}' was not found")
        if current is not None and existence == "create_only":
            raise MutationError(
                "id_conflict",
                f"{operation.split('.')[0]} '{normalized_id}' already exists",
                details={id_field: normalized_id},
            )
        normalized = normalizer(changes, current is None)
        current_record = current.to_dict() if current is not None else {}
        effective = {key: value for key, value in normalized.items() if current_record.get(key) != value}
        if not effective:
            raise MutationError("no_changes", "the proposal would not change the target")
        if current is None:
            required = {
                "project_stage.create": {"project_name", "title", "goal", "evidence_refs"},
                "experiment.log": {"title", "goal"},
                "paper.intake": {"title"},
                "research_artifact.publish": {"kind", "title", "content"},
                "knowledge_relation.upsert": {"subject_ref", "predicate", "object_ref"},
            }.get(operation, set())
            missing = sorted(required - set(effective))
            if missing:
                raise MutationError("invalid_argument", "create proposal is missing required fields", details={"fields": missing})

        identity = {
            "operation": operation,
            "target": {id_field: normalized_id},
            "normalized_changes": effective,
            "base_fingerprint": record_fingerprint(current_record if current is not None else None),
        }
        proposal_id = "sha256:" + hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()
        state = _load_state(self.root_dir)
        if proposal_id in state["proposals"]:
            return dict(state["proposals"][proposal_id])
        if current is None:
            conflicting = _find_pending_target_proposal(
                state["proposals"],
                operation=operation,
                target_field=id_field,
                object_id=normalized_id,
                proposal_id=proposal_id,
                terminal_ids=set(state["commits"]) | set(state["rejections"]),
            )
            if conflicting is not None:
                raise MutationError(
                    "id_reserved",
                    f"{operation.split('.')[0]} '{normalized_id}' already has an uncommitted create proposal",
                    details={id_field: normalized_id, "proposal_id": conflicting["proposal_id"]},
                )
        effective_now = _now(now)
        proposal = {
            "proposal_version": 1,
            "proposal_id": proposal_id,
            **identity,
            "mode": "create" if current is None else "update",
            "diff": {key: {"before": current_record.get(key), "after": value} for key, value in effective.items()},
            "policy": {"side_effect_class": "domain_write", "requires_confirmation": True, "reasons": ["Pi-initiated research-domain write"]},
            "warnings": [],
            "created_at": effective_now.isoformat(),
            "expires_at": (effective_now + TTL).isoformat(),
        }
        state["proposals"][proposal_id] = proposal
        _save_state(self.root_dir, state)
        self._event("pi.domain.mutation.proposed", proposal, request_id)
        return proposal

    def _prepare_apply(self, proposal_id: str, confirmation: Mapping[str, Any] | None, request_id: str | None):
        normalized = _required_text(proposal_id, "proposal_id")
        state = _load_state(self.root_dir)
        committed = state["commits"].get(normalized)
        if committed:
            response = dict(committed["response"])
            response["idempotent_replay"] = True
            self._event("pi.domain.mutation.replayed", {"proposal_id": normalized, "operation": response["operation"], "target": {}}, request_id)
            return {}, state, response
        _raise_if_rejected(state, normalized)
        proposal = state["proposals"].get(normalized)
        if proposal is None:
            raise MutationError("not_found", f"mutation proposal '{normalized}' was not found")
        durable = self.store.domain_proposal_receipt(proposal)
        if durable is not None:
            recovered = publish_domain_receipt(self.root_dir, proposal, durable, state, request_id)
            return {}, state, {**recovered, "idempotent_replay": True}
        if _now() > datetime.fromisoformat(proposal["expires_at"]):
            self._reject("expired_proposal", proposal, request_id)
        if not _valid_confirmation(confirmation, normalized):
            self._reject("confirmation_required", proposal, request_id)
        self._event("pi.domain.mutation.confirmed", proposal, request_id)
        return proposal, state, None

    def _commit(self, proposal: Mapping[str, Any], state: dict[str, Any], record: dict[str, Any], request_id: str | None) -> dict[str, Any]:
        del record
        receipt = self.store.domain_proposal_receipt(proposal)
        if receipt is None:
            raise MutationError("invalid_state", "domain mutation has no durable receipt")
        return publish_domain_receipt(self.root_dir, proposal, receipt, state, request_id)

    def _get(self, operation: str, object_id: str):
        if operation in {"project_stage.create", "project_stage.update"}:
            return self.store.get_project_stage(object_id)
        if operation == "experiment.log":
            return self.store.get_experiment(object_id)
        if operation == "paper.intake":
            return self.store.get_paper(object_id)
        if operation == "research_artifact.publish":
            return self.store.get_research_artifact(object_id)
        if operation == "knowledge_relation.upsert":
            return self.store.get_knowledge_relation(object_id)
        raise MutationError("invalid_proposal", f"unsupported proposal operation '{operation}'")

    def _event(self, event_type: str, proposal: Mapping[str, Any], request_id: str | None, **extra: Any):
        return append_event(self.root_dir, event_type=event_type, payload={"proposal_id": proposal.get("proposal_id"), "operation": proposal.get("operation"), "target": proposal.get("target", {}), "pi_tool_call_id": request_id, **extra})

    def _reject(self, code: str, proposal: Mapping[str, Any], request_id: str | None) -> None:
        self._event("pi.domain.mutation.rejected", proposal, request_id, reason=code)
        raise MutationError(code, "interactive user confirmation is required" if code == "confirmation_required" else "the mutation proposal has expired")

    def _project_changes(self, changes: Mapping[str, Any], creating: bool) -> dict[str, Any]:
        normalized = _normalize(changes, strings={"project_name", "title", "goal", "progress_note"}, nullable={"progress_note"}, lists={"related_tasks", "related_experiments", "evidence_refs", "risks", "next_actions"}, enum=("status", ProjectStageStatus))
        if creating:
            evidence_refs = normalized.get("evidence_refs") or []
            if not evidence_refs:
                raise MutationError("invalid_argument", "project creation requires at least one evidence ref", details={"field": "evidence_refs"})
            invalid_refs = [ref for ref in evidence_refs if not ref.startswith("paper:") or self.store.get_paper(ref.removeprefix("paper:")) is None]
            if invalid_refs:
                raise MutationError("invalid_reference", "project evidence refs must point to stored papers", details={"evidence_refs": invalid_refs})
        return normalized

    def _experiment_changes(self, changes: Mapping[str, Any], creating: bool) -> dict[str, Any]:
        normalized = _normalize(changes, strings={"title", "goal", "project_stage_id", "command", "server", "log_path", "checkpoint_path", "result_summary", "failure_reason"}, nullable={"project_stage_id", "command", "server", "log_path", "checkpoint_path", "result_summary", "failure_reason"}, lists={"related_tasks", "next_actions"}, mappings={"variables", "metrics"}, enum=("status", ExperimentStatus))
        stage_id = normalized.get("project_stage_id")
        if stage_id:
            try:
                self.store.require_project_stage(stage_id)
            except ValueError as exc:
                raise MutationError("invalid_reference", str(exc)) from exc
        return normalized

    def _paper_changes(self, changes: Mapping[str, Any], creating: bool) -> dict[str, Any]:
        normalized = _normalize(changes, strings={"title", "source", "summary", "relevance", "obsidian_note"}, nullable={"source", "summary", "relevance", "obsidian_note"}, lists={"authors", "related_tasks", "related_experiments"}, mappings={"external_ids"}, enum=("status", PaperStatus), integers={"year"})
        external_ids = normalized.get("external_ids")
        if external_ids is not None:
            if not all(
                isinstance(key, str)
                and key.strip()
                and isinstance(value, str)
                and value.strip()
                for key, value in external_ids.items()
            ):
                raise MutationError(
                    "invalid_argument",
                    "external_ids must map non-empty strings to non-empty strings",
                )
            normalized["external_ids"] = {
                str(key).strip(): str(value).strip()
                for key, value in external_ids.items()
            }
        return normalized

    def _artifact_changes(self, changes: Mapping[str, Any], creating: bool) -> dict[str, Any]:
        normalized = _normalize(
            changes,
            strings={"title", "content", "project_stage_id", "period_start", "period_end", "obsidian_note"},
            nullable={"project_stage_id", "period_start", "period_end", "obsidian_note"},
            lists={"evidence_refs", "related_papers", "related_tasks", "related_experiments"},
            enum=("status", ResearchArtifactStatus),
            extra_enums={"kind": ResearchArtifactKind},
        )
        stage_id = normalized.get("project_stage_id")
        if stage_id:
            try:
                self.store.require_project_stage(stage_id)
            except ValueError as exc:
                raise MutationError("invalid_reference", str(exc)) from exc
        return normalized

    def _relation_changes(self, changes: Mapping[str, Any], creating: bool) -> dict[str, Any]:
        normalized = _normalize(
            changes,
            strings={"subject_ref", "object_ref", "note"},
            nullable={"note"},
            lists={"evidence_refs"},
            enum=("predicate", KnowledgeRelationType),
            numbers={"confidence"},
        )
        confidence = normalized.get("confidence")
        if confidence is not None and not 0.0 <= confidence <= 1.0:
            raise MutationError("invalid_argument", "confidence must be between 0 and 1")
        return normalized


class KBMutationService:
    """Governed object-to-vault write proposals using the KB Constitution."""

    def __init__(self, store: ScholarFlowStore, vault_root: Path):
        self.store = store
        self.root_dir = store.root_dir
        self.vault_root = Path(vault_root).resolve()

    def propose_object_write(
        self,
        *,
        object_type: str,
        object_id: str,
        content_kind: str,
        title: str | None = None,
        target_path: str | None = None,
        related_links: list[str] | None = None,
        scope: str = "project",
        journal_required: bool = False,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if object_type not in {"project_stage", "task", "experiment", "paper", "research_artifact"}:
            raise MutationError("invalid_argument", "unsupported KB object_type")
        normalized_scope = str(scope or "project").strip() or "project"
        decision = plan_object_sync(
            self.vault_root,
            self.store,
            object_type=object_type,
            object_id=object_id,
            content_kind=content_kind,
            title=title,
            target_path=target_path,
            scope=normalized_scope,
            related_links=related_links,
            journal_required=bool(journal_required),
        )
        if not decision.allowed:
            raise MutationError("policy_rejected", decision.reason, details=decision.to_dict())
        target = Path(decision.target_path)
        base = _file_fingerprint(target)
        identity = {
            "operation": "kb.object.write",
            "target": {"object_type": object_type, "object_id": object_id, "target_path": str(target)},
            "normalized_changes": {
                "content_kind": content_kind,
                "title": title,
                "target_path": target_path,
                "related_links": list(related_links or []),
                "scope": normalized_scope,
                "journal_required": bool(journal_required),
            },
            "base_fingerprint": base,
        }
        proposal_id = "sha256:" + hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()
        state = _load_state(self.root_dir)
        if proposal_id in state["proposals"]:
            return dict(state["proposals"][proposal_id])
        now = _now()
        proposal = {"proposal_version": 1, "proposal_id": proposal_id, **identity, "diff": {"target_path": str(target), "will_replace": target.exists(), "constitution": decision.to_dict()}, "policy": {"side_effect_class": "destructive_or_external_write", "requires_confirmation": True, "reasons": ["Obsidian vault write"]}, "warnings": [], "created_at": now.isoformat(), "expires_at": (now + TTL).isoformat()}
        state["proposals"][proposal_id] = proposal
        _save_state(self.root_dir, state)
        append_event(self.root_dir, event_type="pi.domain.mutation.proposed", payload={"proposal_id": proposal_id, "operation": "kb.object.write", "target": identity["target"], "pi_tool_call_id": request_id})
        return proposal

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        proposal = _load_state(self.root_dir)["proposals"].get(_required_text(proposal_id, "proposal_id"))
        if proposal is None:
            raise MutationError("not_found", "KB write proposal was not found")
        return dict(proposal)

    def apply(self, proposal_id: str, *, confirmation: Mapping[str, Any] | None, request_id: str | None = None) -> dict[str, Any]:
        normalized = _required_text(proposal_id, "proposal_id")
        state = _load_state(self.root_dir)
        if normalized in state["commits"]:
            response = dict(state["commits"][normalized]["response"])
            response["idempotent_replay"] = True
            append_event(self.root_dir, event_type="pi.domain.mutation.replayed", payload={"proposal_id": normalized, "operation": "kb.object.write", "pi_tool_call_id": request_id})
            return response
        _raise_if_rejected(state, normalized)
        proposal = state["proposals"].get(normalized)
        if proposal is None or proposal.get("operation") != "kb.object.write":
            raise MutationError("not_found", "KB write proposal was not found")
        if not _valid_confirmation(confirmation, normalized):
            append_event(self.root_dir, event_type="pi.domain.mutation.rejected", payload={"proposal_id": normalized, "operation": "kb.object.write", "pi_tool_call_id": request_id, "reason": "confirmation_required"})
            raise MutationError("confirmation_required", "interactive user confirmation is required")
        append_event(self.root_dir, event_type="pi.domain.mutation.confirmed", payload={"proposal_id": normalized, "operation": "kb.object.write", "pi_tool_call_id": request_id})
        target = Path(proposal["target"]["target_path"])
        if _file_fingerprint(target) != proposal["base_fingerprint"]:
            append_event(self.root_dir, event_type="pi.domain.mutation.stale", payload={"proposal_id": normalized, "operation": "kb.object.write", "pi_tool_call_id": request_id})
            raise MutationError("stale_proposal", "the target note changed after proposal creation")
        params = proposal["normalized_changes"]
        decision = sync_object_to_vault(
            self.vault_root,
            self.store,
            object_type=proposal["target"]["object_type"],
            object_id=proposal["target"]["object_id"],
            content_kind=params["content_kind"],
            title=params["title"],
            target_path=params["target_path"],
            scope=params.get("scope", "project"),
            related_links=params["related_links"],
            journal_required=bool(params.get("journal_required", False)),
        )
        if not decision.allowed:
            raise MutationError("policy_rejected", decision.reason, details=decision.to_dict())
        event = append_event(self.root_dir, event_type="pi.domain.mutation.committed", payload={"proposal_id": normalized, "operation": "kb.object.write", "target": proposal["target"], "pi_tool_call_id": request_id})
        response = {"proposal_id": normalized, "operation": "kb.object.write", "decision": decision.to_dict(), "audit_event_id": event["event_id"], "idempotent_replay": False}
        state["commits"][normalized] = {"committed_at": _now().isoformat(), "event_id": event["event_id"], "response": response}; _save_state(self.root_dir, state)
        return response


def _normalize(changes: Mapping[str, Any], *, strings: set[str], nullable: set[str], lists: set[str], enum: tuple[str, Any], mappings: set[str] | None = None, integers: set[str] | None = None, numbers: set[str] | None = None, extra_enums: Mapping[str, Any] | None = None) -> dict[str, Any]:
    mappings, integers, numbers = mappings or set(), integers or set(), numbers or set()
    extra_enums = dict(extra_enums or {})
    allowed = strings | lists | mappings | integers | numbers | {enum[0]} | set(extra_enums)
    unknown = sorted(set(changes) - allowed)
    if unknown or not changes:
        raise MutationError("invalid_argument", "unsupported or empty changes", details={"fields": unknown})
    result: dict[str, Any] = {}
    for key, value in changes.items():
        if key == enum[0] or key in extra_enums:
            raw = _required_text(value, key).lower()
            enum_type = enum[1] if key == enum[0] else extra_enums[key]
            try: result[key] = enum_type(raw).value
            except ValueError as exc: raise MutationError("invalid_argument", f"invalid {key} '{raw}'", details={"allowed": [item.value for item in enum_type]}) from exc
        elif key in lists:
            if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value): raise MutationError("invalid_argument", f"{key} must be a list of non-empty strings")
            result[key] = list(dict.fromkeys(item.strip() for item in value))
        elif key in mappings:
            if not isinstance(value, Mapping): raise MutationError("invalid_argument", f"{key} must be an object")
            result[key] = dict(value)
        elif key in integers:
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)): raise MutationError("invalid_argument", f"{key} must be an integer or null")
            result[key] = value
        elif key in numbers:
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))): raise MutationError("invalid_argument", f"{key} must be a number or null")
            result[key] = None if value is None else float(value)
        elif key in nullable and value is None: result[key] = None
        else: result[key] = _required_text(value, key)
    return result


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _now(value: datetime | None = None) -> datetime:
    if value is None: return datetime.now(timezone.utc)
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _file_fingerprint(path: Path) -> str:
    if not path.exists(): return record_fingerprint(None)
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
