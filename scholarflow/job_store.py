"""Persistent automation job storage for recurring ScholarFlow tasks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .persistence import atomic_write_json, file_lock


class AutomationJobStore:
    """JSON-backed storage for recurring automation jobs."""

    def __init__(self, root_dir: Path):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.root_dir / "automation_jobs.json"

    def list_jobs(self) -> list[dict[str, Any]]:
        payload = self._load()
        return [payload["items"][job_id] for job_id in sorted(payload["items"].keys())]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        payload = self._load()
        job = payload["items"].get(job_id)
        return dict(job) if isinstance(job, dict) else None

    def upsert_job(self, job: dict[str, Any]) -> dict[str, Any]:
        with file_lock(self.file_path):
            payload = self._load()
            payload["items"][job["job_id"]] = dict(job)
            self._save(payload)
        return dict(job)

    def replace_jobs(self, jobs: list[dict[str, Any]]) -> None:
        with file_lock(self.file_path):
            payload = self._load()
            payload["items"] = {job["job_id"]: dict(job) for job in jobs}
            self._save(payload)

    def set_job_status(self, job_id: str, *, status: str, reason: str | None = None) -> dict[str, Any] | None:
        with file_lock(self.file_path):
            payload = self._load()
            existing = payload["items"].get(job_id)
            if not isinstance(existing, dict):
                return None
            existing["status"] = str(status)
            if reason is not None:
                existing["throttle_reason"] = str(reason)
            else:
                existing.pop("throttle_reason", None)
            payload["items"][job_id] = existing
            self._save(payload)
            return dict(existing)

    def list_due_jobs(self, *, now_iso: str) -> list[dict[str, Any]]:
        jobs = self.list_jobs()
        due: list[dict[str, Any]] = []
        for job in jobs:
            if str(job.get("status") or "active") != "active":
                continue
            next_run_at = str(job.get("next_run_at") or "")
            if next_run_at and next_run_at <= now_iso:
                due.append(job)
        return due

    def _load(self) -> dict[str, Any]:
        if not self.file_path.exists():
            return {"version": 1, "items": {}}
        return json.loads(self.file_path.read_text(encoding="utf-8"))

    def _save(self, payload: dict[str, Any]) -> None:
        atomic_write_json(self.file_path, payload)
