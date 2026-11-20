"""Append-only local event bus for runtime and automation events."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .persistence import file_lock


def event_log_path(store_root: Path) -> Path:
    return Path(store_root) / "runtime_events.jsonl"


def append_event(
    store_root: Path,
    *,
    event_type: str,
    payload: dict[str, Any],
    event_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    event = {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": event_type,
        "timestamp": effective_now.isoformat(),
        "payload": dict(payload),
    }
    path = event_log_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        if event_id and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if existing.get("event_id") == event_id:
                    return existing
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return event


def list_events(
    store_root: Path,
    *,
    event_type: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    path = event_log_path(store_root)
    if not path.exists():
        return {"count": 0, "items": []}
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if event_type and str(item.get("event_type") or "") != event_type:
            continue
        items.append(item)
    items = items[-max(0, int(limit)) :]
    return {"count": len(items), "items": items}


def _coerce_now(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)
