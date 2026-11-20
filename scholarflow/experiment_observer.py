"""Read-only observation of stored experiment evidence files."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .domain_queries import DomainQueryError
from .event_bus import append_event
from .storage import ScholarFlowStore


MAX_TAIL_CHARS = 20_000


def configured_observation_roots() -> list[Path]:
    raw = os.environ.get("SCHOLARFLOW_OBSERVE_ROOTS", "")
    return [Path(item).expanduser().resolve() for item in raw.split(os.pathsep) if item.strip()]


def observe_experiment(
    store: ScholarFlowStore,
    experiment_id: str,
    *,
    allowed_roots: Iterable[Path] | None = None,
    include_tail: bool = False,
    tail_lines: int = 50,
    request_id: str | None = None,
) -> dict[str, Any]:
    experiment = store.get_experiment(str(experiment_id).strip())
    if experiment is None:
        raise DomainQueryError("not_found", f"experiment '{experiment_id}' was not found")
    roots = [Path(root).resolve() for root in (allowed_roots if allowed_roots is not None else configured_observation_roots())]
    if not roots:
        raise DomainQueryError(
            "configuration_error",
            "SCHOLARFLOW_OBSERVE_ROOTS must name at least one approved directory",
        )
    bounded_tail_lines = max(1, min(int(tail_lines), 200))
    result = {
        "experiment_id": experiment.experiment_id,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "configured_roots": [str(root) for root in roots],
        "log": _inspect_path(experiment.log_path, roots, include_tail=include_tail, tail_lines=bounded_tail_lines),
        "checkpoint": _inspect_path(experiment.checkpoint_path, roots, include_tail=False, tail_lines=0),
    }
    event = append_event(
        store.root_dir,
        event_type="pi.experiment.observed",
        payload={
            "experiment_id": experiment.experiment_id,
            "pi_tool_call_id": request_id,
            "log_state": result["log"]["state"],
            "checkpoint_state": result["checkpoint"]["state"],
            "tail_included": bool(result["log"].get("tail")),
        },
    )
    result["audit_event_id"] = event["event_id"]
    return result


def _inspect_path(
    raw_path: str | None,
    roots: list[Path],
    *,
    include_tail: bool,
    tail_lines: int,
) -> dict[str, Any]:
    if not raw_path:
        return {"state": "not_configured"}
    path = Path(raw_path).expanduser().resolve()
    if not any(path == root or root in path.parents for root in roots):
        return {"state": "outside_allowed_roots", "path": str(path)}
    if not path.exists():
        return {"state": "missing", "path": str(path)}
    stat = path.stat()
    payload: dict[str, Any] = {
        "state": "available",
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "is_file": path.is_file(),
    }
    if include_tail and path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        payload["tail"] = "\n".join(text.splitlines()[-tail_lines:])[-MAX_TAIL_CHARS:]
        payload["tail_truncated"] = len(text) > len(payload["tail"])
    return payload
