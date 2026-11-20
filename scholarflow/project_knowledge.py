"""Automatic project-derived knowledge snapshots and LightRAG indexing.

Only confirmed ScholarFlow domain records are mirrored here.  Conversation
history and model-authored memory are deliberately excluded from the snapshot,
so retrieval can never silently promote chat into research truth.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import threading
from typing import Any, Callable, Iterable, Mapping

from .domain_queries import ScholarFlowQueryService
from .event_bus import append_event
from .knowledge_engine import build_knowledge_engine
from .knowledge_ingestion import (
    IngestionCandidate,
    KnowledgeIngestionManifest,
    apply_ingestion_plan,
    build_ingestion_plan,
)
from .persistence import atomic_write_json, atomic_write_text
from .storage import ScholarFlowStore


PROJECT_KNOWLEDGE_STATE_VERSION = 1
PROJECT_KNOWLEDGE_STATE_FILENAME = "project-sync.json"


class ProjectKnowledgeService:
    """Maintain one deterministic AI-facing snapshot per research project."""

    def __init__(
        self,
        store: ScholarFlowStore,
        *,
        engine_factory: Callable[[Path], Any] = build_knowledge_engine,
        background: bool = True,
        wait_timeout_seconds: float = 90.0,
    ) -> None:
        self.store = store
        self.queries = ScholarFlowQueryService(store)
        self.engine_factory = engine_factory
        self.background = bool(background)
        self.wait_timeout_seconds = max(1.0, float(wait_timeout_seconds))
        self.root = store.root_dir / "_knowledge"
        self.snapshot_root = self.root / "project-snapshots"
        self.state_path = self.root / PROJECT_KNOWLEDGE_STATE_FILENAME
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._requested_projects: set[str] = set()
        self._request_all = False

    def status(self, *, project_id: str | None = None) -> dict[str, Any]:
        normalized = str(project_id or "").strip()
        with self._lock:
            items = list(self._read_state()["projects"].values())
            worker_alive = bool(self._worker and self._worker.is_alive())
        if normalized:
            items = [item for item in items if item.get("project_id") == normalized]
        items.sort(key=lambda item: str(item.get("generated_at") or ""), reverse=True)
        counts: dict[str, int] = {}
        for item in items:
            state = str(item.get("status") or "unknown")
            counts[state] = counts.get(state, 0) + 1
        return {
            "schema_version": 1,
            "automatic": True,
            "source_of_truth": "confirmed_scholarflow_domain",
            "chat_is_excluded": True,
            "worker_running": worker_alive,
            "count": len(items),
            "counts": counts,
            "items": items,
        }

    def schedule(
        self,
        project_ids: Iterable[str] | None = None,
        *,
        trigger: str = "domain_change",
    ) -> dict[str, Any]:
        selected = self._project_ids(project_ids)
        scheduled: list[str] = []
        for project_id in selected:
            candidate, snapshot_path = self._write_snapshot(project_id)
            self._update_project(
                project_id,
                {
                    "project_id": project_id,
                    "source_ref": candidate.source_ref,
                    "title": candidate.title,
                    "snapshot_path": snapshot_path,
                    "content_hash": f"sha256:{candidate.digest}",
                    "status": "pending",
                    "trigger": str(trigger or "domain_change")[:200],
                    "generated_at": _now(),
                    "last_error": None,
                },
            )
            scheduled.append(project_id)
            append_event(
                self.store.root_dir,
                event_type="knowledge.project.snapshot.updated",
                payload={
                    "project_id": project_id,
                    "source_ref": candidate.source_ref,
                    "content_hash": f"sha256:{candidate.digest}",
                    "trigger": trigger,
                },
            )
        if scheduled and self.background:
            try:
                configured_engine = self.engine_factory(self.store.root_dir)
                if not hasattr(configured_engine, "ingest_text"):
                    message = "LightRAG is not enabled; the project snapshot remains queued."
                    for project_id in scheduled:
                        self._mark_waiting(project_id, message)
                else:
                    self._start_worker()
            except Exception as exc:
                for project_id in scheduled:
                    self._mark_waiting(
                        project_id,
                        f"{type(exc).__name__}: {exc}"[:1_000],
                    )
        return {"scheduled": scheduled, **self.status()}

    def retry(
        self,
        *,
        project_id: str | None = None,
        wait: bool = False,
    ) -> dict[str, Any]:
        selected = self._project_ids([project_id] if project_id else None)
        if not selected:
            return self.status(project_id=project_id)
        for item in selected:
            if item not in self._read_state()["projects"]:
                self.schedule([item], trigger="manual_retry")
        if wait:
            self._drain(selected)
        else:
            self._start_worker(selected)
        return self.status(project_id=project_id)

    def snapshot(self, project_id: str) -> dict[str, Any]:
        candidate, snapshot_path = self._write_snapshot(project_id, persist=False)
        return {
            "project_id": project_id,
            "source_ref": candidate.source_ref,
            "title": candidate.title,
            "content_hash": f"sha256:{candidate.digest}",
            "snapshot_path": snapshot_path,
            "text": candidate.text,
            "source_of_truth": "confirmed_scholarflow_domain",
            "chat_is_excluded": True,
        }

    def resume_pending(self) -> dict[str, Any]:
        """Resume durable bridge-created queue entries in a resident process."""

        retryable = {
            str(item.get("project_id"))
            for item in self._read_state()["projects"].values()
            if str(item.get("status") or "")
            in {"pending", "waiting_for_engine", "failed"}
            and str(item.get("project_id") or "").strip()
        }
        if retryable:
            self._start_worker(retryable)
        return self.status()

    def reconcile(
        self,
        *,
        trigger: str = "startup_reconcile",
        wait: bool = False,
    ) -> dict[str, Any]:
        """Detect missing or stale project snapshots and queue only those projects.

        Normal writes already schedule snapshots immediately. This startup audit
        closes the recovery gap after manual data repair, interrupted state
        persistence, or an ingestion-manifest reset.
        """

        project_ids = self._project_ids(None)
        state = self._read_state()["projects"]
        manifest_sources = KnowledgeIngestionManifest(self.store.root_dir).read()[
            "sources"
        ]
        stale: list[str] = []
        for project_id in project_ids:
            candidate, snapshot_path = self._write_snapshot(project_id, persist=False)
            expected_hash = f"sha256:{candidate.digest}"
            current = state.get(project_id) or {}
            indexed = manifest_sources.get(candidate.source_ref) or {}
            snapshot_exists = (self.store.root_dir / snapshot_path).is_file()
            if (
                current.get("content_hash") != expected_hash
                or indexed.get("content_hash") != expected_hash
                or not snapshot_exists
            ):
                stale.append(project_id)

        if stale:
            self.schedule(stale, trigger=trigger)
            if wait:
                self._drain(stale)
        report = self.status()
        report.update(
            {
                "checked": len(project_ids),
                "scheduled": stale,
                "stale_count": len(stale),
            }
        )
        return report

    def close(self, *, timeout_seconds: float = 2.0) -> None:
        with self._lock:
            worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=max(0.0, float(timeout_seconds)))

    def _start_worker(self, project_ids: Iterable[str] | None = None) -> None:
        with self._lock:
            selected = {str(item) for item in (project_ids or ()) if str(item)}
            if selected:
                self._requested_projects.update(selected)
            else:
                self._request_all = True
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run_worker,
                name="scholarflow-project-knowledge",
                daemon=True,
            )
            self._worker.start()

    def _run_worker(self) -> None:
        while True:
            with self._lock:
                selected = None if self._request_all else tuple(self._requested_projects)
                self._request_all = False
                self._requested_projects.clear()
            self._drain(selected)
            with self._lock:
                if self._request_all or self._requested_projects:
                    continue
                self._worker = None
                return

    def _drain(self, project_ids: Iterable[str] | None = None) -> None:
        state = self._read_state()
        selected = set(project_ids or ())
        pending = [
            project_id
            for project_id, item in state["projects"].items()
            if (not selected or project_id in selected)
            and item.get("status") in {"pending", "waiting_for_engine", "failed"}
        ]
        if not pending:
            return
        try:
            engine = self.engine_factory(self.store.root_dir)
            health = engine.health()
            if not bool(health.get("available")) or not hasattr(engine, "ingest_text"):
                message = "LightRAG is not enabled or reachable; the project snapshot remains queued."
                for project_id in pending:
                    self._mark_waiting(project_id, message)
                return
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:1_000]
            for project_id in pending:
                self._mark_waiting(project_id, message)
            return
        for project_id in pending:
            try:
                self._index_project(project_id, engine)
            except Exception as exc:
                self._update_project(
                    project_id,
                    {
                        "status": "failed",
                        "last_error": f"{type(exc).__name__}: {exc}"[:1_000],
                        "last_attempt_at": _now(),
                    },
                )
                append_event(
                    self.store.root_dir,
                    event_type="knowledge.project.index.failed",
                    payload={"project_id": project_id, "error": str(exc)[:1_000]},
                )

    def _index_project(self, project_id: str, engine: Any) -> None:
        candidate, _ = self._write_snapshot(project_id)
        manifest = KnowledgeIngestionManifest(self.store.root_dir)
        plan = build_ingestion_plan(
            manifest,
            [candidate],
            scopes=[candidate.scope],
            detect_missing=False,
        )
        result = apply_ingestion_plan(
            manifest,
            engine,
            plan,
            allow_deletes=False,
            wait_timeout_seconds=self.wait_timeout_seconds,
            poll_interval_seconds=0.25,
        )
        changed = any(
            item.get("status") in {"added", "updated"}
            for item in result.get("results") or []
        )
        expected_hash = f"sha256:{candidate.digest}"
        current = self._read_state()["projects"].get(project_id) or {}
        if current.get("content_hash") != expected_hash:
            self._update_project(
                project_id,
                {
                    "status": "pending",
                    "last_error": None,
                    "last_attempt_at": _now(),
                },
            )
            self._start_worker([project_id])
            return
        self._update_project(
            project_id,
            {
                "status": "indexed",
                "last_error": None,
                "last_attempt_at": _now(),
                "indexed_at": _now(),
                "index_result": result,
            },
        )
        append_event(
            self.store.root_dir,
            event_type="knowledge.project.index.completed",
            payload={"project_id": project_id, "changed": changed},
        )

    def _mark_waiting(self, project_id: str, message: str) -> None:
        self._update_project(
            project_id,
            {
                "status": "waiting_for_engine",
                "last_error": message[:1_000],
                "last_attempt_at": _now(),
            },
        )

    def _project_ids(self, values: Iterable[str] | None) -> list[str]:
        if values is None:
            return [
                str(item["stage_id"])
                for item in self.queries.search_project_stages(limit=100)["items"]
            ]
        normalized: list[str] = []
        for value in values:
            item = str(value or "").strip()
            if item and item not in normalized:
                self.queries.get_project_stage(item)
                normalized.append(item)
        return normalized

    def _write_snapshot(
        self,
        project_id: str,
        *,
        persist: bool = True,
    ) -> tuple[IngestionCandidate, str]:
        project = self.queries.get_project_stage(project_id)
        tasks = self.queries.search_tasks(project_stage_id=project_id, limit=100)["items"]
        experiments = self.queries.search_experiments(project_stage_id=project_id, limit=100)["items"]
        artifacts = self.queries.search_research_artifacts(project_stage_id=project_id, limit=100)["items"]
        papers = self._project_papers(project, tasks, experiments, artifacts)
        relations = self._project_relations(project_id, tasks, experiments, artifacts, papers)
        text = _render_snapshot(project, tasks, experiments, artifacts, papers, relations)
        safe_id = _safe_snapshot_name(project_id)
        target = self.snapshot_root / f"{safe_id}.md"
        if persist:
            atomic_write_text(target, text)
        relative = target.relative_to(self.store.root_dir).as_posix()
        candidate = IngestionCandidate(
            source_ref=f"project:{project_id}",
            source_type="project_snapshot",
            title=f"Project knowledge: {project.get('project_name') or project_id}",
            text=text,
            scope=f"project:{project_id}",
            locator=relative,
            source_version=str(project.get("updated_at") or _now()),
            metadata={
                "project_stage_id": project_id,
                "generated_from": "confirmed_scholarflow_domain",
                "chat_excluded": True,
                "relative_path": relative,
            },
        )
        return candidate, relative

    def _project_papers(
        self,
        project: Mapping[str, Any],
        tasks: list[dict[str, Any]],
        experiments: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        paper_ids = {
            str(ref).removeprefix("paper:")
            for ref in project.get("evidence_refs") or []
            if str(ref).startswith("paper:")
        }
        task_ids = {str(item.get("task_id")) for item in tasks}
        experiment_ids = {str(item.get("experiment_id")) for item in experiments}
        for item in tasks + artifacts:
            paper_ids.update(str(ref).removeprefix("paper:") for ref in item.get("related_papers") or [])
        selected = []
        for paper in self.queries.search_papers(limit=100)["items"]:
            if (
                str(paper.get("paper_id")) in paper_ids
                or task_ids.intersection(str(item) for item in paper.get("related_tasks") or [])
                or experiment_ids.intersection(str(item) for item in paper.get("related_experiments") or [])
            ):
                selected.append(paper)
        return selected

    def _project_relations(
        self,
        project_id: str,
        tasks: list[dict[str, Any]],
        experiments: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
        papers: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        refs = {project_id, f"project_stage:{project_id}"}
        refs.update(f"task:{item['task_id']}" for item in tasks)
        refs.update(f"experiment:{item['experiment_id']}" for item in experiments)
        refs.update(f"research_artifact:{item['artifact_id']}" for item in artifacts)
        refs.update(f"paper:{item['paper_id']}" for item in papers)
        return [
            item
            for item in self.queries.search_knowledge_relations(limit=100)["items"]
            if str(item.get("subject_ref")) in refs or str(item.get("object_ref")) in refs
        ]

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": PROJECT_KNOWLEDGE_STATE_VERSION, "projects": {}}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": PROJECT_KNOWLEDGE_STATE_VERSION, "projects": {}}
        if not isinstance(payload, Mapping) or not isinstance(payload.get("projects"), Mapping):
            return {"schema_version": PROJECT_KNOWLEDGE_STATE_VERSION, "projects": {}}
        return {
            "schema_version": PROJECT_KNOWLEDGE_STATE_VERSION,
            "projects": {
                str(key): dict(value)
                for key, value in payload["projects"].items()
                if isinstance(value, Mapping)
            },
        }

    def _update_project(self, project_id: str, changes: Mapping[str, Any]) -> None:
        with self._lock:
            state = self._read_state()
            current = dict(state["projects"].get(project_id) or {})
            current.update(dict(changes))
            state["projects"][project_id] = current
            atomic_write_json(self.state_path, state)


def _render_snapshot(
    project: Mapping[str, Any],
    tasks: list[dict[str, Any]],
    experiments: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    papers: list[dict[str, Any]],
    relations: list[dict[str, Any]],
) -> str:
    lines = [
        "---",
        "scholarflow_source: confirmed_domain",
        f"project_stage_id: {project.get('stage_id')}",
        "chat_content_included: false",
        "---",
        "",
        f"# {project.get('project_name') or project.get('stage_id')}",
        "",
        f"Stage: {project.get('title') or ''}",
        f"Status: {project.get('status') or ''}",
        f"Goal: {project.get('goal') or ''}",
        f"Progress: {project.get('progress_note') or ''}",
        "",
        "## Risks",
        *_bullets(project.get("risks") or []),
        "",
        "## Next actions",
        *_bullets(project.get("next_actions") or []),
        "",
        "## Tasks",
    ]
    lines.extend(
        f"- [{item.get('status')}] {item.get('title')} ({item.get('task_id')}): {item.get('description') or ''}"
        for item in tasks
    )
    lines.extend(["", "## Experiments"])
    lines.extend(
        f"- [{item.get('status')}] {item.get('title')} ({item.get('experiment_id')}): goal={item.get('goal') or ''}; result={item.get('result_summary') or ''}; next={'; '.join(item.get('next_actions') or [])}"
        for item in experiments
    )
    lines.extend(["", "## Papers and evidence"])
    lines.extend(
        f"- {item.get('title')} ({item.get('paper_id')}, {item.get('year') or 'year unknown'}): {item.get('summary') or item.get('relevance') or ''}"
        for item in papers
    )
    lines.extend(["", "## Research artifacts"])
    lines.extend(
        f"### {item.get('title')} ({item.get('artifact_id')}, {item.get('status')})\n\n{str(item.get('content') or '')[:20_000]}"
        for item in artifacts
    )
    lines.extend(["", "## Confirmed knowledge relations"])
    lines.extend(
        f"- {item.get('subject_ref')} --{item.get('predicate')}--> {item.get('object_ref')}; evidence={', '.join(item.get('evidence_refs') or [])}; note={item.get('note') or ''}"
        for item in relations
    )
    lines.extend(["", "## Provenance", "- Generated only from confirmed ScholarFlow typed records.", "- Conversation history, transient model memory, and unconfirmed proposals are excluded.", ""])
    return "\n".join(lines)[:250_000]


def _bullets(values: Iterable[Any]) -> list[str]:
    selected = [f"- {str(item)}" for item in values if str(item).strip()]
    return selected or ["- None recorded"]


def _safe_snapshot_name(project_id: str) -> str:
    normalized = str(project_id or "").strip()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip(".-") or "project"
    digest = sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{slug[:80]}-{digest}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
