"""Runtime supervisor primitives for long-running ScholarFlow deployments."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from .event_bus import append_event, event_log_path, list_events
from .consistency import audit_domain_consistency
from .gateway import gateway_inbox_path, gateway_state_path, load_gateway_state, poll_gateway_inbox_once
from .job_store import AutomationJobStore
from .persistence import atomic_write_json
from .pi_rpc_host import pi_rpc_status
from .quality import detect_stale_experiments, workflow_health_summary
from .scheduler import run_due_jobs
from .storage import ScholarFlowStore


class PiHostStatus(TypedDict):
    running: bool
    last_heartbeat: str | None
    pending_requests: int
    supervisor_status: str
    gateway_status: str
    rpc_status: str
    rpc_running: bool
    rpc: dict[str, Any]


def supervisor_state_path(store_root: Path) -> Path:
    return Path(store_root) / "runtime_supervisor_state.json"


def load_supervisor_state(store_root: Path) -> dict[str, Any]:
    path = supervisor_state_path(store_root)
    if not path.exists():
        return _default_supervisor_state()
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key, value in _default_supervisor_state().items():
        payload.setdefault(key, value)
    return payload


def save_supervisor_state(store_root: Path, payload: dict[str, Any]) -> None:
    path = supervisor_state_path(store_root)
    atomic_write_json(path, payload)


def start_runtime_supervisor(
    store_root: Path,
    *,
    poll_interval_seconds: int = 600,
    max_consecutive_failures: int = 3,
    base_backoff_seconds: int = 30,
    max_backoff_seconds: int = 300,
    now: datetime | None = None,
) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    state = load_supervisor_state(store_root)
    state["status"] = "running"
    state["poll_interval_seconds"] = int(poll_interval_seconds)
    state["max_consecutive_failures"] = max(1, int(max_consecutive_failures))
    state["base_backoff_seconds"] = max(1, int(base_backoff_seconds))
    state["max_backoff_seconds"] = max(int(base_backoff_seconds), int(max_backoff_seconds))
    state["consecutive_failures"] = 0
    state["backoff_seconds"] = 0
    state["circuit_state"] = "closed"
    state["circuit_open_until"] = None
    state["last_error"] = None
    state["last_started_at"] = effective_now.isoformat()
    state["last_heartbeat_at"] = effective_now.isoformat()
    save_supervisor_state(store_root, state)
    append_event(
        store_root,
        event_type="runtime.supervisor.started",
        payload={
            "poll_interval_seconds": int(poll_interval_seconds),
            "max_consecutive_failures": state["max_consecutive_failures"],
            "base_backoff_seconds": state["base_backoff_seconds"],
            "max_backoff_seconds": state["max_backoff_seconds"],
        },
        now=effective_now,
    )
    return dict(state)


def stop_runtime_supervisor(store_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    state = load_supervisor_state(store_root)
    state["status"] = "stopped"
    state["last_stopped_at"] = effective_now.isoformat()
    save_supervisor_state(store_root, state)
    append_event(
        store_root,
        event_type="runtime.supervisor.stopped",
        payload={},
        now=effective_now,
    )
    return dict(state)


def runtime_supervisor_status(store_root: Path) -> dict[str, Any]:
    return load_supervisor_state(store_root)


def get_pi_host_status(store_root: Path) -> PiHostStatus:
    state = load_supervisor_state(store_root)
    gateway = load_gateway_state(store_root)
    inbox_path = gateway_inbox_path(store_root)
    inbox = json.loads(inbox_path.read_text(encoding="utf-8")) if inbox_path.exists() else {"items": []}
    pending_requests = sum(1 for item in inbox.get("items", []) if item.get("status") == "pending")
    supervisor_status = str(state.get("status") or "stopped")
    gateway_status = str(gateway.get("status") or "stopped")
    rpc = pi_rpc_status(store_root)
    return {
        "running": supervisor_status == "running" and gateway_status == "running",
        "last_heartbeat": state.get("last_heartbeat_at"),
        "pending_requests": pending_requests,
        "supervisor_status": supervisor_status,
        "gateway_status": gateway_status,
        "rpc_status": rpc["status"],
        "rpc_running": rpc["status"] == "running",
        "rpc": rpc,
    }


def runtime_health_summary(store_root: Path, *, recent_limit: int = 50) -> dict[str, Any]:
    state = load_supervisor_state(store_root)
    gateway = load_gateway_state(store_root)
    pi_host = get_pi_host_status(store_root)
    events = list_events(store_root, limit=max(1, int(recent_limit)))
    items = list(events.get("items") or [])
    tick_events = [item for item in items if item.get("event_type") == "runtime.supervisor.tick"]
    failed_ticks = [item for item in tick_events if str((item.get("payload") or {}).get("status") or "") == "failed"]
    succeeded_ticks = [item for item in tick_events if str((item.get("payload") or {}).get("status") or "") == "succeeded"]
    active_jobs = [
        job
        for job in AutomationJobStore(store_root).list_jobs()
        if str(job.get("status") or "active") == "active"
    ]
    stale_experiments = detect_stale_experiments(ScholarFlowStore(store_root))
    workflows = workflow_health_summary(store_root)
    health_status = "healthy"
    if (
        state.get("circuit_state") == "open"
        or workflows["status"] == "degraded"
        or (pi_host["rpc"].get("configured") and pi_host["rpc_status"] in {"stale", "circuit_open"})
    ):
        health_status = "degraded"
    elif failed_ticks or stale_experiments["status"] == "warn":
        health_status = "warning"
    return {
        "status": health_status,
        "runtime_supervisor": {
            "status": state.get("status"),
            "last_tick_at": state.get("last_tick_at"),
            "last_tick_status": state.get("last_tick_status"),
            "last_heartbeat_at": state.get("last_heartbeat_at"),
            "consecutive_failures": state.get("consecutive_failures", 0),
            "circuit_state": state.get("circuit_state"),
            "backoff_seconds": state.get("backoff_seconds", 0),
            "last_error": state.get("last_error"),
        },
        "gateway": {
            "status": gateway.get("status"),
            "last_polled_at": gateway.get("last_polled_at"),
            "last_received_at": gateway.get("last_received_at"),
            "processed_count": gateway.get("processed_count", 0),
            "blocked_count": gateway.get("blocked_count", 0),
        },
        "pi_host": pi_host,
        "pi_rpc": pi_host["rpc"],
        "automation": {
            "job_count": len(AutomationJobStore(store_root).list_jobs()),
            "active_job_count": len(active_jobs),
        },
        "events": {
            "recent_event_count": events.get("count", 0),
            "recent_tick_count": len(tick_events),
            "recent_tick_success_count": len(succeeded_ticks),
            "recent_tick_failure_count": len(failed_ticks),
        },
        "stale_experiments": stale_experiments,
        "workflows": workflows,
    }


def runtime_doctor(store_root: Path, *, vault_root: Path) -> dict[str, Any]:
    store_root = Path(store_root)
    vault_root = Path(vault_root)
    checks = [
        _check_path("store_root_exists", store_root, should_exist=True),
        _check_writable("store_root_writable", store_root),
        _check_path("vault_root_exists", vault_root, should_exist=True),
        _check_writable("vault_root_writable", vault_root),
        _check_path("event_log_parent_exists", event_log_path(store_root).parent, should_exist=True),
        _check_writable("event_log_parent_writable", event_log_path(store_root).parent),
        _check_path("job_store_parent_exists", (store_root / "automation_jobs.json").parent, should_exist=True),
        _check_writable("job_store_parent_writable", (store_root / "automation_jobs.json").parent),
        _check_path("runtime_state_parent_exists", supervisor_state_path(store_root).parent, should_exist=True),
        _check_writable("runtime_state_parent_writable", supervisor_state_path(store_root).parent),
        _check_path("gateway_state_parent_exists", gateway_state_path(store_root).parent, should_exist=True),
        _check_writable("gateway_state_parent_writable", gateway_state_path(store_root).parent),
    ]
    health = runtime_health_summary(store_root)
    consistency = audit_domain_consistency(ScholarFlowStore(store_root))
    checks.append(
        {
            "check_id": "domain_consistency",
            "status": "pass" if consistency["status"] == "ok" else "warn",
            "detail": f"Cross-store consistency found {consistency['issue_count']} issue(s).",
        }
    )
    if health["runtime_supervisor"]["status"] == "stopped":
        checks.append(
            {
                "check_id": "runtime_supervisor_running",
                "status": "warn",
                "detail": "Runtime supervisor is currently stopped.",
            }
        )
    if health["automation"]["job_count"] == 0:
        checks.append(
            {
                "check_id": "automation_jobs_present",
                "status": "warn",
                "detail": "No automation jobs are scheduled yet.",
            }
        )
    if health["gateway"]["status"] == "stopped":
        checks.append(
            {
                "check_id": "gateway_running",
                "status": "warn",
                "detail": "Gateway is currently stopped.",
            }
        )
    if health["pi_host"]["rpc"].get("configured") and health["pi_host"]["rpc_status"] in {"stale", "circuit_open"}:
        checks.append(
            {
                "check_id": "pi_rpc_supervisor",
                "status": "warn",
                "detail": f"Supervised Pi RPC host is {health['pi_host']['rpc_status']}.",
            }
        )
    if health["stale_experiments"]["stale_count"]:
        checks.append(
            {
                "check_id": "stale_experiments",
                "status": "warn",
                "detail": f"Found {health['stale_experiments']['stale_count']} stale experiment(s).",
            }
        )
    if health["workflows"]["status"] == "degraded":
        checks.append(
            {
                "check_id": "workflow_health",
                "status": "warn",
                "detail": "Scheduled Pi workflows have overdue, failed, or throttled work.",
            }
        )
    overall_status = "ok"
    if any(item["status"] == "fail" for item in checks):
        overall_status = "error"
    elif any(item["status"] == "warn" for item in checks):
        overall_status = "warn"
    return {
        "status": overall_status,
        "check_count": len(checks),
        "checks": checks,
        "health": health,
        "consistency": consistency,
    }


def runtime_trend_report(
    store_root: Path,
    *,
    recent_limit: int = 200,
    failure_top_k: int = 5,
) -> dict[str, Any]:
    store_root = Path(store_root)
    health = runtime_health_summary(store_root, recent_limit=recent_limit)
    window = max(1, int(recent_limit))
    all_events = list_events(store_root, limit=window).get("items") or []
    tick_events = [item for item in all_events if item.get("event_type") == "runtime.supervisor.tick"]
    tick_total = len(tick_events)
    tick_success = sum(
        1
        for item in tick_events
        if str((item.get("payload") or {}).get("status") or "") == "succeeded"
    )
    tick_failure = sum(
        1
        for item in tick_events
        if str((item.get("payload") or {}).get("status") or "") == "failed"
    )
    tick_success_rate = (tick_success / tick_total) if tick_total else None
    job_success = sum(1 for item in all_events if item.get("event_type") == "automation.job.succeeded")
    job_failure = sum(1 for item in all_events if item.get("event_type") == "automation.job.failed")
    gateway_processed = sum(1 for item in all_events if item.get("event_type") == "gateway.receive.processed")
    gateway_blocked = sum(1 for item in all_events if item.get("event_type") == "gateway.receive.blocked")
    gateway_polled = sum(1 for item in all_events if item.get("event_type") == "gateway.poll")
    failure_reasons: dict[str, int] = {}
    touched_job_types: dict[str, int] = {}
    for item in all_events:
        if item.get("event_type") == "automation.job.failed":
            reason = str((item.get("payload") or {}).get("error") or "unknown")
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
        if item.get("event_type") in {"automation.job.succeeded", "automation.job.failed"}:
            job_type = str((item.get("payload") or {}).get("job_type") or "unknown")
            touched_job_types[job_type] = touched_job_types.get(job_type, 0) + 1
    ranked_failures = sorted(
        ({"reason": reason, "count": count} for reason, count in failure_reasons.items()),
        key=lambda entry: entry["count"],
        reverse=True,
    )[: max(1, int(failure_top_k))]
    return {
        "status": health.get("status"),
        "window_size": window,
        "tick": {
            "total": tick_total,
            "success": tick_success,
            "failure": tick_failure,
            "success_rate": tick_success_rate,
        },
        "automation": {
            "job_succeeded": job_success,
            "job_failed": job_failure,
            "touched_job_types": touched_job_types,
        },
        "gateway": {
            "processed_events": gateway_processed,
            "blocked_events": gateway_blocked,
            "polled_events": gateway_polled,
        },
        "failure_reasons": ranked_failures,
        "health": health,
    }


def pause_runtime_supervisor(store_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    state = load_supervisor_state(store_root)
    state["status"] = "paused"
    state["last_paused_at"] = effective_now.isoformat()
    save_supervisor_state(store_root, state)
    append_event(
        store_root,
        event_type="runtime.supervisor.paused",
        payload={},
        now=effective_now,
    )
    return dict(state)


def resume_runtime_supervisor(store_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    state = load_supervisor_state(store_root)
    state["status"] = "running"
    state["last_resumed_at"] = effective_now.isoformat()
    state["last_heartbeat_at"] = effective_now.isoformat()
    save_supervisor_state(store_root, state)
    append_event(
        store_root,
        event_type="runtime.supervisor.resumed",
        payload={},
        now=effective_now,
    )
    return dict(state)


def runtime_supervisor_heartbeat(store_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    state = load_supervisor_state(store_root)
    state["last_heartbeat_at"] = effective_now.isoformat()
    save_supervisor_state(store_root, state)
    append_event(
        store_root,
        event_type="runtime.supervisor.heartbeat",
        payload={"status": state.get("status")},
        now=effective_now,
    )
    return dict(state)


def run_runtime_supervisor_once(
    store: ScholarFlowStore,
    *,
    vault_root: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    state = load_supervisor_state(store.root_dir)
    if state.get("status") != "running":
        status = "supervisor_paused" if state.get("status") == "paused" else "supervisor_stopped"
        return {
            "status": status,
            "reason": "runtime supervisor is not running",
            "runtime_supervisor": state,
        }
    circuit_open_until = _parse_timestamp(state.get("circuit_open_until"))
    if state.get("circuit_state") == "open" and circuit_open_until and effective_now < circuit_open_until:
        remaining_seconds = max(0.0, (circuit_open_until - effective_now).total_seconds())
        append_event(
            store.root_dir,
            event_type="runtime.supervisor.backing_off",
            payload={"remaining_seconds": remaining_seconds},
            now=effective_now,
        )
        return {
            "status": "circuit_open",
            "reason": "runtime supervisor circuit is open",
            "remaining_backoff_seconds": remaining_seconds,
            "runtime_supervisor": state,
        }
    runtime_supervisor_heartbeat(store.root_dir, now=effective_now)
    tick_status = "succeeded"
    automation_error: str | None = None
    gateway_error: str | None = None
    auto_adjustments: dict[str, Any] = {}
    try:
        auto_adjustments = _apply_observation_driven_adjustments(store.root_dir, effective_now)
    except Exception as exc:
        auto_adjustments = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        automation = run_due_jobs(store, vault_root=vault_root, now=effective_now)
    except Exception as exc:
        automation_error = f"{type(exc).__name__}: {exc}"
        automation = {
            "due_count": 0,
            "executed_count": 0,
            "items": [],
            "error": automation_error,
        }
    try:
        gateway = poll_gateway_inbox_once(store.root_dir, now=effective_now)
    except Exception as exc:
        gateway_error = f"{type(exc).__name__}: {exc}"
        gateway = {
            "status": "gateway_poll_failed",
            "error": gateway_error,
        }
    if automation_error or gateway_error:
        tick_status = "failed"
    state = load_supervisor_state(store.root_dir)
    state["last_tick_at"] = effective_now.isoformat()
    state["last_tick_status"] = tick_status
    if tick_status == "failed":
        _apply_failure_backoff(state, effective_now, automation_error or gateway_error or "runtime tick failed")
    else:
        _reset_failure_backoff(state)
    save_supervisor_state(store.root_dir, state)
    append_event(
        store.root_dir,
        event_type="runtime.supervisor.tick",
        payload={
            "status": tick_status,
            "due_count": automation.get("due_count", 0),
            "executed_count": automation.get("executed_count", 0),
            "consecutive_failures": state.get("consecutive_failures", 0),
            "circuit_state": state.get("circuit_state"),
            "backoff_seconds": state.get("backoff_seconds", 0),
            "error": automation.get("error"),
            "gateway_status": gateway.get("status"),
            "gateway_error": gateway_error,
            "auto_adjustments": auto_adjustments,
        },
        now=effective_now,
    )
    return {
        "status": "ticked",
        "tick_status": tick_status,
        "runtime_supervisor": state,
        "automation": automation,
        "gateway": gateway,
        "auto_adjustments": auto_adjustments,
    }


def _apply_observation_driven_adjustments(
    store_root: Path,
    now: datetime,
) -> dict[str, Any]:
    """Apply lightweight, observation-driven adjustments based on recent events.

    The intent is to close the loop between `runtime_trend_report` and runtime
    behavior. We avoid large policy changes and only react to signals that are
    already reliable: repeated job failures and stale activity.
    """
    store_root = Path(store_root)
    report = runtime_trend_report(store_root, recent_limit=200, failure_top_k=5)
    job_failures = report.get("automation", {}).get("job_failed", 0)
    top_reason = (report.get("failure_reasons") or [{}])[0].get("reason")
    last_tick = (report.get("health", {}).get("runtime_supervisor", {}) or {}).get("last_tick_at")
    adjustments: dict[str, Any] = {
        "throttled_jobs": [],
        "nudges": [],
    }
    if job_failures >= 2 and top_reason:
        try:
            from .job_store import AutomationJobStore

            store = AutomationJobStore(store_root)
            for job in store.list_jobs():
                updated = store.set_job_status(
                    job["job_id"],
                    status="throttled",
                    reason=f"runtime trend: {top_reason} (failures={job_failures})",
                )
                if updated is not None:
                    adjustments["throttled_jobs"].append(
                        {
                            "job_id": updated["job_id"],
                            "job_type": updated.get("job_type"),
                            "reason": updated.get("throttle_reason"),
                        }
                    )
        except Exception as exc:  # pragma: no cover - defensive
            adjustments["throttle_error"] = f"{type(exc).__name__}: {exc}"
    if last_tick:
        adjustments["nudges"].append(
            {
                "type": "last_tick_observed",
                "last_tick_at": last_tick,
            }
        )
    append_event(
        store_root,
        event_type="runtime.supervisor.auto_adjustment",
        payload=adjustments,
        now=now,
    )
    return adjustments


def run_runtime_supervisor_loop(
    store: ScholarFlowStore,
    *,
    vault_root: Path,
    max_ticks: int | None = None,
    sleep_seconds: float | None = None,
    sleep_fn: Any | None = None,
) -> dict[str, Any]:
    effective_sleep = sleep_fn or time.sleep
    items: list[dict[str, Any]] = []
    iterations = 0
    loop_status = "max_ticks_reached" if max_ticks is not None else "stopped"
    while True:
        state = load_supervisor_state(store.root_dir)
        if state.get("status") == "stopped":
            loop_status = "stopped"
            break
        result = run_runtime_supervisor_once(store, vault_root=vault_root)
        items.append(
            {
                "iteration": iterations + 1,
                "status": result.get("status"),
                "tick_status": result.get("tick_status"),
                "remaining_backoff_seconds": result.get("remaining_backoff_seconds"),
            }
        )
        iterations += 1
        if max_ticks is not None and iterations >= max_ticks:
            break
        delay_seconds = _resolve_loop_delay(
            load_supervisor_state(store.root_dir),
            sleep_seconds=sleep_seconds,
        )
        if delay_seconds > 0:
            effective_sleep(delay_seconds)
    return {
        "status": "loop_finished",
        "loop_status": loop_status,
        "iterations": iterations,
        "items": items,
        "runtime_supervisor": load_supervisor_state(store.root_dir),
    }


def _default_supervisor_state() -> dict[str, Any]:
    return {
        "status": "stopped",
        "poll_interval_seconds": 600,
        "max_consecutive_failures": 3,
        "base_backoff_seconds": 30,
        "max_backoff_seconds": 300,
        "consecutive_failures": 0,
        "backoff_seconds": 0,
        "circuit_state": "closed",
        "circuit_open_until": None,
        "last_error": None,
        "last_started_at": None,
        "last_stopped_at": None,
        "last_paused_at": None,
        "last_resumed_at": None,
        "last_heartbeat_at": None,
        "last_tick_at": None,
        "last_tick_status": None,
    }


def _check_path(check_id: str, path: Path, *, should_exist: bool) -> dict[str, Any]:
    exists = Path(path).exists()
    if should_exist and not exists:
        return {
            "check_id": check_id,
            "status": "fail",
            "detail": f"Path does not exist: {path}",
        }
    return {
        "check_id": check_id,
        "status": "pass",
        "detail": f"Path is available: {path}",
    }


def _check_writable(check_id: str, path: Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {
            "check_id": check_id,
            "status": "fail",
            "detail": f"Path does not exist: {target}",
        }
    is_writable = os.access(target, os.W_OK)
    return {
        "check_id": check_id,
        "status": "pass" if is_writable else "fail",
        "detail": f"{'Writable' if is_writable else 'Not writable'}: {target}",
    }


def _apply_failure_backoff(state: dict[str, Any], now: datetime, error: str) -> None:
    consecutive_failures = int(state.get("consecutive_failures", 0)) + 1
    state["consecutive_failures"] = consecutive_failures
    state["last_error"] = error
    base_backoff = max(1, int(state.get("base_backoff_seconds", 30)))
    max_backoff = max(base_backoff, int(state.get("max_backoff_seconds", 300)))
    backoff_seconds = min(max_backoff, base_backoff * (2 ** max(0, consecutive_failures - 1)))
    state["backoff_seconds"] = backoff_seconds
    max_failures = max(1, int(state.get("max_consecutive_failures", 3)))
    if consecutive_failures >= max_failures:
        state["circuit_state"] = "open"
        state["circuit_open_until"] = now.timestamp() + float(backoff_seconds)
    else:
        state["circuit_state"] = "closed"
        state["circuit_open_until"] = None


def _reset_failure_backoff(state: dict[str, Any]) -> None:
    state["consecutive_failures"] = 0
    state["backoff_seconds"] = 0
    state["circuit_state"] = "closed"
    state["circuit_open_until"] = None
    state["last_error"] = None


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return None


def _resolve_loop_delay(state: dict[str, Any], *, sleep_seconds: float | None) -> float:
    if sleep_seconds is not None:
        return max(0.0, float(sleep_seconds))
    circuit_open_until = _parse_timestamp(state.get("circuit_open_until"))
    if state.get("circuit_state") == "open" and circuit_open_until is not None:
        remaining = (circuit_open_until - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, remaining)
    if state.get("status") == "paused":
        return 1.0
    return max(0.0, float(state.get("poll_interval_seconds", 600)))


def _coerce_now(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)
