"""Short-lived, governed synchronization plans for the AI knowledge index."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import secrets
from threading import RLock, Thread
import time
from typing import Any, Callable

from .domain_queries import DomainQueryError
from .knowledge_engine import LightRAGClient, build_knowledge_engine
from .knowledge_ingestion import (
    IngestionCandidate,
    IngestionPlan,
    KnowledgeIngestionManifest,
    apply_ingestion_plan,
    build_ingestion_plan,
    discover_file_sources,
    discover_zotero_sources,
)
from .reader_workspace import ReaderWorkspaceStore
from .knowledge_jobs import SyncJobStore
from .persistence import file_lock, PersistenceLockTimeout
from .zotero_client import build_zotero_client, describe_zotero_client


class KnowledgeSyncService:
    """Discover sources, retain an exact plan, and reject stale application."""

    def __init__(
        self,
        data_dir: Path,
        *,
        vault_root: Path | None = None,
        plan_ttl_seconds: float = 1800.0,
        zotero_factory: Callable[[], Any] = build_zotero_client,
        reader_store: ReaderWorkspaceStore | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.vault_root = Path(vault_root).resolve() if vault_root else None
        self.plan_ttl_seconds = max(60.0, float(plan_ttl_seconds))
        self.zotero_factory = zotero_factory
        self.reader_store = reader_store or ReaderWorkspaceStore(self.data_dir)
        self.jobs = SyncJobStore(self.data_dir)
        self._worker: Thread | None = None
        self._lock = RLock()

    def options(self) -> dict[str, Any]:
        zotero_client = self.zotero_factory()
        zotero = describe_zotero_client(zotero_client)
        zotero_backend = str(zotero.get("backend") or "null")
        zotero_probe = (
            zotero_client.list_collections()
            if zotero_backend != "null"
            else None
        )
        zotero_available = bool(
            zotero_probe is not None and zotero_probe.status == "ok"
        )
        items = [
            {
                "source": "obsidian",
                "label": "已配置的 Obsidian vault",
                "available": bool(self.vault_root and self.vault_root.is_dir()),
                "detail": self.vault_root.name if self.vault_root else "未配置",
            },
            {
                "source": "zotero",
                "label": "当前 Zotero 文献库",
                "available": zotero_available,
                "detail": (
                    zotero_backend
                    if zotero_available
                    else f"{zotero_backend} · unreachable"
                ),
            },
            {
                "source": "local_pdf",
                "label": "ScholarFlow 暂存 PDF",
                "available": True,
                "detail": "按单份 PDF 生成受控同步计划",
            },
        ]
        return {"items": items, "read_only": True}

    def create_plan(
        self,
        *,
        source: str,
        collection_key: str | None = None,
        import_id: str | None = None,
        detect_missing: bool = True,
    ) -> dict[str, Any]:
        normalized_source = str(source or "").strip().casefold()
        normalized_collection = str(collection_key or "").strip() or None
        normalized_import = str(import_id or "").strip() or None
        candidates, scopes, discovery = self._discover(
            normalized_source,
            collection_key=normalized_collection,
            import_id=normalized_import,
        )
        manifest = KnowledgeIngestionManifest(self.data_dir)
        plan = build_ingestion_plan(
            manifest,
            candidates,
            scopes=scopes,
            detect_missing=bool(detect_missing and discovery.get("complete")),
            complete_scopes=scopes if discovery.get("complete") else (),
        )
        fingerprint = _plan_fingerprint(plan)
        plan_id = f"ksp_{secrets.token_urlsafe(18)}"
        record = {
            "schema_version": 1,
            "plan_id": plan_id,
            "source": normalized_source,
            "collection_key": normalized_collection,
            "import_id": normalized_import,
            "fingerprint": fingerprint,
            "expires_in_seconds": int(self.plan_ttl_seconds),
            "created_at": time.time(), "expires_at": time.time() + self.plan_ttl_seconds,
            "status": "planned", "attempts": 0, "confirmed": False,
            "detect_missing": bool(detect_missing and discovery.get("complete")),
            "source_root_hash": sha256(str(self.vault_root).encode()).hexdigest() if normalized_source == "obsidian" else None,
            "results": [], "receipt": None,
            "discovery": discovery,
            "warnings": ["来源快照不完整或版本不一致；已禁用删除推断。"] if detect_missing and not discovery.get("complete") else [],
            "plan": plan.to_dict(),
            "read_only": True,
        }
        self.jobs.save(record)
        return {**self.jobs.public(record), "expires_in_seconds": int(self.plan_ttl_seconds)}

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        return self.jobs.public(self.jobs.get(plan_id))

    def list_jobs(self) -> dict[str, Any]:
        records = self.jobs.records()
        return {"items": [self.jobs.public(item) for item in records[:100]], "count": len(records), "worker_running": bool(self._worker and self._worker.is_alive()), "read_only": True}

    def submit_plan(self, plan_id: str, *, allow_deletes: bool = False) -> dict[str, Any]:
        with self._lock:
            record = self.jobs.get(plan_id)
            if record["status"] == "applied":
                return {**record["receipt"], "idempotent_replay": True}
            self._validate_authorization(record, allow_deletes)
            if record["status"] not in {"queued", "running"}:
                record.update(status="queued", confirmed=True, allow_deletes=bool(allow_deletes), error_code=None, error_type=None)
                self.jobs.save(record)
            self._kick_worker()
        return self.get_plan(plan_id)

    def _validate_authorization(self, record: dict[str, Any], allow_deletes: bool) -> None:
        if record["status"] in {"stale", "expired"}:
            raise DomainQueryError("stale_knowledge_sync_plan", "create a fresh plan before applying changed sources")
        if not record.get("confirmed") and record["expires_at"] < time.time():
            raise DomainQueryError("knowledge_sync_plan_not_found", "knowledge synchronization plan has expired")
        if record.get("confirmed") and record.get("allow_deletes", False) != bool(allow_deletes):
            raise DomainQueryError("invalid_argument", "delete authorization differs from the confirmed job; create a new plan")

    def _kick_worker(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            if not any(item["status"] == "queued" for item in self.jobs.records()):
                return
            self._worker = Thread(target=self._run_queued, name="scholarflow-knowledge-sync", daemon=True)
            self._worker.start()

    def _run_queued(self) -> None:
        while True:
            with self._lock:
                queued = [item for item in self.jobs.records() if item["status"] == "queued"]
                if not queued:
                    self._worker = None
                    return
                record = queued[-1]
            try:
                self.apply_plan(record["plan_id"], allow_deletes=record.get("allow_deletes", False))
            except DomainQueryError as exc:
                if exc.code == "knowledge_engine_busy":
                    time.sleep(.25)
                # Other errors are durable and wait for explicit retry.
            except Exception:
                # apply_plan records a sanitized failure; do not hot-loop a failed job.
                return

    def resume_pending(self) -> None:
        for record in self.jobs.records():
            if record["status"] == "running" and record.get("confirmed"):
                record.update(status="queued", recovery="interrupted_execution")
                self.jobs.save(record)
        self._kick_worker()

    def apply_plan(
        self, plan_id: str, *, allow_deletes: bool = False,
        wait_timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        self.jobs.root.mkdir(parents=True, exist_ok=True)
        try:
            with file_lock(self.jobs.root / "execution", timeout_seconds=.2):
                return self._apply_locked(plan_id, allow_deletes=allow_deletes, wait_timeout_seconds=wait_timeout_seconds)
        except PersistenceLockTimeout as exc:
            raise DomainQueryError("knowledge_engine_busy", "another knowledge synchronization job is running") from exc

    def _apply_locked(self, plan_id: str, *, allow_deletes: bool, wait_timeout_seconds: float) -> dict[str, Any]:
        record = self.jobs.get(plan_id)
        if record["status"] == "applied":
            return {**record["receipt"], "idempotent_replay": True}
        self._validate_authorization(record, allow_deletes)
        try:
            if record.get("source_root_hash") and record["source_root_hash"] != sha256(str(self.vault_root).encode()).hexdigest():
                raise DomainQueryError("stale_knowledge_sync_plan", "configured source root changed; create a new plan")
            candidates, scopes, discovery = self._discover(record["source"], collection_key=record["collection_key"], import_id=record["import_id"])
            if record["detect_missing"] and not discovery.get("complete"):
                raise DomainQueryError("stale_knowledge_sync_plan", "source snapshot is no longer complete; recreate the plan")
            refreshed = build_ingestion_plan(KnowledgeIngestionManifest(self.data_dir), candidates, scopes=scopes, detect_missing=record["detect_missing"], complete_scopes=scopes if discovery.get("complete") else ())
            if not record.get("attempts"):
                if _plan_fingerprint(refreshed) != record["fingerprint"]:
                    raise DomainQueryError("stale_knowledge_sync_plan", "knowledge sources or manifest changed; create a new plan")
                remaining = refreshed
            else:
                remaining, reconciled = self._reconcile_remaining(record, refreshed, allow_deletes)
                record["results"] = reconciled
            record.update(status="running", confirmed=True, allow_deletes=bool(allow_deletes), attempts=record.get("attempts", 0) + 1, error_code=None, error_type=None)
            self.jobs.save(record)
            engine = build_knowledge_engine(self.data_dir)
            if not isinstance(engine, LightRAGClient):
                raise DomainQueryError("knowledge_engine_unavailable", "LightRAG must be enabled before applying synchronization")
            def progress(result):
                results = {item["source_ref"]: item for item in record["results"]}
                results[result["source_ref"]] = result
                record["results"] = list(results.values())
                self.jobs.save(record)
            result = apply_ingestion_plan(
                KnowledgeIngestionManifest(self.data_dir), engine, remaining,
                allow_deletes=bool(allow_deletes),
                wait_timeout_seconds=max(1.0, min(float(wait_timeout_seconds), 900.0)),
                progress=progress,
            )
            combined = {item["source_ref"]: item for item in record["results"]}
            combined.update({item["source_ref"]: item for item in result.get("results", [])})
            if combined:
                counts: dict[str, int] = {}
                for item in combined.values():
                    counts[item["status"]] = counts.get(item["status"], 0) + 1
                result = {**result, "results": list(combined.values()), "counts": counts}
            receipt = {"plan_id": plan_id, "fingerprint": record["fingerprint"], **result, "idempotent_replay": False}
            record.update(status="applied", receipt=receipt, finished_at=time.time())
            self.jobs.save(record)
            return receipt
        except Exception as exc:
            code = str(getattr(exc, "code", "knowledge_sync_execution_failed"))
            record.update(status="stale" if code == "stale_knowledge_sync_plan" else "failed", error_code=code, error_type=type(exc).__name__)
            self.jobs.save(record)
            raise

    @staticmethod
    def _reconcile_remaining(record: dict[str, Any], refreshed: IngestionPlan, allow_deletes: bool) -> tuple[IngestionPlan, list[dict[str, Any]]]:
        original = {item["source_ref"]: item for item in record["plan"]["actions"]}
        current = {item.source_ref: item for item in refreshed.actions}
        if set(current) - set(original):
            raise DomainQueryError("stale_knowledge_sync_plan", "source scope changed during execution")
        remaining = []
        completed = []
        statuses = {"add": "added", "update": "updated", "refresh": "refreshed", "unchanged": "unchanged"}
        for source_ref, before in original.items():
            action = current.get(source_ref)
            if action is not None and action.to_dict() == before:
                remaining.append(action)
            elif action is not None and action.action == "unchanged" and before["action"] in statuses and action.to_dict().get("candidate") == before.get("candidate"):
                completed.append({"source_ref": source_ref, "status": statuses[before["action"]], "reconciled": True})
            elif action is None and before["action"] == "remove":
                completed.append({"source_ref": source_ref, "status": "deleted" if allow_deletes else "delete_deferred", "reconciled": True})
            else:
                raise DomainQueryError("stale_knowledge_sync_plan", "source content or unexpected index state changed; recreate the plan")
        return IngestionPlan(scopes=refreshed.scopes, actions=tuple(remaining)), completed

    def _discover(
        self,
        source: str,
        *,
        collection_key: str | None,
        import_id: str | None,
    ) -> tuple[list[IngestionCandidate], list[str], dict[str, Any]]:
        if source == "obsidian":
            if self.vault_root is None or not self.vault_root.is_dir():
                raise DomainQueryError(
                    "knowledge_source_unavailable",
                    "an existing Obsidian vault must be configured first",
                )
            root_id = "configured-vault"
            scope = f"obsidian:{root_id}"
            return (
                discover_file_sources(
                    self.vault_root,
                    source_type="obsidian",
                    root_id=root_id,
                ),
                [scope],
                {"complete": True},
            )
        if source == "zotero":
            client = self.zotero_factory()
            backend = str(getattr(client, "backend", "null") or "null")
            if backend == "null":
                raise DomainQueryError(
                    "knowledge_source_unavailable",
                    "Zotero is not configured or reachable",
                )
            scope = f"zotero:{backend}:{collection_key or 'all'}"
            discovery: dict[str, Any] = {}
            return (
                discover_zotero_sources(
                    client,
                    collection_key=collection_key,
                    complete_snapshot=True,
                    discovery=discovery,
                ),
                [scope],
                discovery,
            )
        if source == "local_pdf":
            if not import_id:
                raise DomainQueryError(
                    "invalid_argument",
                    "local PDF synchronization requires an import_id",
                )
            candidate = self.reader_store.ingestion_candidate(import_id)
            return [candidate], [candidate.scope], {"complete": True}
        raise DomainQueryError(
            "invalid_argument",
            "source must be obsidian, zotero, or local_pdf",
        )



def _plan_fingerprint(plan: IngestionPlan) -> str:
    encoded = json.dumps(
        plan.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{sha256(encoded).hexdigest()}"
