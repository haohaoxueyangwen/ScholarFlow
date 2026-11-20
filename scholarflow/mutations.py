"""Governed, two-step mutations exposed to the Pi runtime."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .enums import TaskStatus
from .event_bus import append_event
from .models import Task
from .proposal_state import (
    find_pending_target_proposal as _find_pending_target_proposal,
    load_proposal_state as _load_state,
    raise_if_rejected as _raise_if_rejected,
    save_proposal_state as _save_state,
    valid_confirmation as _valid_confirmation,
    serialized_proposal_apply,
    publish_domain_receipt,
)
from .storage import ScholarFlowStore

PROPOSAL_VERSION = 1
PROPOSAL_TTL = timedelta(hours=24)
NULL_FINGERPRINT = "sha256:" + hashlib.sha256(b"null").hexdigest()
TASK_MUTABLE_FIELDS = {
    "title",
    "description",
    "project_stage_id",
    "source",
    "priority",
    "status",
    "owner",
    "related_experiments",
    "related_papers",
    "due_hint",
}
TASK_LIST_FIELDS = {"related_experiments", "related_papers"}
TASK_NULLABLE_FIELDS = TASK_MUTABLE_FIELDS - {"title", "status"} - TASK_LIST_FIELDS


class MutationError(ValueError):
    """Stable mutation failure returned across the Pi bridge."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class TaskMutationService:
    """Create and commit concurrency-safe task update proposals."""

    def __init__(self, store: ScholarFlowStore):
        self.store = store
        self.root_dir = store.root_dir

    def propose_update(
        self,
        task_id: str,
        changes: Mapping[str, Any],
        *,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_id = _required_text(task_id, "task_id")
        current = self.store.get_task(normalized_id)
        if current is None:
            raise MutationError("not_found", f"task '{normalized_id}' was not found", details={"task_id": normalized_id})
        normalized_changes = self._normalize_changes(changes)
        current_record = current.to_dict()
        effective_changes = {
            key: value for key, value in normalized_changes.items() if current_record.get(key) != value
        }
        if not effective_changes:
            raise MutationError("no_changes", "the proposed task update would not change the record")

        base_fingerprint = record_fingerprint(current_record)
        identity = {
            "operation": "task.update",
            "target": {"task_id": normalized_id},
            "normalized_changes": effective_changes,
            "base_fingerprint": base_fingerprint,
        }
        proposal_id = "sha256:" + hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
        store = _load_state(self.root_dir)
        existing = store["proposals"].get(proposal_id)
        if existing is not None:
            return dict(existing)

        effective_now = _coerce_now(now)
        proposal = {
            "proposal_version": PROPOSAL_VERSION,
            "proposal_id": proposal_id,
            **identity,
            "diff": {
                key: {"before": current_record.get(key), "after": value}
                for key, value in effective_changes.items()
            },
            "policy": {
                "side_effect_class": "domain_write",
                "requires_confirmation": True,
                "reasons": ["Pi-initiated business-record update"],
            },
            "warnings": [],
            "created_at": effective_now.isoformat(),
            "expires_at": (effective_now + PROPOSAL_TTL).isoformat(),
        }
        store["proposals"][proposal_id] = proposal
        _save_state(self.root_dir, store)
        append_event(
            self.root_dir,
            event_type="pi.domain.mutation.proposed",
            payload={
                "proposal_id": proposal_id,
                "operation": "task.update",
                "task_id": normalized_id,
                "pi_tool_call_id": request_id,
                "requires_confirmation": True,
            },
        )
        return proposal

    def propose_create(
        self, task_id: str, values: Mapping[str, Any], *, request_id: str | None = None, now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_id = _required_text(task_id, "task_id")
        if self.store.get_task(normalized_id) is not None:
            raise MutationError("already_exists", f"task '{normalized_id}' already exists", details={"task_id": normalized_id})
        normalized = self._normalize_changes(values)
        if "title" not in normalized:
            raise MutationError("invalid_argument", "title is required when creating a task")
        normalized.setdefault("status", TaskStatus.PENDING.value)
        identity = {
            "operation": "task.create",
            "target": {"task_id": normalized_id},
            "normalized_changes": normalized,
            "base_fingerprint": NULL_FINGERPRINT,
        }
        proposal_id = "sha256:" + hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
        state = _load_state(self.root_dir)
        if proposal_id in state["proposals"]:
            return dict(state["proposals"][proposal_id])
        conflicting = _find_pending_target_proposal(
            state["proposals"],
            operation="task.create",
            target_field="task_id",
            object_id=normalized_id,
            proposal_id=proposal_id,
            terminal_ids=set(state["commits"]) | set(state["rejections"]),
        )
        if conflicting is not None:
            raise MutationError(
                "id_reserved",
                f"task '{normalized_id}' already has an uncommitted create proposal",
                details={"task_id": normalized_id, "proposal_id": conflicting["proposal_id"]},
            )
        effective_now = _coerce_now(now)
        proposal = {
            "proposal_version": PROPOSAL_VERSION, "proposal_id": proposal_id, **identity,
            "diff": {key: {"before": None, "after": value} for key, value in normalized.items()},
            "policy": {"side_effect_class": "domain_write", "requires_confirmation": True, "reasons": ["Pi-initiated business-record creation"]},
            "warnings": [], "created_at": effective_now.isoformat(), "expires_at": (effective_now + PROPOSAL_TTL).isoformat(),
        }
        state["proposals"][proposal_id] = proposal
        _save_state(self.root_dir, state)
        append_event(self.root_dir, event_type="pi.domain.mutation.proposed", payload={
            "proposal_id": proposal_id, "operation": "task.create", "task_id": normalized_id,
            "pi_tool_call_id": request_id, "requires_confirmation": True,
        })
        return proposal

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        normalized_id = _required_text(proposal_id, "proposal_id")
        proposal = _load_state(self.root_dir)["proposals"].get(normalized_id)
        if proposal is None:
            raise MutationError(
                "not_found",
                f"mutation proposal '{normalized_id}' was not found",
                details={"proposal_id": normalized_id},
            )
        return dict(proposal)

    @serialized_proposal_apply
    def apply_update(
        self,
        proposal_id: str,
        *,
        confirmation: Mapping[str, Any] | None,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_id = _required_text(proposal_id, "proposal_id")
        state = _load_state(self.root_dir)
        committed = state["commits"].get(normalized_id)
        if committed is not None:
            replay = dict(committed["response"])
            replay["idempotent_replay"] = True
            replay_operation = replay.get("operation", "task.update")
            append_event(
                self.root_dir,
                event_type="pi.domain.mutation.replayed",
                payload={"proposal_id": normalized_id, "operation": replay_operation, "pi_tool_call_id": request_id},
            )
            return replay
        _raise_if_rejected(state, normalized_id)

        proposal = state["proposals"].get(normalized_id)
        if proposal is None:
            raise MutationError("not_found", f"mutation proposal '{normalized_id}' was not found")
        durable = self.store.domain_proposal_receipt(proposal)
        if durable is not None:
            recovered = publish_domain_receipt(self.root_dir, proposal, durable, state, request_id)
            return {**recovered, "idempotent_replay": True}
        effective_now = _coerce_now(now)
        if effective_now > datetime.fromisoformat(proposal["expires_at"]):
            self._reject("expired_proposal", proposal, request_id)
        operation = proposal.get("operation")
        if operation not in {"task.update", "task.create"}:
            self._reject("invalid_proposal", proposal, request_id)
        if proposal["policy"].get("requires_confirmation") and not _valid_confirmation(confirmation, normalized_id):
            self._reject("confirmation_required", proposal, request_id)

        task_id = proposal["target"]["task_id"]
        current = self.store.get_task(task_id)
        current_fingerprint = record_fingerprint(current.to_dict()) if current is not None else NULL_FINGERPRINT
        if current_fingerprint != proposal["base_fingerprint"]:
            append_event(
                self.root_dir,
                event_type="pi.domain.mutation.stale",
                payload={"proposal_id": normalized_id, "operation": operation, "task_id": task_id, "pi_tool_call_id": request_id},
            )
            raise MutationError(
                "stale_proposal",
                "the task changed after this proposal was created",
                details={"proposal_id": normalized_id, "task_id": task_id},
            )

        receipt = dict(confirmation or {})
        confirmed_event = append_event(
            self.root_dir,
            event_type="pi.domain.mutation.confirmed",
            payload={
                "proposal_id": normalized_id,
                "operation": operation,
                "task_id": task_id,
                "pi_tool_call_id": request_id,
                "confirmed_by": receipt.get("confirmed_by"),
                "confirmed_at": receipt.get("confirmed_at"),
            },
        )
        del confirmed_event
        durable = self.store.apply_domain_proposal(proposal)
        return publish_domain_receipt(self.root_dir, proposal, durable, state, request_id)

    def _normalize_changes(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(changes, Mapping):
            raise MutationError("invalid_argument", "changes must be an object")
        unknown = sorted(set(changes) - TASK_MUTABLE_FIELDS)
        if unknown:
            raise MutationError("invalid_argument", "unsupported task fields", details={"fields": unknown})
        if not changes:
            raise MutationError("invalid_argument", "at least one task field is required")

        normalized: dict[str, Any] = {}
        for key, value in changes.items():
            if key == "status":
                raw = _required_text(value, "status").lower()
                try:
                    normalized[key] = TaskStatus(raw).value
                except ValueError as exc:
                    raise MutationError(
                        "invalid_argument",
                        f"invalid task status '{raw}'",
                        details={"allowed": [item.value for item in TaskStatus]},
                    ) from exc
            elif key in TASK_LIST_FIELDS:
                if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                    raise MutationError("invalid_argument", f"{key} must be a list of non-empty strings")
                normalized[key] = list(dict.fromkeys(item.strip() for item in value))
            elif key in TASK_NULLABLE_FIELDS and value is None:
                normalized[key] = None
            else:
                normalized[key] = _required_text(value, key)

        stage_id = normalized.get("project_stage_id")
        if stage_id and self.store.get_project_stage(stage_id) is None:
            raise MutationError("invalid_reference", f"project_stage '{stage_id}' was not found", details={"project_stage_id": stage_id})
        return normalized

    def _reject(self, code: str, proposal: Mapping[str, Any], request_id: str | None) -> None:
        append_event(
            self.root_dir,
            event_type="pi.domain.mutation.rejected",
            payload={
                "proposal_id": proposal.get("proposal_id"),
                "operation": proposal.get("operation"),
                "pi_tool_call_id": request_id,
                "reason": code,
            },
        )
        messages = {
            "expired_proposal": "the mutation proposal has expired",
            "invalid_proposal": "the proposal operation is not supported by this apply endpoint",
            "confirmation_required": "interactive user confirmation is required",
        }
        raise MutationError(code, messages[code], details={"proposal_id": proposal.get("proposal_id")})


def record_fingerprint(record: Mapping[str, Any] | None) -> str:
    if record is None:
        return NULL_FINGERPRINT
    return "sha256:" + hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()


def required_text(value: Any, field: str) -> str:
    normalized = str(value).strip() if value is not None else ""
    if not normalized:
        raise MutationError("invalid_argument", f"{field} must be a non-empty string", details={"field": field})
    return normalized


_required_text = required_text


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _coerce_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
