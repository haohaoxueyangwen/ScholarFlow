"""Persistent confirmation and pairing governance for ScholarFlow."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .persistence import atomic_write_json


def pending_confirmations_path(store_root: Path) -> Path:
    return Path(store_root) / "pending_confirmations.json"


def load_confirmation_store(store_root: Path) -> dict[str, Any]:
    path = pending_confirmations_path(store_root)
    if not path.exists():
        return {"version": 4, "items": [], "history": [], "pairings": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("version", 4)
    payload.setdefault("items", [])
    payload.setdefault("history", [])
    payload.setdefault("pairings", [])
    return payload


def save_confirmation_store(store_root: Path, payload: dict[str, Any]) -> None:
    path = pending_confirmations_path(store_root)
    atomic_write_json(path, payload)


def record_pending_confirmations(store_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    pending = load_confirmation_store(store_root)
    items = list(pending.get("items", []))
    request_id = str((payload.get("request") or {}).get("request_id") or "")
    if (
        payload.get("final_status") == "awaiting_confirmation"
        and str((payload.get("agent_policy") or {}).get("confirmation_scope") or "") == "planning"
    ):
        confirmation_id = f"{request_id}:planning:confirm_plan"
        if any(existing.get("confirmation_id") == confirmation_id for existing in items) is False:
            items.append(
                {
                    "confirmation_id": confirmation_id,
                    "request_id": request_id,
                    "status": "awaiting_confirmation",
                    "decision": "needs_human_confirm",
                    "object_ref": f"request:{request_id}",
                    "title": str((payload.get("agent_plan") or {}).get("primary_action") or "planning_confirmation"),
                    "reason": str((payload.get("clarification") or {}).get("assistant_message") or payload.get("note") or ""),
                    "action": "confirm_plan",
                    "confirmation_scope": "planning",
                    "request": dict(payload.get("request") or {}),
                    "auto_kb_sync": False,
                }
            )
    for index, item in enumerate(payload.get("kb_sync_results", [])):
        if item.get("decision") != "needs_human_confirm":
            continue
        object_ref = str(item.get("object_ref") or "")
        action = str(item.get("action") or "")
        confirmation_id = f"{request_id}:{index}:{action or 'confirm'}"
        if any(existing.get("confirmation_id") == confirmation_id for existing in items):
            continue
        items.append(
            {
                "confirmation_id": confirmation_id,
                "request_id": request_id,
                "status": item.get("status") or "awaiting_confirmation",
                "decision": item.get("decision"),
                "object_ref": object_ref,
                "title": item.get("title"),
                "reason": item.get("reason"),
                "action": action,
                "confirmation_scope": "merge" if object_ref.startswith("kb_merge:") else "writeback",
                "merge_from": item.get("merge_from"),
                "merge_into": item.get("merge_into"),
            }
        )
    pending["items"] = items
    save_confirmation_store(store_root, pending)
    return {"count": len(items), "items": items}


def list_pending_confirmations(store_root: Path) -> dict[str, Any]:
    pending = load_confirmation_store(store_root)
    items = list(pending.get("items", []))
    return {"count": len(items), "items": items}


def list_confirmation_history(store_root: Path) -> dict[str, Any]:
    pending = load_confirmation_store(store_root)
    history = list(pending.get("history", []))
    return {"count": len(history), "items": history}


def get_pending_confirmation(store_root: Path, confirmation_id: str) -> dict[str, Any] | None:
    pending = load_confirmation_store(store_root)
    return next((item for item in pending.get("items", []) if item.get("confirmation_id") == confirmation_id), None)


def remove_pending_confirmation(store_root: Path, confirmation_id: str) -> dict[str, Any]:
    pending = load_confirmation_store(store_root)
    items = list(pending.get("items", []))
    pending["items"] = [item for item in items if item.get("confirmation_id") != confirmation_id]
    save_confirmation_store(store_root, pending)
    return pending


def append_confirmation_history(store_root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    pending = load_confirmation_store(store_root)
    history = list(pending.get("history", []))
    history.append(dict(entry))
    pending["history"] = history
    save_confirmation_store(store_root, pending)
    return {"count": len(history), "items": history}


def list_pairings(store_root: Path, *, status: str | None = None) -> dict[str, Any]:
    pending = load_confirmation_store(store_root)
    items = list(pending.get("pairings", []))
    if status:
        items = [item for item in items if str(item.get("status") or "") == status]
    return {"count": len(items), "items": items}


def request_pairing(
    store_root: Path,
    *,
    sender_id: str,
    source: str,
    preview: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    pending = load_confirmation_store(store_root)
    pairings = list(pending.get("pairings", []))
    pairing_id = f"pairing:{source}:{sender_id}"
    existing = next((item for item in pairings if item.get("pairing_id") == pairing_id), None)
    if existing is not None:
        if existing.get("status") == "approved":
            return existing
        existing["last_seen_at"] = effective_now.isoformat()
        existing["preview"] = preview
        pending["pairings"] = pairings
        save_confirmation_store(store_root, pending)
        return existing
    pairing = {
        "pairing_id": pairing_id,
        "sender_id": sender_id,
        "source": source,
        "status": "awaiting_approval",
        "pairing_code": _pairing_code(source=source, sender_id=sender_id),
        "preview": preview,
        "created_at": effective_now.isoformat(),
        "last_seen_at": effective_now.isoformat(),
    }
    pairings.append(pairing)
    pending["pairings"] = pairings
    save_confirmation_store(store_root, pending)
    return pairing


def approve_pairing(
    store_root: Path,
    *,
    pairing_id: str,
    approved_by: str = "user",
    now: datetime | None = None,
) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    pending = load_confirmation_store(store_root)
    pairings = list(pending.get("pairings", []))
    target = next((item for item in pairings if item.get("pairing_id") == pairing_id), None)
    if target is None:
        raise ValueError(f"pairing '{pairing_id}' was not found")
    target["status"] = "approved"
    target["approved_by"] = approved_by
    target["approved_at"] = effective_now.isoformat()
    pending["pairings"] = pairings
    save_confirmation_store(store_root, pending)
    return dict(target)


def is_pairing_approved(store_root: Path, *, sender_id: str, source: str) -> bool:
    pending = load_confirmation_store(store_root)
    for item in pending.get("pairings", []):
        if item.get("sender_id") == sender_id and item.get("source") == source and item.get("status") == "approved":
            return True
    return False


def _pairing_code(*, source: str, sender_id: str) -> str:
    digest = hashlib.sha1(f"{source}:{sender_id}".encode("utf-8")).hexdigest()
    return digest[:6].upper()


def _coerce_now(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)
