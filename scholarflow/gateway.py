"""Lightweight gateway state and inbound routing for long-running ScholarFlow use."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .confirmation_store import (
    approve_pairing,
    is_pairing_approved,
    request_pairing,
)
from .event_bus import append_event
from .persistence import atomic_write_json
from .scheduler import run_due_jobs
from .storage import ScholarFlowStore


def gateway_state_path(store_root: Path) -> Path:
    return Path(store_root) / "gateway_state.json"


def gateway_inbox_path(store_root: Path) -> Path:
    return Path(store_root) / "gateway_inbox.json"


def list_gateway_inbox(store_root: Path, *, status: str = "pending", limit: int = 100) -> dict[str, Any]:
    path = gateway_inbox_path(store_root)
    if not path.exists():
        return {"count": 0, "items": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = [item for item in payload.get("items", []) if not status or item.get("status") == status]
    normalized_limit = max(1, min(int(limit), 100))
    items = items[-normalized_limit:]
    return {"count": len(items), "items": items}


def get_gateway_message(store_root: Path, *, message_id: str) -> dict[str, Any]:
    path = gateway_inbox_path(store_root)
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload.get("items", []):
            if item.get("message_id") == message_id:
                return dict(item)
    raise ValueError(f"gateway message '{message_id}' was not found")


def enqueue_workflow_request(
    store_root: Path,
    *,
    skill_name: str,
    payload: dict[str, Any] | None = None,
    source: str = "scheduler",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Queue a Pi-native workflow request targeted at a governed Skill lane."""

    normalized_skill = str(skill_name or "").strip()
    if not normalized_skill:
        raise ValueError("skill_name is required")
    workflow_payload = dict(payload or {})
    workflow_type = str(workflow_payload.pop("workflow_type", "")).strip() or "scheduled_workflow"
    instruction = str(workflow_payload.pop("instruction", "")).strip()
    if not instruction:
        instruction = f"Use the {normalized_skill} Skill to process the queued ScholarFlow workflow request."
    return enqueue_pi_workflow(
        store_root,
        workflow_type=workflow_type,
        instruction=instruction,
        payload=workflow_payload,
        source=source,
        now=now,
        skill_name=normalized_skill,
    )


