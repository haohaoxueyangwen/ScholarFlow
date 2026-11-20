"""Bounded, persistent workbench translation tasks around the Pi runtime."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import secrets
from threading import Event, RLock, Thread
import time
from typing import Any, Callable, Mapping

from .domain_queries import DomainQueryError
from .persistence import atomic_write_json, file_lock


MAX_SOURCE_CHARS = 5_000
MAX_TRANSLATION_CHARS = 100_000
MAX_PENDING_TASKS = 3
TERMINAL_RETENTION = timedelta(days=30)
_TASK_ID = re.compile(r"^tr_[0-9a-f]{32}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReaderTranslationService:
    def __init__(self, data_dir: Path, runner: Callable[[str, Callable[[Any], bool]], dict[str, Any]]):
        self.root = Path(data_dir) / "_reader_translations"
        self.root.mkdir(parents=True, exist_ok=True)
        self.runner = runner
        self._guard = RLock()
        self._worker: Thread | None = None
        self._cancellations: dict[str, Event] = {}
        self._processes: dict[str, Any] = {}
        self._prune_terminal()
        self.recover_interrupted()

    def _prune_terminal(self) -> None:
        cutoff = datetime.now(timezone.utc) - TERMINAL_RETENTION
        for path in self.root.glob("tr_*.json"):
            record = self._read(path.stem)
            if record.get("status") not in {"completed", "failed", "cancelled", "interrupted"}:
                continue
            try:
                finished = datetime.fromisoformat(record.get("finished_at") or record.get("updated_at"))
            except (TypeError, ValueError):
                continue
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            if finished < cutoff:
                with file_lock(path):
                    path.unlink(missing_ok=True)

    def _path(self, task_id: str) -> Path:
        if not _TASK_ID.fullmatch(str(task_id or "")):
            raise DomainQueryError("not_found", "reader translation task was not found")
        return self.root / f"{task_id}.json"

    def _read(self, task_id: str) -> dict[str, Any]:
        path = self._path(task_id)
        with file_lock(path):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise DomainQueryError("not_found", "reader translation task was not found") from exc
            except (OSError, ValueError) as exc:
                raise DomainQueryError("reader_translation_record_invalid", "reader translation task record is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("task_id") != task_id or payload.get("schema_version") != 1:
            raise DomainQueryError("reader_translation_record_invalid", "reader translation task record is invalid")
        return payload

    def _write(self, record: dict[str, Any]) -> None:
        path = self._path(record["task_id"])
        with file_lock(path):
            atomic_write_json(path, record)

    @staticmethod
    def _public(record: Mapping[str, Any]) -> dict[str, Any]:
        return {key: record[key] for key in ("schema_version", "task_id", "status", "source_sha256", "translation", "error_code", "created_at", "updated_at", "finished_at") if key in record}

    def create(self, source: str, *, task_id: str | None = None) -> dict[str, Any]:
        text = str(source or "").strip()
        if not text:
            raise DomainQueryError("invalid_argument", "translation source is required")
        if len(text) > MAX_SOURCE_CHARS:
            raise DomainQueryError("invalid_argument", f"translation source must be <= {MAX_SOURCE_CHARS} characters")
        normalized_task_id = str(task_id or f"tr_{secrets.token_hex(16)}")
        path = self._path(normalized_task_id)
        digest = sha256(text.encode("utf-8")).hexdigest()
        with self._guard, file_lock(self.root / "queue"):
            try:
                existing = self._read(normalized_task_id)
            except DomainQueryError as exc:
                if exc.code != "not_found":
                    raise
                existing = None
            if existing is not None:
                if existing.get("source_sha256") != digest:
                    raise DomainQueryError("reader_translation_id_conflict", "translation task ID was already used for different text")
                return self._public(existing)
            records = [self._read(path.stem) for path in self.root.glob("tr_*.json")]
            occupied = [item for item in records if item.get("status") in {"queued", "running", "cancelling"}]
            if len(occupied) >= MAX_PENDING_TASKS:
                raise DomainQueryError("reader_translation_queue_full", "translation queue is full; wait for a running task")
            record = {"schema_version": 1, "task_id": normalized_task_id, "status": "queued", "source": text,
                      "source_sha256": digest, "translation": "",
                      "error_code": "", "created_at": _now(), "updated_at": _now(), "finished_at": ""}
            self._write(record)
            self._kick_locked()
        return self._public(record)

    def get(self, task_id: str) -> dict[str, Any]:
        return self._public(self._read(task_id))

    def list(self, *, limit: int = 20) -> dict[str, Any]:
        records = [self._read(path.stem) for path in self.root.glob("tr_*.json")]
        records.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        items = [self._public(record) for record in records[:max(1, min(int(limit), 100))]]
        return {"count": len(items), "items": items, "read_only": True}

    def cancel(self, task_id: str) -> dict[str, Any]:
        task_id = str(task_id or "")
        process = None
        with self._guard:
            record = self._read(task_id)
            if record["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return self._public(record)
            cancellation = self._cancellations.get(task_id)
            if record["status"] == "queued":
                record.update(status="cancelled", source="", finished_at=_now(), updated_at=_now())
                self._write(record)
                return self._public(record)
            record.update(status="cancelling", updated_at=_now())
            self._write(record)
            if cancellation:
                cancellation.set()
            process = self._processes.get(task_id)
        if process is not None:
            Thread(target=process.close, name="scholarflow-translation-cancel", daemon=True).start()
        return self._public(record)

    def recover_interrupted(self) -> None:
        for path in self.root.glob("tr_*.json"):
            record = self._read(path.stem)
            if record.get("status") in {"queued", "running", "cancelling"}:
                record.update(status="interrupted", source="", error_code="workbench_restarted", finished_at=_now(), updated_at=_now())
                self._write(record)

    def close(self) -> None:
        for path in self.root.glob("tr_*.json"):
            record = self._read(path.stem)
            if record.get("status") in {"queued", "running", "cancelling"}:
                self.cancel(record["task_id"])

    def _kick_locked(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = Thread(target=self._drain, name="scholarflow-reader-translation", daemon=True)
        self._worker.start()

    def _drain(self) -> None:
        while True:
            with self._guard:
                queued = []
                for path in self.root.glob("tr_*.json"):
                    try:
                        candidate = self._read(path.stem)
                    except DomainQueryError:
                        continue
                    if candidate.get("status") == "queued":
                        queued.append(candidate)
                if not queued:
                    self._worker = None
                    return
                record = min(queued, key=lambda item: item["created_at"])
                task_id = record["task_id"]
                cancellation = Event()
                self._cancellations[task_id] = cancellation
                source = record["source"]
                record.update(status="running", updated_at=_now())
                self._write(record)

            def register_process(process: Any) -> bool:
                with self._guard:
                    self._processes[task_id] = process
                    cancelled = cancellation.is_set()
                if cancelled:
                    Thread(target=process.close, name="scholarflow-translation-cancel", daemon=True).start()
                return not cancelled

            try:
                if cancellation.is_set():
                    raise DomainQueryError("translation_cancelled", "translation was cancelled")
                result = self.runner(source, register_process)
                if cancellation.is_set():
                    raise DomainQueryError("translation_cancelled", "translation was cancelled")
                translation = str(result.get("translation") or "").strip()
                if not translation or len(translation) > MAX_TRANSLATION_CHARS:
                    raise DomainQueryError("translation_empty", "translation did not return a bounded text result")
                record.update(status="completed", translation=translation, error_code="")
            except Exception as exc:
                code = str(getattr(exc, "code", "translation_unavailable"))[:100]
                record.update(status="cancelled" if cancellation.is_set() or code == "translation_cancelled" else "failed", error_code=code)
            finally:
                record.update(source="", finished_at=_now(), updated_at=_now())
                self._write(record)
                with self._guard:
                    self._processes.pop(task_id, None)
                    self._cancellations.pop(task_id, None)
