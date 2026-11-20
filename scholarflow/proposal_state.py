"""Shared persistence and governance helpers for all mutation proposal lanes."""

from __future__ import annotations

import json
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Mapping

from .persistence import atomic_write_json, file_lock


@contextmanager
def proposal_application_lock(root_dir: Path):
    """Serialize terminal decisions; entity CAS also guards ordinary writers."""
    root = Path(root_dir)
    root.mkdir(parents=True, exist_ok=True)
    with file_lock(root / "proposal-applications", timeout_seconds=30.0):
        yield


def serialized_proposal_apply(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with proposal_application_lock(self.root_dir):
            return method(self, *args, **kwargs)
    return wrapped


def publish_domain_receipt(root_dir: Path, proposal: Mapping[str, Any], receipt: Mapping[str, Any], state: dict[str, Any], request_id: str | None):
    from .event_bus import append_event

    response = dict(receipt)
    event = append_event(
        root_dir, event_type="pi.domain.mutation.committed",
        event_id=str(receipt["audit_event_id"]),
        payload={"proposal_id": proposal["proposal_id"], "operation": proposal["operation"], "target": proposal["target"], "pi_tool_call_id": request_id},
    )
    state["commits"][proposal["proposal_id"]] = {
        "committed_at": receipt["committed_at"],
        "event_id": event["event_id"], "response": response,
    }
    save_proposal_state(root_dir, state)
    return response


def proposal_state_path(root_dir: Path) -> Path:
    return Path(root_dir) / "pi_mutations.json"


def _load_proposal_state_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": 2,
            "revision": 0,
            "proposals": {},
            "commits": {},
            "rejections": {},
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = 2
    payload.setdefault("revision", 0)
    payload.setdefault("proposals", {})
    payload.setdefault("commits", {})
    payload.setdefault("rejections", {})
    return payload


def load_proposal_state(root_dir: Path) -> dict[str, Any]:
    """Read a complete snapshot while an atomic Windows replacement may run."""
    path = proposal_state_path(root_dir)
    with file_lock(path):
        return _load_proposal_state_unlocked(path)


def save_proposal_state(root_dir: Path, payload: Mapping[str, Any]) -> None:
    path = proposal_state_path(root_dir)
    with file_lock(path):
        current = _load_proposal_state_unlocked(path)
        incoming = dict(payload)
        # Proposal state is append-only. Merge under the lock so two Pi calls
        # based on stale snapshots cannot erase each other's proposal/commit.
        state = {
            **current,
            **incoming,
            "version": 2,
            "revision": max(int(current.get("revision", 0)), int(incoming.get("revision", 0))) + 1,
            "proposals": {**current.get("proposals", {}), **incoming.get("proposals", {})},
            "commits": {**current.get("commits", {}), **incoming.get("commits", {})},
            "rejections": {
                **current.get("rejections", {}),
                **incoming.get("rejections", {}),
            },
            **{key: {**current.get(key, {}), **incoming.get(key, {})}
               for key in ("harness_requests", "harness_bindings", "harness_decisions", "projection_steps")},
        }
        # A late caller may carry an older intent snapshot. A completed
        # external step is a historical receipt and must never regress.
        for proposal_id, step in current.get("projection_steps", {}).items():
            if step.get("phase") == "completed":
                state["projection_steps"][proposal_id] = step
            elif "steps" in step:
                # Multi-file receipts are monotonic individually, even while
                # the overall operation remains partial. A stale snapshot
                # must not erase a completed file or an outstanding intent.
                newer=state["projection_steps"].get(proposal_id, {})
                merged={**step.get("steps",{}),**newer.get("steps",{})}
                for relative_path,receipt in step.get("steps",{}).items():
                    if receipt.get("phase")=="completed": merged[relative_path]=receipt
                state["projection_steps"][proposal_id]={**step,**newer,"steps":merged}
        atomic_write_json(path, state)


def reject_proposal(
    root_dir: Path,
    proposal_id: str,
    *,
    rejected_by: str,
    rejected_at: str,
    reason: str,
    request_id: str,
) -> dict[str, Any]:
    """Persist one terminal, idempotent user rejection under the state lock."""
    path = proposal_state_path(root_dir)
    with proposal_application_lock(root_dir), file_lock(path):
        state = _load_proposal_state_unlocked(path)
        from .storage import ScholarFlowStore
        proposal = state["proposals"].get(proposal_id)
        durable = ScholarFlowStore(root_dir).domain_proposal_receipt(proposal) if proposal else None
        if proposal_id in state["commits"] or durable is not None:
            raise ValueError("proposal_already_committed")
        proposal = state["proposals"].get(proposal_id)
        if proposal is None:
            raise KeyError(proposal_id)
        existing = state["rejections"].get(proposal_id)
        if existing is not None:
            return {**existing, "idempotent_replay": True}
        receipt = {
            "proposal_id": proposal_id,
            "operation": proposal.get("operation"),
            "target": proposal.get("target", {}),
            "decision": "rejected",
            "status": "rejected",
            "rejected_by": rejected_by,
            "rejected_at": rejected_at,
            "reason": reason,
            "request_id": request_id,
            "idempotent_replay": False,
        }
        state["rejections"][proposal_id] = receipt
        state["revision"] = int(state.get("revision", 0)) + 1
        atomic_write_json(path, state)
        return receipt


def raise_if_rejected(state: Mapping[str, Any], proposal_id: str) -> None:
    rejection = state.get("rejections", {}).get(proposal_id)
    if rejection is not None:
        from .mutations import MutationError

        raise MutationError(
            "proposal_rejected",
            "the proposal was rejected by the user",
            details={
                "proposal_id": proposal_id,
                "rejected_at": rejection.get("rejected_at"),
            },
        )


def record_rejection_audit_event(
    root_dir: Path,
    proposal_id: str,
    event_id: str,
) -> dict[str, Any]:
    path = proposal_state_path(root_dir)
    with file_lock(path):
        state = _load_proposal_state_unlocked(path)
        receipt = state["rejections"].get(proposal_id)
        if receipt is None:
            raise KeyError(proposal_id)
        updated = {**receipt, "audit_event_id": event_id}
        state["rejections"][proposal_id] = updated
        state["revision"] = int(state.get("revision", 0)) + 1
        atomic_write_json(path, state)
        return updated


def valid_confirmation(receipt: Mapping[str, Any] | None, proposal_id: str) -> bool:
    return bool(
        receipt
        and receipt.get("proposal_id") == proposal_id
        and receipt.get("confirmed") is True
        and receipt.get("confirmed_by") == "interactive_user"
        and str(receipt.get("confirmed_at") or "").strip()
        and str(receipt.get("pi_tool_call_id") or "").strip()
    )


def find_pending_target_proposal(
    proposals: Mapping[str, Any],
    *,
    operation: str,
    target_field: str,
    object_id: str,
    proposal_id: str,
    terminal_ids: set[str] | None = None,
) -> Mapping[str, Any] | None:
    terminal = terminal_ids or set()
    for candidate in proposals.values():
        if candidate.get("proposal_id") == proposal_id:
            continue
        if candidate.get("proposal_id") in terminal:
            continue
        if candidate.get("operation") != operation:
            continue
        if candidate.get("target", {}).get(target_field) == object_id:
            return candidate
    return None