def enqueue_pi_workflow(
    store_root: Path,
    *,
    workflow_type: str,
    instruction: str,
    payload: dict[str, Any] | None = None,
    source: str = "scheduler",
    now: datetime | None = None,
    skill_name: str | None = None,
) -> dict[str, Any]:
    """Queue deterministic automation work for Pi without running a model."""

    effective_now = _coerce_now(now)
    normalized_type = str(workflow_type or "").strip()
    normalized_instruction = str(instruction or "").strip()
    normalized_skill = str(skill_name or "").strip() or None
    if not normalized_type or not normalized_instruction:
        raise ValueError("workflow_type and instruction are required")
    workflow_payload = dict(payload or {})
    identity = json.dumps(
        {
            "workflow_type": normalized_type,
            "instruction": normalized_instruction,
            "payload": workflow_payload,
            "skill_name": normalized_skill,
            "scheduled_at": effective_now.isoformat(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    message_id = f"wf-{normalized_type}-{effective_now.strftime('%Y%m%d%H%M%S')}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:8]}"
    item = {
        "message_id": message_id,
        "sender_id": f"automation:{normalized_type}",
        "source": source,
        "raw_input": normalized_instruction,
        "workflow_type": normalized_type,
        "skill_name": normalized_skill,
        "workflow_payload": workflow_payload,
        "status": "pending",
        "received_at": effective_now.isoformat(),
    }
    path = gateway_inbox_path(store_root)
    inbox = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "items": []}
    existing = next((entry for entry in inbox["items"] if entry.get("message_id") == message_id), None)
    if existing is not None:
        return dict(existing)
    inbox["items"].append(item)
    atomic_write_json(path, inbox)
    state = load_gateway_state(store_root)
    state["queued_count"] = int(state.get("queued_count") or 0) + 1
    save_gateway_state(store_root, state)
    append_event(
        store_root,
        event_type="automation.workflow.queued_for_pi",
        payload={
            "message_id": message_id,
            "workflow_type": normalized_type,
            "skill_name": normalized_skill,
            "source": source,
        },
        now=effective_now,
    )
    return item


def acknowledge_gateway_message(
    store_root: Path,
    *,
    message_id: str,
    outcome: str = "processed",
    note: str | None = None,
    confirmed: bool = False,
    confirmed_by: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mark one queued message after Pi has handled it with user confirmation."""

    if not confirmed:
        raise ValueError("acknowledging a gateway message requires explicit confirmation")
    normalized_outcome = str(outcome or "").strip().lower()
    if normalized_outcome not in {"processed", "dismissed", "failed"}:
        raise ValueError("outcome must be processed, dismissed, or failed")
    path = gateway_inbox_path(store_root)
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "items": []}
    effective_now = _coerce_now(now)
    selected: dict[str, Any] | None = None
    was_pending = False
    for item in payload.get("items", []):
        if item.get("message_id") != message_id:
            continue
        selected = item
        was_pending = item.get("status") == "pending"
        if was_pending:
            item["status"] = normalized_outcome
            item["acknowledged_at"] = effective_now.isoformat()
            item["acknowledged_by"] = str(confirmed_by or "interactive_user")
            if note:
                item["outcome_note"] = str(note).strip()
        break
    if selected is None:
        raise ValueError(f"gateway message '{message_id}' was not found")
    atomic_write_json(path, payload)
    state = load_gateway_state(store_root)
    if was_pending and normalized_outcome == "processed":
        state["processed_count"] = int(state.get("processed_count") or 0) + 1
        save_gateway_state(store_root, state)
    append_event(
        store_root,
        event_type="gateway.message.acknowledged",
        payload={"message_id": message_id, "outcome": normalized_outcome, "confirmed_by": confirmed_by},
        now=effective_now,
    )
    return dict(selected)


def load_gateway_state(store_root: Path) -> dict[str, Any]:
    path = gateway_state_path(store_root)
    if not path.exists():
        return _default_gateway_state()
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key, value in _default_gateway_state().items():
        payload.setdefault(key, value)
    return payload


def save_gateway_state(store_root: Path, payload: dict[str, Any]) -> None:
    path = gateway_state_path(store_root)
    atomic_write_json(path, payload)


def start_gateway(store_root: Path, *, trust_mode: str = "pairing", now: datetime | None = None) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    state = load_gateway_state(store_root)
    state["status"] = "running"
    state["trust_mode"] = trust_mode
    state["last_started_at"] = effective_now.isoformat()
    save_gateway_state(store_root, state)
    return dict(state)


def stop_gateway(store_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    state = load_gateway_state(store_root)
    state["status"] = "stopped"
    state["last_stopped_at"] = effective_now.isoformat()
    save_gateway_state(store_root, state)
    return dict(state)


def gateway_status(store_root: Path) -> dict[str, Any]:
    return load_gateway_state(store_root)


def receive_gateway_input(
    store: ScholarFlowStore,
    *,
    sender_id: str,
    raw_input: str,
    source: str = "gateway",
    trust_mode: str | None = None,
    vault_root: Path | None = None,
    auto_kb_sync: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    state = load_gateway_state(store.root_dir)
    if state.get("status") != "running":
        return {
            "status": "gateway_stopped",
            "reason": "gateway is not running",
            "gateway": state,
        }

    sender_id = str(sender_id or "").strip()
    raw_input = str(raw_input or "").strip()
    if not sender_id or not raw_input:
        raise ValueError("sender_id and raw_input are required")

    effective_trust_mode = str(trust_mode or state.get("trust_mode") or "pairing")
    state["last_received_at"] = effective_now.isoformat()

    if effective_trust_mode == "pairing" and not is_pairing_approved(store.root_dir, sender_id=sender_id, source=source):
        pairing = request_pairing(
            store.root_dir,
            sender_id=sender_id,
            source=source,
            preview=raw_input[:160],
            now=effective_now,
        )
        state["blocked_count"] = int(state.get("blocked_count") or 0) + 1
        save_gateway_state(store.root_dir, state)
        append_event(
            store.root_dir,
            event_type="gateway.receive.blocked",
            payload={
                "sender_id": sender_id,
                "source": source,
                "pairing_id": pairing["pairing_id"],
                "pairing_code": pairing["pairing_code"],
            },
            now=effective_now,
        )
        return {
            "status": "awaiting_pairing",
            "gateway": state,
            "pairing": pairing,
            "message": "陌生发送者已被门控，需先批准 pairing 才会进入执行链路。",
        }

    request_id = _build_request_id(source=source, sender_id=sender_id, raw_input=raw_input, now=effective_now)
    inbox_path = gateway_inbox_path(store.root_dir)
    inbox = json.loads(inbox_path.read_text(encoding="utf-8")) if inbox_path.exists() else {"version": 1, "items": []}
    item = {
        "message_id": request_id,
        "sender_id": sender_id,
        "source": source,
        "raw_input": raw_input,
        "status": "pending",
        "received_at": effective_now.isoformat(),
    }
    existing_item = next((existing for existing in inbox["items"] if existing.get("message_id") == request_id), None)
    if existing_item is None:
        inbox["items"].append(item)
        atomic_write_json(inbox_path, inbox)
        append_event(
            store.root_dir,
            event_type="gateway.receive.queued_for_pi",
            payload={
                "request_id": request_id,
                "sender_id": sender_id,
                "source": source,
            },
            now=effective_now,
        )
        state["queued_count"] = int(state.get("queued_count") or 0) + 1
        save_gateway_state(store.root_dir, state)
    else:
        item = existing_item
    return {
        "status": "queued_for_pi",
        "gateway": state,
        "request_id": request_id,
        "message": item,
    }


def approve_gateway_pairing(
    store_root: Path,
    *,
    pairing_id: str,
    approved_by: str = "user",
    now: datetime | None = None,
) -> dict[str, Any]:
    approved = approve_pairing(store_root, pairing_id=pairing_id, approved_by=approved_by, now=now)
    append_event(
        store_root,
        event_type="gateway.pairing.approved",
        payload={
            "pairing_id": approved["pairing_id"],
            "sender_id": approved["sender_id"],
            "source": approved["source"],
        },
        now=now,
    )
    return approved


def poll_gateway_once(
    store: ScholarFlowStore,
    *,
    vault_root: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    gateway_poll = poll_gateway_inbox_once(store.root_dir, now=effective_now)
    if gateway_poll.get("status") == "gateway_stopped":
        return gateway_poll
    automation = run_due_jobs(store, vault_root=vault_root, now=effective_now)
    return {
        "status": "polled",
        "gateway": gateway_poll["gateway"],
        "gateway_poll": gateway_poll,
        "automation": automation,
    }


def poll_gateway_inbox_once(store_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    state = load_gateway_state(store_root)
    if state.get("status") != "running":
        return {
            "status": "gateway_stopped",
            "reason": "gateway is not running",
            "gateway": state,
        }
    state["last_polled_at"] = effective_now.isoformat()
    pending = list_gateway_inbox(store_root, status="pending", limit=100)
    save_gateway_state(store_root, state)
    append_event(
        store_root,
        event_type="gateway.poll",
        payload={
            "mode": "inbox",
            "polled_message_count": pending["count"],
        },
        now=effective_now,
    )
    return {
        "status": "polled",
        "gateway": state,
        "polled_message_count": pending["count"],
        "items": pending["items"],
    }


def _default_gateway_state() -> dict[str, Any]:
    return {
        "status": "stopped",
        "trust_mode": "pairing",
        "processed_count": 0,
        "queued_count": 0,
        "blocked_count": 0,
        "last_started_at": None,
        "last_stopped_at": None,
        "last_received_at": None,
        "last_polled_at": None,
    }


def _build_request_id(*, source: str, sender_id: str, raw_input: str, now: datetime) -> str:
    sanitized = "".join(char if char.isalnum() else "-" for char in sender_id).strip("-") or "sender"
    digest = hashlib.sha256(raw_input.encode("utf-8")).hexdigest()[:8]
    return f"gw-{source}-{sanitized}-{now.strftime('%Y%m%d%H%M%S')}-{digest}"


def _coerce_now(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)
