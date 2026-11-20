"""Durable, local workflow records; not the AI index or research truth."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .domain_queries import DomainQueryError
from .persistence import atomic_write_json, file_lock


class SyncJobStore:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "_knowledge_sync"

    def path(self, plan_id: str) -> Path:
        if not re.fullmatch(r"ksp_[A-Za-z0-9_-]{24}", plan_id):
            raise DomainQueryError("knowledge_sync_plan_not_found", "knowledge synchronization plan was not found")
        return self.root / f"{plan_id}.json"

    def get(self, plan_id: str) -> dict[str, Any]:
        try:
            payload = json.loads(self.path(plan_id).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DomainQueryError("knowledge_sync_plan_not_found", "knowledge synchronization plan was not found") from exc
        except (OSError, ValueError) as exc:
            raise DomainQueryError("knowledge_sync_store_invalid", "knowledge synchronization record is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("plan_id") != plan_id:
            raise DomainQueryError("knowledge_sync_store_invalid", "knowledge synchronization record is invalid")
        return payload

    def save(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(record["plan_id"])
        with file_lock(path):
            atomic_write_json(path, record)

    def records(self) -> list[dict[str, Any]]:
        return sorted((self.get(path.stem) for path in self.root.glob("ksp_*.json")), key=lambda item: item["created_at"], reverse=True) if self.root.exists() else []

    @staticmethod
    def public(record: dict[str, Any]) -> dict[str, Any]:
        fields = ("plan_id", "source", "collection_key", "import_id", "fingerprint", "plan", "discovery", "warnings", "created_at", "expires_at", "status", "attempts", "error_code", "error_type", "results", "receipt", "confirmed", "allow_deletes")
        return {**{key: record.get(key) for key in fields}, "read_only": True}
