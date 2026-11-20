"""Lightweight recurring automation scheduler for ScholarFlow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .event_bus import append_event
from .job_store import AutomationJobStore
from .storage import ScholarFlowStore
from .vault_automation import append_daily_digest, maintain_vault_views, run_kb_backfill

DEFAULT_JOBS = (
    {
        "job_id": "daily_digest",
        "job_type": "daily_digest",
        "interval_minutes": 720,
        "payload": {},
    },
    {
        "job_id": "vault_maintenance",
        "job_type": "vault_maintenance",
        "interval_minutes": 720,
        "payload": {},
    },
    {
        "job_id": "kb_backfill",
        "job_type": "kb_backfill",
        "interval_minutes": 1440,
        "payload": {"include_tasks": False, "overwrite_existing": False},
    },
    {
        "job_id": "pi_daily_review",
        "job_type": "pi_workflow",
        "interval_minutes": 1440,
        "payload": {
            "workflow_type": "daily_review",
            "instruction": "Use the scholarflow-daily-review Skill to review current research evidence and propose a daily_report artifact.",
        },
    },
    {
        "job_id": "pi_weekly_review",
        "job_type": "pi_workflow",
        "interval_minutes": 10080,
        "payload": {
            "workflow_type": "weekly_review",
            "instruction": "Use the scholarflow-weekly-review Skill to review projects, tasks, papers, experiments, and artifacts.",
        },
    },
    {
        "job_id": "pi_experiment_watch",
        "job_type": "pi_workflow",
        "interval_minutes": 60,
        "payload": {
            "workflow_type": "experiment_watch",
            "instruction": "Find running experiments, observe approved evidence files, and propose evidence-backed log updates when needed.",
        },
    },
)


def schedule_default_jobs(store_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    job_store = AutomationJobStore(store_root)
    created_or_updated: list[dict[str, Any]] = []
    for spec in DEFAULT_JOBS:
        existing = job_store.get_job(spec["job_id"]) or {}
        job = {
            "job_id": spec["job_id"],
            "job_type": spec["job_type"],
            "schedule_mode": "interval",
            "interval_minutes": spec["interval_minutes"],
            "next_run_at": existing.get("next_run_at") or effective_now.isoformat(),
            "last_run_at": existing.get("last_run_at"),
            "last_status": existing.get("last_status", "scheduled"),
            "status": existing.get("status", "active"),
            "payload": dict(existing.get("payload") or spec.get("payload") or {}),
        }
        job_store.upsert_job(job)
        created_or_updated.append(job)
    append_event(
        store_root,
        event_type="automation.schedule_defaults",
        payload={
            "count": len(created_or_updated),
            "job_ids": [item["job_id"] for item in created_or_updated],
        },
        now=effective_now,
    )
    return {
        "count": len(created_or_updated),
        "items": created_or_updated,
    }


def list_automation_jobs(store_root: Path) -> dict[str, Any]:
    job_store = AutomationJobStore(store_root)
    jobs = job_store.list_jobs()
    return {
        "count": len(jobs),
        "items": jobs,
    }


def run_due_jobs(
    store: ScholarFlowStore,
    *,
    vault_root: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    effective_now = _coerce_now(now)
    now_iso = effective_now.isoformat()
    job_store = AutomationJobStore(store.root_dir)
    jobs = job_store.list_jobs()
    due = job_store.list_due_jobs(now_iso=now_iso)
    executed: list[dict[str, Any]] = []

    by_id = {job["job_id"]: dict(job) for job in jobs}
    for job in due:
        updated_job = dict(job)
        try:
            result = _execute_job(
                store,
                vault_root=vault_root,
                job=job,
                now=effective_now,
            )
            updated_job["last_status"] = "succeeded"
            updated_job["last_result"] = result
            append_event(
                store.root_dir,
                event_type="automation.job.succeeded",
                payload={
                    "job_id": updated_job["job_id"],
                    "job_type": updated_job["job_type"],
                    "result_summary": _summarize_job_result(result),
                },
                now=effective_now,
            )
        except Exception as exc:
            result = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            updated_job["last_status"] = "failed"
            updated_job["last_result"] = result
            append_event(
                store.root_dir,
                event_type="automation.job.failed",
                payload={
                    "job_id": updated_job["job_id"],
                    "job_type": updated_job["job_type"],
                    "error": result["error"],
                },
                now=effective_now,
            )
        updated_job["last_run_at"] = now_iso
        updated_job["next_run_at"] = (effective_now + timedelta(minutes=int(job["interval_minutes"]))).isoformat()
        by_id[updated_job["job_id"]] = updated_job
        executed.append(
            {
                "job_id": updated_job["job_id"],
                "job_type": updated_job["job_type"],
                "status": updated_job["last_status"],
                "result": result,
            }
        )

    job_store.replace_jobs(list(by_id.values()))
    return {
        "due_count": len(due),
        "executed_count": len(executed),
        "items": executed,
    }


def _execute_job(
    store: ScholarFlowStore,
    *,
    vault_root: Path,
    job: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    job_type = str(job.get("job_type") or "")
    payload = dict(job.get("payload") or {})
    if job_type == "vault_maintenance":
        return maintain_vault_views(store, vault_root=vault_root, now=now)
    if job_type == "daily_digest":
        return append_daily_digest(store, vault_root=vault_root, now=now)
    if job_type == "kb_backfill":
        return run_kb_backfill(
            store,
            vault_root=vault_root,
            include_tasks=bool(payload.get("include_tasks")),
            overwrite_existing=bool(payload.get("overwrite_existing")),
            source_tag="automation",
        )
    if job_type == "pi_workflow":
        from .gateway import enqueue_workflow_request

        item = enqueue_workflow_request(
            store.root_dir,
            skill_name="scholarflow-automation-inbox",
            payload=payload,
            now=now,
        )
        return {
            "status": "queued_for_pi",
            "message_id": item["message_id"],
            "workflow_type": item["workflow_type"],
            "skill_name": item.get("skill_name"),
        }
    raise ValueError(f"unsupported automation job_type: {job_type}")


def _coerce_now(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _summarize_job_result(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"status": "unknown"}
    return {
        "status": result.get("status") or "ok",
        "count": result.get("count"),
        "written_count": result.get("written_count"),
        "executed_count": result.get("executed_count"),
    }
