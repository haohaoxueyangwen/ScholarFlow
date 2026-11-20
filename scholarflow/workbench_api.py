"""Loopback-only application API for the ScholarFlow research workbench."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request as UrlRequest, urlopen

from .domain_queries import DomainQueryError, ScholarFlowQueryService
from .conversation import message_text
from .event_bus import append_event, list_events
from .gateway import (
    acknowledge_gateway_message,
    enqueue_workflow_request,
    gateway_status,
    list_gateway_inbox,
)
from .intake import IntakeService
from .knowledge_candidates import CandidateKnowledgeService
from .knowledge_engine import build_knowledge_engine, prepare_knowledge_bundle
from .knowledge_ingestion import KnowledgeIngestionManifest, summarize_ingestion_manifest
from .knowledge_sync import KnowledgeSyncService
from .literature_search import LiteratureSearchError, ResilientLiteratureSearch
from .project_knowledge import ProjectKnowledgeService
from .manuscript import ManuscriptService
from .mutations import MutationError, TaskMutationService
from .proposal_state import (
    load_proposal_state,
    record_rejection_audit_event,
    reject_proposal,
)
from .research_mutations import KBMutationService, ResearchMutationService
from .reader_workspace import MAX_READER_PDF_BYTES, ReaderWorkspaceStore
from .reader_translation import ReaderTranslationService
from .remote_pdf import RemotePdfError, fetch_remote_pdf
from .runtime_supervisor import get_pi_host_status
from .workbench_telemetry import record_http_error
from .web_research import WebResearchService
from .runtime_supervisor import runtime_health_summary
from .scheduler import list_automation_jobs
from .quality import audit_knowledge_engine_manifest
from .storage import ScholarFlowStore
from .pi_lifecycle import node_command
from .pi_rpc_host import PiRpcConfig, PiRpcHostError, SubprocessPiRpcProcess
from .workbench_agent import (
    DEFAULT_PI_MODEL,
    DEFAULT_PI_PROVIDER,
    WorkbenchAgentError,
    WorkbenchAgentSessionService,
)
from .zotero_client import (
    ZoteroClientError,
    ZoteroLocalClient,
    build_zotero_client,
    describe_zotero_client,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
ALLOWED_ORIGINS = {
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://[::1]:3000",
}
ALLOWED_ORIGINS_ENV = "SCHOLARFLOW_WORKBENCH_ALLOWED_ORIGINS"
SESSION_COOKIE = "scholarflow_workbench_session"
READER_TRANSLATE_MAX_SOURCE_CHARS = 5_000
READER_TRANSLATE_TIMEOUT_SECONDS = 180.0

AUTOMATION_WORKFLOWS = {
    "daily_review": {
        "label": "今日研究复盘",
        "skill_name": "scholarflow-automation-inbox",
        "instruction": (
            "Use the scholarflow-daily-review Skill to review current research "
            "evidence and propose a daily_report artifact. Do not apply writes."
        ),
    },
    "weekly_review": {
        "label": "本周研究复盘",
        "skill_name": "scholarflow-automation-inbox",
        "instruction": (
            "Use the scholarflow-weekly-review Skill to review projects, tasks, "
            "papers, experiments, and artifacts. Do not apply writes."
        ),
    },
    "experiment_watch": {
        "label": "实验状态检查",
        "skill_name": "scholarflow-automation-inbox",
        "instruction": (
            "Find running experiments, inspect only approved evidence, and "
            "propose evidence-backed log updates when needed. Do not apply writes."
        ),
    },
}


def _allowed_origins() -> set[str]:
    """Return the fixed UI origins plus explicitly configured loopback origins."""

    origins = set(ALLOWED_ORIGINS)
    for candidate in os.getenv(ALLOWED_ORIGINS_ENV, "").split(","):
        origin = candidate.strip().rstrip("/")
        if not origin:
            continue
        parsed = urlparse(origin)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and parsed.port is not None
            and not parsed.username
            and not parsed.password
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        ):
            origins.add(origin)
    return origins


class WorkbenchApplication:
    """Thin composition layer over existing deterministic services."""

    def __init__(
        self,
        data_dir: Path,
        *,
        agent_sessions: WorkbenchAgentSessionService | None = None,
        repository_root: Path | None = None,
        vault_root: Path | None = None,
        literature_searcher: Any | None = None,
        zotero_client: Any | None = None,
    ):
        self.store = ScholarFlowStore(Path(data_dir))
        self.queries = ScholarFlowQueryService(self.store)
        self.intake = IntakeService(self.store)
        self.candidates = CandidateKnowledgeService(self.store)
        self.tasks = TaskMutationService(self.store)
        self.research = ResearchMutationService(self.store)
        self.manuscripts = ManuscriptService(self.store)
        configured_vault = vault_root or (
            Path(os.environ["SCHOLARFLOW_VAULT_DIR"]).expanduser()
            if str(os.environ.get("SCHOLARFLOW_VAULT_DIR") or "").strip()
            else None
        )
        self.vault_root = (
            Path(configured_vault).resolve() if configured_vault else None
        )
        self.kb = (
            KBMutationService(self.store, self.vault_root)
            if self.vault_root is not None and self.vault_root.exists()
            else None
        )
        self.reader = ReaderWorkspaceStore(self.store.root_dir)
        self.web_research = WebResearchService(self.store.root_dir)
        self.knowledge_sync = KnowledgeSyncService(
            self.store.root_dir,
            vault_root=self.vault_root,
            reader_store=self.reader,
        )
        self.project_knowledge = ProjectKnowledgeService(self.store)
        self._background_services_started = False
        self.agent_sessions = agent_sessions or WorkbenchAgentSessionService(
            self.store.root_dir
        )
        self.repository_root = Path(
            repository_root or Path(__file__).resolve().parents[1]
        )
        self.reader_translations = ReaderTranslationService(
            self.store.root_dir,
            runner=lambda source, register_process: _run_isolated_reader_translation(
                repository_root=self.repository_root,
                data_dir=self.store.root_dir,
                source=source,
                on_process=register_process,
            ),
        )
        self.literature_searcher = literature_searcher or ResilientLiteratureSearch()
        self._configured_zotero_client = zotero_client

    def start_background_services(self) -> None:
        """Start resident-only recovery workers after the HTTP server owns them."""

        if self._background_services_started:
            return
        self._background_services_started = True
        # Pi bridge commands are short-lived: they persist snapshots and queue
        # entries, while this resident process owns reconciliation and async
        # ingestion. The audit also recovers out-of-band confirmed-data repairs.
        self.project_knowledge.reconcile(trigger="workbench_startup")
        self.project_knowledge.resume_pending()
        self.knowledge_sync.resume_pending()

    def dashboard(self) -> dict[str, Any]:
        projects = self.queries.search_project_stages(limit=20)
        tasks = self.queries.search_tasks(limit=100)
        papers = self.queries.search_papers(limit=100)
        experiments = self.queries.search_experiments(limit=100)
        artifacts = self.queries.search_research_artifacts(limit=50)
        relations = self.queries.search_knowledge_relations(limit=50)
        candidates = self.candidates.list(limit=100)
        proposals = self.proposals()
        active_tasks = [
            item
            for item in tasks["items"]
            if item.get("status") in {"pending", "in_progress", "blocked"}
        ]
        return {
            "schema_version": 1,
            "generated_at": _now(),
            "source": "scholarflow-domain",
            "summary": {
                "active_projects": sum(
                    item.get("status") == "active"
                    for item in projects["items"]
                ),
                "open_tasks": len(active_tasks),
                "papers": papers["total_matches"],
                "running_experiments": sum(
                    item.get("status") == "running"
                    for item in experiments["items"]
                ),
                "pending_candidates": sum(
                    item.get("status") == "pending"
                    for item in candidates["items"]
                ),
                "pending_proposals": proposals["count"],
            },
            "projects": projects["items"],
            "focus_tasks": active_tasks[:8],
            "recent_papers": papers["items"][:6],
            "experiments": experiments["items"][:6],
            "artifacts": artifacts["items"][:6],
            "relations": relations["items"][:8],
            "candidates": candidates["items"][:8],
            "proposals": proposals["items"],
            "proposal_receipts": self.proposal_history(limit=8)["items"],
            "runtime": _safe_runtime_status(self.store.root_dir),
        }

    def project_workspace(self, stage_id: str) -> dict[str, Any]:
        """Aggregate one project's existing typed truth for the Workbench."""

        project = self.queries.get_project_stage(stage_id)
        normalized_stage_id = str(project["stage_id"])
        tasks = self.queries.search_tasks(
            project_stage_id=normalized_stage_id,
            limit=100,
        )["items"]
        experiments = self.queries.search_experiments(
            project_stage_id=normalized_stage_id,
            limit=100,
        )["items"]
        artifacts = self.queries.search_research_artifacts(
            project_stage_id=normalized_stage_id,
            limit=100,
        )["items"]

        task_ids = {str(item["task_id"]) for item in tasks}
        experiment_ids = {str(item["experiment_id"]) for item in experiments}
        artifact_ids = {str(item["artifact_id"]) for item in artifacts}
        paper_ids = {
            str(ref).split(":", 1)[1]
            for ref in project.get("evidence_refs") or []
            if str(ref).startswith("paper:")
        }
        for task in tasks:
            paper_ids.update(str(item) for item in task.get("related_papers") or [])
        for artifact in artifacts:
            paper_ids.update(str(item) for item in artifact.get("related_papers") or [])
        all_papers = [paper.to_dict() for paper in self.store.list_papers()]
        for paper in all_papers:
            if task_ids.intersection(str(item) for item in paper.get("related_tasks") or []):
                paper_ids.add(str(paper["paper_id"]))
            if experiment_ids.intersection(str(item) for item in paper.get("related_experiments") or []):
                paper_ids.add(str(paper["paper_id"]))
        papers_by_id = {str(item["paper_id"]): item for item in all_papers}
        for paper_id in sorted(paper_ids):
            if paper_id in papers_by_id:
                continue
            try:
                papers_by_id[paper_id] = self.queries.get_paper(paper_id)
            except DomainQueryError:
                continue
        papers = [papers_by_id[paper_id] for paper_id in sorted(paper_ids) if paper_id in papers_by_id]

        related_objects = {
            "project_stage": {normalized_stage_id},
            "task": task_ids,
            "experiment": experiment_ids,
            "research_artifact": artifact_ids,
            "paper": paper_ids,
        }
        pending = [
            item
            for item in self.proposals()["items"]
            if _project_item_matches(item, normalized_stage_id, related_objects)
        ]
        history = [
            item
            for item in self.proposal_history(limit=200)["items"]
            if _project_item_matches(item, normalized_stage_id, related_objects)
        ]
        evidence = self.object_evidence(
            f"project_stage:{normalized_stage_id}",
            limit=100,
        )
        open_tasks = [
            item for item in tasks
            if item.get("status") in {"pending", "in_progress", "blocked"}
        ]
        return {
            "schema_version": 1,
            "generated_at": _now(),
            "read_only": True,
            "project": project,
            "summary": {
                "task_count": len(tasks),
                "open_task_count": len(open_tasks),
                "blocked_task_count": sum(item.get("status") == "blocked" for item in tasks),
                "paper_count": len(papers),
                "experiment_count": len(experiments),
                "running_experiment_count": sum(item.get("status") == "running" for item in experiments),
                "artifact_count": len(artifacts),
                "draft_count": sum(item.get("status") == "draft" for item in artifacts),
                "pending_proposal_count": len(pending),
            },
            "framing": {
                "title": project.get("title"),
                "goal": project.get("goal"),
                "risks": project.get("risks") or [],
                "next_actions": project.get("next_actions") or [],
                "evidence_refs": project.get("evidence_refs") or [],
            },
            "tasks": tasks,
            "papers": papers,
            "experiments": experiments,
            "artifacts": artifacts,
            "evidence": evidence,
            "pending_proposals": pending,
            "history": history[:50],
        }

    def literature_search(self, query: str, *, limit: int = 12, **filters) -> dict[str, Any]:
        """Search external scholarly indexes without mutating the governed library."""

        try:
            options = {key: value for key, value in filters.items() if value and not (key == "page" and value == 1)}
            items = self.literature_searcher.search(query, limit=limit, **options)
        except LiteratureSearchError as exc:
            raise DomainQueryError(exc.code, str(exc)) from exc
        local_papers = [paper.to_dict() for paper in self.store.list_papers()]
        local_dois = {
            _normalized_doi((paper.get("external_ids") or {}).get("doi"))
            for paper in local_papers
            if isinstance(paper, Mapping)
        }
        local_titles = {
            _normalized_title(paper.get("title"))
            for paper in local_papers
            if isinstance(paper, Mapping)
        }
        normalized_items = []
        for item in items:
            doi = _normalized_doi(item.get("doi"))
            title = _normalized_title(item.get("title"))
            normalized_items.append(
                {
                    **item,
                    "in_library": bool(
                        (doi and doi in local_dois)
                        or (title and title in local_titles)
                    ),
                }
            )
        return {
            "schema_version": 1,
            "generated_at": _now(),
            "read_only": True,
            "provider": normalized_items[0].get("provider") if normalized_items else "online",
            "query": " ".join(str(query or "").split()),
            "count": len(normalized_items),
            "items": normalized_items,
            "pagination": getattr(items, "metadata", {"page": filters.get("page", 1), "has_more": False, "complete_snapshot": False, "filter_scope": "bounded_provider_response", "total_matches": None}),
        }

    def reader_imports(self) -> dict[str, Any]:
        return self.reader.list_imports(limit=100)

    def _reader_projects(self, values: Any) -> list[str]:
        if values is None:
            return []
        if not isinstance(values, list) or len(values) > 100 or not all(isinstance(value, str) and value.strip() for value in values):
            raise DomainQueryError("invalid_argument", "project_stage_ids must be a list of project IDs")
        projects = sorted({value.strip() for value in values})
        for project_id in projects:
            if self.store.get_project_stage(project_id) is None:
                raise DomainQueryError("invalid_reference", "selected project was not found")
        return projects

    def import_reader_pdf(self, filename: str, payload: bytes, *, project_stage_ids: list[str] | None = None) -> dict[str, Any]:
        return self.reader.import_pdf(filename, payload, origin={"source": "local_upload"}, project_stage_ids=self._reader_projects(project_stage_ids))

    def import_reader_pdf_from_url(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        pdf_url = str(payload.get("pdf_url") or "").strip()
        project_stage_ids = self._reader_projects(payload.get("project_stage_ids"))
        try:
            raw_pdf, _content_type, final_url = fetch_remote_pdf(pdf_url, max_bytes=MAX_READER_PDF_BYTES)
        except RemotePdfError as exc:
            raise DomainQueryError(exc.code, str(exc)) from exc
        filename = self._reader_import_filename(pdf_url=final_url, title=str(payload.get("title") or ""), disposition="")
        doi = _normalized_doi(payload.get("doi"))[:300]
        raw_authors = payload.get("authors")
        authors = [" ".join(str(item).split())[:160] for item in raw_authors[:100] if str(item).strip()] if isinstance(raw_authors, list) else []
        try:
            year = int(payload.get("year")) if not isinstance(payload.get("year"), bool) else None
        except (TypeError, ValueError, OverflowError):
            year = None
        if year is not None and not 1000 <= year <= 2100:
            year = None
        origin = {"source": "remote_open_pdf", "doi": doi, "source_host": str(urlparse(final_url).hostname or "")[:253], "source_url_sha256": sha256(final_url.encode("utf-8")).hexdigest()}
        return self.reader.import_pdf(
            filename,
            raw_pdf,
            origin=origin,
            bibliography={"title": " ".join(str(payload.get("title") or "").split())[:500], "authors": authors, "doi": doi, "year": year},
            project_stage_ids=project_stage_ids,
        )

    @staticmethod
    def _reader_import_filename(*, pdf_url: str, title: str, disposition: str) -> str:
        match = re.search(r'filename="?([^";]+)"?', disposition, flags=re.IGNORECASE)
        if match:
            candidate = unquote(match.group(1).strip())
            if candidate:
                return candidate
        stem = Path(urlparse(pdf_url).path).name
        if stem:
            return stem
        safe_title = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-._")
        return f"{safe_title or 'open-access-paper'}.pdf"

    def reader_import_projects(self, import_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.reader.set_project_links(import_id, self._reader_projects(payload.get("project_stage_ids")))

    def reparse_reader_import(self, import_id: str) -> dict[str, Any]:
        imported = self.reader.get_import(import_id)
        return self.reader.import_pdf(str(imported["filename"]), self.reader.pdf_path(import_id).read_bytes(), force_reparse=True)

    def stage_paper_pdf(self, paper_id: str, *, project_stage_ids: list[str] | None = None) -> dict[str, Any]:
        filename, payload, metadata = self.paper_pdf(paper_id)
        paper = self.queries.get_paper(paper_id)
        external_ids = paper.get("external_ids") if isinstance(paper.get("external_ids"), Mapping) else {}
        return self.reader.import_pdf(
            filename,
            payload,
            project_stage_ids=self._reader_projects(project_stage_ids),
            origin={
                "source": str(metadata.get("source") or "paper"),
                "paper_id": paper_id,
                "zotero_key": str(external_ids.get("zotero") or ""),
            },
        )

    def propose_reader_import_paper(
        self,
        import_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        imported = self.reader.get_import(import_id)
        bibliography = imported.get("bibliography") if isinstance(imported.get("bibliography"), Mapping) else {}
        origin_id = str((imported.get("origin") or {}).get("paper_id") or "")
        requested_id = str(payload.get("paper_id") or "").strip()
        doi = str(payload.get("doi") or (imported.get("origin") or {}).get("doi") or "").strip()
        existing = self.store.get_paper(requested_id or origin_id) if requested_id or origin_id else None
        if existing is None:
            matches = [paper for paper in self.store.list_papers() if paper.external_ids.get("local_pdf") == import_id or (doi and str(paper.external_ids.get("doi") or "").casefold() == doi.casefold())]
            if len(matches) > 1:
                raise DomainQueryError("ambiguous_paper", "multiple papers match this document; choose a paper_id")
            existing = matches[0] if matches else None
        if existing is not None:
            changes = {"external_ids": {**existing.external_ids, "local_pdf": import_id}}
            title = payload.get("title") or bibliography.get("title") or ""
            authors = payload.get("authors") or bibliography.get("authors") or []
            year = payload.get("year") if payload.get("year") is not None else bibliography.get("year")
            # Linking a document must not replace curated Zotero metadata. Only
            # fill genuinely absent fields from the retained source bibliography.
            if not existing.title and title:
                changes["title"] = title
            if not existing.authors and isinstance(authors, list) and authors:
                changes["authors"] = authors
            if existing.year is None and isinstance(year, int) and not isinstance(year, bool) and 1000 <= year <= 2100:
                changes["year"] = year
            if doi:
                changes["external_ids"]["doi"] = doi
            try:
                return self.research.propose_paper_intake(existing.paper_id, changes)
            except MutationError as exc:
                if exc.code != "no_changes":
                    raise
                return {"status": "already_linked", "paper_id": existing.paper_id, "paper": existing.to_dict(), "document_ref": f"local_pdf:{import_id}"}
        requested_authors = payload.get("authors") or bibliography.get("authors")
        authors = (
            requested_authors
            if isinstance(requested_authors, list)
            else imported.get("authors") or []
        )
        external_ids = {"local_pdf": import_id}
        doi = str(payload.get("doi") or (imported.get("origin") or {}).get("doi") or "").strip()
        if doi:
            external_ids["doi"] = doi
        changes: dict[str, Any] = {
            "title": str(payload.get("title") or bibliography.get("title") or imported.get("title") or imported.get("filename") or "PDF"),
            "authors": authors,
            "source": "local_pdf",
            "external_ids": external_ids,
            "status": "to_read",
        }
        year = payload.get("year") if payload.get("year") is not None else bibliography.get("year")
        if isinstance(year, int) and not isinstance(year, bool) and 1000 <= year <= 2100:
            changes["year"] = year
        paper_id = str(payload.get("paper_id") or f"paper-local-{import_id.removeprefix('pdf_')}").strip()
        return self.research.propose_paper_intake(paper_id, changes)

    def reader_import_pdf(self, import_id: str) -> tuple[str, bytes, dict[str, Any]]:
        imported = self.reader.get_import(import_id)
        target = self.reader.pdf_path(import_id)
        return (
            str(imported.get("filename") or f"{import_id}.pdf"),
            target.read_bytes(),
            {"source": "local_pdf", "size_bytes": target.stat().st_size},
        )

    def reader_import_reader(
        self,
        import_id: str,
        *,
        offset: int = 0,
        limit: int = 250,
    ) -> dict[str, Any]:
        imported = self.reader.get_import(import_id)
        document = self.reader.structured_document(import_id)
        pdf = {
            "available": True,
            "source": "local_pdf",
            "size_bytes": int(imported.get("size_bytes") or 0),
        }
        if not isinstance(document, Mapping) or not isinstance(document.get("blocks"), list):
            return {
                "schema_version": 1,
                "read_only": True,
                "available": False,
                "paper": {"paper_id": import_id, "title": imported.get("title")},
                "reason": str(imported.get("parse_error") or "structured_pdf_not_indexed"),
                "sources": [],
                "pdf": pdf,
            }
        return self._structured_reader_response(
            document,
            paper={"paper_id": import_id, "title": imported.get("title")},
            source={
                "source_ref": f"local-pdf:{import_id}:document.pdf",
                "source_type": "paper",
                "title": imported.get("title"),
                "scope": f"local-pdf:{import_id}",
                "source_version": imported.get("content_hash"),
                "status": "staged",
            },
            sources=[],
            pdf=pdf,
            offset=offset,
            limit=limit,
        )

    def reader_annotations(self, target_ref: str) -> dict[str, Any]:
        return self.reader.list_annotations(target_ref)

    def create_reader_annotation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.reader.create_annotation(payload)

    def delete_reader_annotation(self, annotation_id: str) -> dict[str, Any]:
        return self.reader.delete_annotation(annotation_id)

    def reader_translate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        source = str(payload.get("source") or "").strip()
        return self.reader_translations.create(source)

    def _matched_paper_sources(self, paper: Mapping[str, Any]) -> list[dict[str, Any]]:
        manifest = KnowledgeIngestionManifest(self.store.root_dir)
        records = manifest.read()["sources"]
        zotero_key = str((paper.get("external_ids") or {}).get("zotero") or "")
        matched: list[dict[str, Any]] = []
        for source_ref, raw_record in records.items():
            if not isinstance(raw_record, Mapping):
                continue
            metadata = raw_record.get("metadata")
            metadata_map = metadata if isinstance(metadata, Mapping) else {}
            belongs = (
                str(metadata_map.get("paper_id") or "") == str(paper["paper_id"])
                or str(paper["paper_id"]) in (metadata_map.get("paper_ids") or [])
                or (str((paper.get("external_ids") or {}).get("local_pdf") or "") and str(metadata_map.get("document_ref") or "") == f"local_pdf:{paper['external_ids']['local_pdf']}")
                or (zotero_key and str(metadata_map.get("item_key") or "") == zotero_key)
                or (zotero_key and str(source_ref) == f"zotero:{zotero_key}")
            )
            if belongs:
                matched.append({**dict(raw_record), "source_ref": str(source_ref)})
        return matched

    def _local_zotero_client(self) -> Any:
        client = self._configured_zotero_client or build_zotero_client(mode="local")
        if not isinstance(client, ZoteroLocalClient) and str(getattr(client, "backend", "")) != "local":
            raise DomainQueryError(
                "paper_pdf_zotero_local_required",
                "open Zotero Desktop and enable its local API to read this PDF",
            )
        if not callable(getattr(client, "list_children", None)) or not callable(
            getattr(client, "attachment_file_url", None)
        ):
            raise DomainQueryError(
                "paper_pdf_zotero_local_required",
                "the selected Zotero backend cannot expose local PDF attachments",
            )
        return client

    def _resolve_paper_pdf(self, paper_id: str) -> tuple[Path, dict[str, Any]]:
        paper = self.queries.get_paper(paper_id)
        local_import = str((paper.get("external_ids") or {}).get("local_pdf") or "").strip()
        if local_import:
            return self.reader.pdf_path(local_import), paper
        zotero_key = str((paper.get("external_ids") or {}).get("zotero") or "").strip()
        matched = self._matched_paper_sources(paper)
        attachment_key = ""
        for record in matched:
            metadata = record.get("metadata")
            metadata_map = metadata if isinstance(metadata, Mapping) else {}
            candidate = str(metadata_map.get("attachment_key") or "").strip()
            if candidate:
                attachment_key = candidate
                break
        if not attachment_key:
            if not zotero_key:
                raise DomainQueryError(
                    "paper_pdf_not_linked",
                    "this paper is not linked to a Zotero item",
                )
        client = self._local_zotero_client()
        if not attachment_key:
            try:
                children = client.list_children(zotero_key)
            except ZoteroClientError as exc:
                raise DomainQueryError(
                    "paper_pdf_zotero_unavailable",
                    "Zotero could not list this paper's attachments",
                ) from exc
            if children.status != "ok" or not isinstance(children.data, list):
                raise DomainQueryError(
                    "paper_pdf_zotero_unavailable",
                    "Zotero could not list this paper's attachments",
                )
            for child in children.data:
                if not isinstance(child, Mapping):
                    continue
                data = child.get("data")
                child_data = data if isinstance(data, Mapping) else child
                if (
                    str(child_data.get("itemType") or "").casefold() == "attachment"
                    and str(child_data.get("contentType") or "").casefold() == "application/pdf"
                ):
                    attachment_key = str(child.get("key") or child_data.get("key") or "").strip()
                    if attachment_key:
                        break
        if not re.fullmatch(r"[A-Za-z0-9]{1,64}", attachment_key):
            raise DomainQueryError(
                "paper_pdf_not_found",
                "this Zotero item has no readable PDF attachment",
            )
        try:
            resolved = client.attachment_file_url(attachment_key)
        except ZoteroClientError as exc:
            raise DomainQueryError(
                "paper_pdf_zotero_unavailable",
                "Zotero could not resolve this PDF attachment",
            ) from exc
        if resolved.status != "ok" or not isinstance(resolved.data, str):
            raise DomainQueryError(
                "paper_pdf_zotero_unavailable",
                "Zotero could not resolve this PDF attachment",
            )
        parsed = urlparse(resolved.data.strip())
        if (
            parsed.scheme.casefold() != "file"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or parsed.netloc.casefold() not in {"", "localhost"}
        ):
            raise DomainQueryError(
                "paper_pdf_invalid_source",
                "Zotero returned an unsupported attachment location",
            )
        raw_path = unquote(parsed.path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", raw_path):
            raw_path = raw_path[1:]
        target = Path(raw_path).resolve()
        try:
            stat = target.stat()
        except OSError as exc:
            raise DomainQueryError(
                "paper_pdf_not_found",
                "the Zotero PDF attachment is not available on disk",
            ) from exc
        if not target.is_file() or target.suffix.casefold() != ".pdf":
            raise DomainQueryError(
                "paper_pdf_invalid_source",
                "the Zotero attachment is not a readable PDF",
            )
        if stat.st_size <= 0 or stat.st_size > 80_000_000:
            raise DomainQueryError(
                "paper_pdf_too_large",
                "the Zotero PDF exceeds the 80 MB reader limit",
            )
        return target, paper

    def paper_pdf_status(self, paper_id: str) -> dict[str, Any]:
        try:
            target, paper = self._resolve_paper_pdf(paper_id)
        except DomainQueryError as exc:
            return {"available": False, "reason": exc.code, "source": "zotero_local"}
        source = "local_pdf" if (paper.get("external_ids") or {}).get("local_pdf") else "zotero_local"
        return {
            "available": True,
            "source": source,
            "size_bytes": target.stat().st_size,
        }

    def paper_pdf(self, paper_id: str) -> tuple[str, bytes, dict[str, Any]]:
        target, paper = self._resolve_paper_pdf(paper_id)
        try:
            payload = target.read_bytes()
        except OSError as exc:
            raise DomainQueryError(
                "paper_pdf_unreadable",
                "the Zotero PDF attachment could not be read",
            ) from exc
        return (
            f"{str(paper['paper_id'])}.pdf",
            payload,
            {
                "source": (
                    "local_pdf"
                    if (paper.get("external_ids") or {}).get("local_pdf")
                    else "zotero_local"
                ),
                "size_bytes": len(payload),
            },
        )

    def paper_reader(
        self,
        paper_id: str,
        *,
        offset: int = 0,
        limit: int = 250,
    ) -> dict[str, Any]:
        """Return a sanitized structured-PDF snapshot linked to one Paper."""

        paper = self.queries.get_paper(paper_id)
        local_import = str((paper.get("external_ids") or {}).get("local_pdf") or "").strip()
        if local_import:
            response = self.reader_import_reader(local_import, offset=offset, limit=limit)
            return {**response, "paper": paper}
        matched = self._matched_paper_sources(paper)

        sources = [_sanitized_reader_source(item) for item in matched]
        pdf = self.paper_pdf_status(paper_id)
        structured = [
            item for item in matched
            if str(item.get("structured_document_path") or "").strip()
        ]
        structured.sort(
            key=lambda item: (
                str(item.get("status") or "") == "processed",
                str(item.get("source_version") or ""),
            ),
            reverse=True,
        )
        if not structured:
            return {
                "schema_version": 1,
                "read_only": True,
                "available": False,
                "paper": paper,
                "reason": (
                    "structured_pdf_not_indexed"
                    if matched
                    else "paper_source_not_linked"
                ),
                "sources": sources,
                "pdf": pdf,
            }

        record = structured[0]
        relative = str(record["structured_document_path"])
        knowledge_root = (self.store.root_dir / "_knowledge").resolve()
        document_root = (knowledge_root / "scientific-documents").resolve()
        target = (knowledge_root / relative).resolve()
        try:
            target.relative_to(document_root)
        except ValueError as exc:
            raise DomainQueryError(
                "paper_reader_snapshot_invalid",
                "structured paper snapshot escaped the governed catalog",
            ) from exc
        try:
            if target.stat().st_size > 25_000_000:
                raise DomainQueryError(
                    "paper_reader_snapshot_too_large",
                    "structured paper snapshot exceeds the reader limit",
                )
            document = json.loads(target.read_text(encoding="utf-8"))
        except DomainQueryError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise DomainQueryError(
                "paper_reader_snapshot_invalid",
                "structured paper snapshot is unreadable",
            ) from exc
        if not isinstance(document, Mapping) or not isinstance(document.get("blocks"), list):
            raise DomainQueryError(
                "paper_reader_snapshot_invalid",
                "structured paper snapshot has an unsupported schema",
            )

        return self._structured_reader_response(
            document,
            paper=paper,
            source=_sanitized_reader_source(record),
            sources=sources,
            pdf=pdf,
            offset=offset,
            limit=limit,
        )

    def _structured_reader_response(
        self,
        document: Mapping[str, Any],
        *,
        paper: Mapping[str, Any],
        source: Mapping[str, Any],
        sources: list[dict[str, Any]],
        pdf: Mapping[str, Any],
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        blocks = [item for item in document["blocks"] if isinstance(item, Mapping)]
        bounded_offset = max(0, int(offset))
        bounded_limit = max(1, min(int(limit), 500))
        selected_blocks = blocks[bounded_offset:bounded_offset + bounded_limit]
        references = document.get("references")
        citations = document.get("citations")
        return {
            "schema_version": 1,
            "read_only": True,
            "available": True,
            "paper": dict(paper),
            "source": dict(source),
            "sources": sources,
            "pdf": pdf,
            "document": {
                "schema_version": document.get("schema_version"),
                "document_id": document.get("document_id"),
                "title": document.get("title"),
                "authors": document.get("authors") if isinstance(document.get("authors"), list) else [],
                "content_hash": document.get("content_hash"),
                "page_count": document.get("page_count"),
                "parser": document.get("parser") if isinstance(document.get("parser"), Mapping) else {},
                "metadata": _sanitized_document_metadata(document.get("metadata")),
                "warnings": document.get("warnings") if isinstance(document.get("warnings"), list) else [],
                "block_count": len(blocks),
                "blocks": [dict(item) for item in selected_blocks],
                "references": [dict(item) for item in (references or [])[:500] if isinstance(item, Mapping)],
                "citations": [dict(item) for item in (citations or [])[:2000] if isinstance(item, Mapping)],
                "toc": _scientific_document_toc(blocks),
                "offset": bounded_offset,
                "limit": bounded_limit,
                "has_more": bounded_offset + len(selected_blocks) < len(blocks),
            },
        }

    def propose_manuscript_create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        sections = payload.get("sections") or []
        evidence_refs = payload.get("evidence_refs") or []
        if not isinstance(sections, list) or not isinstance(evidence_refs, list):
            raise MutationError("invalid_argument", "sections and evidence_refs must be JSON arrays")
        return self.manuscripts.propose_create(
            str(payload.get("manuscript_id") or ""),
            project_stage_id=str(payload.get("project_stage_id") or ""),
            title=str(payload.get("title") or ""),
            target_journal=str(payload.get("target_journal") or "generic"),
            language=str(payload.get("language") or "en"),
            main_file=str(payload.get("main_file") or "main.tex"),
            sections=[str(item) for item in sections] or None,
            evidence_refs=[str(item) for item in evidence_refs],
            request_id="workbench",
        )

    def propose_manuscript_patch(
        self,
        manuscript_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        evidence_refs = payload.get("evidence_refs") or []
        if not isinstance(evidence_refs, list):
            raise MutationError("invalid_argument", "evidence_refs must be a JSON array")
        return self.manuscripts.propose_patch(
            manuscript_id,
            section=str(payload.get("section") or ""),
            replacement_latex=str(payload.get("replacement_latex") or ""),
            evidence_refs=[str(item) for item in evidence_refs],
            base_file_hash=str(payload.get("base_file_hash") or ""),
            request_id="workbench",
        )

    def global_search(self, query: str, *, limit: int = 30) -> dict[str, Any]:
        """Search typed research truth without involving the model or KB index."""

        normalized = str(query or "").strip()
        if len(normalized) < 2:
            raise DomainQueryError(
                "invalid_argument",
                "search query must contain at least 2 characters",
            )
        bounded = max(1, min(int(limit), 50))
        per_type = min(bounded, 20)
        groups = (
            ("projects", "project_stage", "stage_id", "project_name", self.queries.search_project_stages(query=normalized, limit=per_type)),
            ("tasks", "task", "task_id", "title", self.queries.search_tasks(query=normalized, limit=per_type)),
            ("papers", "paper", "paper_id", "title", self.queries.search_papers(query=normalized, limit=per_type)),
            ("experiments", "experiment", "experiment_id", "title", self.queries.search_experiments(query=normalized, limit=per_type)),
            ("artifacts", "research_artifact", "artifact_id", "title", self.queries.search_research_artifacts(query=normalized, limit=per_type)),
        )
        items: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for resource, object_type, id_field, title_field, result in groups:
            counts[resource] = int(result.get("total_matches") or 0)
            for record in result["items"]:
                object_id = str(record[id_field])
                items.append({
                    "resource": resource,
                    "object_type": object_type,
                    "object_id": object_id,
                    "ref": object_id if object_type == "project_stage" else f"{object_type}:{object_id}",
                    "title": str(record.get(title_field) or object_id),
                    "status": record.get("status") or record.get("kind"),
                    "updated_at": record.get("updated_at"),
                })
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        selected = items[:bounded]
        return {
            "query": normalized,
            "count": len(selected),
            "total_matches": sum(counts.values()),
            "counts": counts,
            "items": selected,
        }

    def object_evidence(self, object_ref: str, *, limit: int = 50) -> dict[str, Any]:
        """Return the confirmed, bidirectional evidence graph around one object."""

        normalized_ref = str(object_ref or "").strip()
        if not normalized_ref or ":" not in normalized_ref:
            raise DomainQueryError(
                "invalid_argument",
                "ref must be a typed ScholarFlow object reference",
                details={"field": "ref"},
            )
        bounded = max(1, min(int(limit), 100))
        outgoing = self.queries.search_knowledge_relations(
            subject_ref=normalized_ref,
            limit=bounded,
        )["items"]
        incoming = self.queries.search_knowledge_relations(
            object_ref=normalized_ref,
            limit=bounded,
        )["items"]
        relations: list[dict[str, Any]] = []
        seen: set[str] = set()
        evidence_refs: set[str] = set()
        predicate_counts: dict[str, int] = {}
        for direction, items in (("outgoing", outgoing), ("incoming", incoming)):
            for item in items:
                relation_id = str(item["relation_id"])
                if relation_id in seen:
                    continue
                seen.add(relation_id)
                predicate = str(item.get("predicate") or "related_to")
                predicate_counts[predicate] = predicate_counts.get(predicate, 0) + 1
                refs = [str(ref) for ref in item.get("evidence_refs") or [] if str(ref).strip()]
                evidence_refs.update(refs)
                relations.append({
                    **item,
                    "direction": direction,
                    "counterpart_ref": (
                        item.get("object_ref") if direction == "outgoing" else item.get("subject_ref")
                    ),
                    "evidence_refs": refs,
                })
        relations.sort(
            key=lambda item: (str(item.get("updated_at") or ""), str(item["relation_id"])),
            reverse=True,
        )
        selected = relations[:bounded]
        selected_refs = {
            ref
            for item in selected
            for ref in item.get("evidence_refs") or []
        }
        return {
            "object_ref": normalized_ref,
            "read_only": True,
            "count": len(selected),
            "total_matches": len(relations),
            "predicate_counts": predicate_counts,
            "evidence_refs": sorted(selected_refs),
            "items": selected,
        }

    def knowledge_health(self) -> dict[str, Any]:
        """Report the current runtime, separately from historical acceptance."""

        health = build_knowledge_engine(self.store.root_dir).health()
        sources = summarize_ingestion_manifest(
            KnowledgeIngestionManifest(self.store.root_dir),
            limit=1,
        )
        return {
            "health": health,
            "source_count": sources["count"],
            "by_scope": sources["by_scope"],
            "by_type": sources["by_type"],
            "read_only": True,
        }

    def knowledge_sources(
        self,
        *,
        scope: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return {
            **summarize_ingestion_manifest(
                KnowledgeIngestionManifest(self.store.root_dir),
                scope=str(scope or "").strip() or None,
                limit=limit,
            ),
            "read_only": True,
        }

    def knowledge_search(
        self,
        query: str,
        *,
        mode: str = "auto",
        limit: int = 5,
        scope: str | None = None,
        project_stage_id: str | None = None,
    ) -> dict[str, Any]:
        return prepare_knowledge_bundle(
            build_knowledge_engine(self.store.root_dir),
            query=query,
            mode=mode,
            limit=limit,
            scope=scope,
            project_stage_id=project_stage_id,
        ).to_dict()

    def knowledge_sync_options(self) -> dict[str, Any]:
        return self.knowledge_sync.options()

    def create_knowledge_sync_plan(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.knowledge_sync.create_plan(
            source=str(payload.get("source") or ""),
            collection_key=str(payload.get("collection_key") or "").strip() or None,
            import_id=str(payload.get("import_id") or "").strip() or None,
            detect_missing=_payload_bool(payload, "detect_missing", default=True),
        )

    def apply_knowledge_sync_plan(
        self,
        plan_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        apply = self.knowledge_sync.apply_plan if _payload_bool(payload, "wait", default=True) else self.knowledge_sync.submit_plan
        return apply(
            plan_id,
            allow_deletes=_payload_bool(payload, "allow_deletes", default=False),
        )

    def proposals(self) -> dict[str, Any]:
        state = load_proposal_state(self.store.root_dir)
        items = []
        for proposal_id, proposal in state["proposals"].items():
            if (
                proposal_id in state["commits"]
                or proposal_id in state["rejections"]
                or _proposal_expired(proposal)
            ):
                continue
            view = _proposal_view(proposal, lane="domain")
            view["review"] = self._proposal_review(view)
            items.append(view)
        for proposal in self.candidates.list_review_proposals()["items"]:
            if _proposal_expired(proposal):
                continue
            view = _proposal_view(proposal, lane="candidate")
            view["review"] = self._proposal_review(view)
            items.append(view)
        items.sort(
            key=lambda item: (item["created_at"], item["proposal_id"]),
            reverse=True,
        )
        return {"count": len(items), "items": items}

    def _proposal_review(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        """Build a bounded, read-only review model from authoritative state."""

        diff = dict(proposal.get("diff") or {})
        changed_fields = sorted(str(field) for field in diff)
        before_values = [
            dict(change or {}).get("before")
            for change in diff.values()
            if isinstance(change, Mapping)
        ]
        mode = "create" if before_values and all(value is None for value in before_values) else "update"
        title_change = dict(diff.get("title") or {})
        kind_change = dict(diff.get("kind") or {})
        evidence_change = dict(diff.get("evidence_refs") or {})
        evidence_refs = _string_list(evidence_change.get("after"), limit=100)
        project_change = dict(diff.get("project_stage_id") or {})
        project_stage_id = str(project_change.get("after") or "").strip()
        if project_stage_id:
            evidence_refs.append(f"project_stage:{project_stage_id}")
        for field, prefix in (
            ("related_papers", "paper"),
            ("related_experiments", "experiment"),
            ("related_tasks", "task"),
        ):
            relation_change = dict(diff.get(field) or {})
            evidence_refs.extend(
                f"{prefix}:{object_id}"
                for object_id in _string_list(relation_change.get("after"), limit=100)
            )
        evidence_refs = list(dict.fromkeys(evidence_refs))[:100]
        evidence = [self._resolve_proposal_evidence(ref) for ref in evidence_refs]
        unresolved_count = sum(not item["resolved"] for item in evidence)
        return {
            "title": str(title_change.get("after") or proposal.get("operation") or "Proposal"),
            "kind": str(kind_change.get("after") or ""),
            "mode": mode,
            "changed_fields": changed_fields,
            "change_count": len(changed_fields),
            "evidence": evidence,
            "evidence_count": len(evidence),
            "unresolved_evidence_count": unresolved_count,
            "requires_confirmation": bool(
                dict(proposal.get("policy") or {}).get(
                    "requires_confirmation",
                    True,
                )
            ),
            "side_effect_class": str(
                dict(proposal.get("policy") or {}).get(
                    "side_effect_class"
                )
                or "unknown"
            ),
            "read_only": True,
        }

    def _resolve_proposal_evidence(self, ref: str) -> dict[str, Any]:
        normalized = str(ref or "").strip()
        object_type, separator, object_id = normalized.partition(":")
        resolvers = {
            "project_stage": ("projects", self.queries.get_project_stage, "project_name"),
            "task": ("tasks", self.queries.get_task, "title"),
            "paper": ("papers", self.queries.get_paper, "title"),
            "experiment": ("experiments", self.queries.get_experiment, "title"),
            "research_artifact": ("artifacts", self.queries.get_research_artifact, "title"),
        }
        resolver = resolvers.get(object_type) if separator and object_id else None
        if resolver is None:
            return {
                "ref": normalized,
                "object_type": object_type or "unknown",
                "object_id": object_id,
                "resource": None,
                "label": normalized,
                "status": None,
                "resolved": False,
            }
        resource, query, title_field = resolver
        try:
            item = query(object_id)
        except DomainQueryError:
            return {
                "ref": normalized,
                "object_type": object_type,
                "object_id": object_id,
                "resource": resource,
                "label": normalized,
                "status": None,
                "resolved": False,
            }
        return {
            "ref": normalized,
            "object_type": object_type,
            "object_id": object_id,
            "resource": resource,
            "label": str(item.get(title_field) or object_id),
            "status": item.get("status"),
            "resolved": True,
        }

    def proposal_history(self, *, limit: int = 50) -> dict[str, Any]:
        normalized_limit = min(200, max(1, int(limit)))
        state = load_proposal_state(self.store.root_dir)
        items = [
            _applied_receipt(commit, lane="domain")
            for commit in state["commits"].values()
        ]
        items.extend(
            _rejection_receipt(receipt, lane="domain")
            for receipt in state["rejections"].values()
        )
        candidate_history = self.candidates.review_proposal_history()["items"]
        # Expiry is derived, not a user decision: never mutate state on a read.
        expired = [
            (proposal, "domain")
            for proposal_id, proposal in state["proposals"].items()
            if proposal_id not in state["commits"]
            and proposal_id not in state["rejections"]
            and _proposal_expired(proposal)
        ]
        expired.extend(
            (proposal, "candidate")
            for proposal in self.candidates.list_review_proposals()["items"]
            if _proposal_expired(proposal)
        )
        for proposal, lane in expired:
            items.append({
                **_proposal_view(proposal, lane=lane),
                "status": "expired",
                "decided_at": proposal.get("expires_at"),
                "reason": "Proposal expired without being applied; original draft retained.",
            })
        for receipt in candidate_history:
            if receipt.get("status") == "rejected":
                items.append(_rejection_receipt(receipt, lane="candidate"))
            else:
                items.append(_applied_receipt(receipt, lane="candidate"))
        items.sort(
            key=lambda item: (
                str(item.get("decided_at") or ""),
                str(item.get("proposal_id") or ""),
            ),
            reverse=True,
        )
        return {
            "count": len(items),
            "items": items[:normalized_limit],
        }

    def proposal_detail(self, proposal_id: str) -> dict[str, Any]:
        normalized = str(proposal_id or "").strip()
        for item in self.proposals()["items"]:
            if item["proposal_id"] == normalized:
                return item
        for item in self.proposal_history(limit=200)["items"]:
            if item["proposal_id"] == normalized:
                return item
        raise MutationError(
            "not_found",
            f"proposal '{normalized}' was not found",
        )

    def audit(self, *, limit: int = 100) -> dict[str, Any]:
        normalized_limit = min(500, max(1, int(limit)))
        payload = list_events(self.store.root_dir, limit=normalized_limit)
        items = list(reversed(payload["items"]))
        return {
            "count": len(items),
            "items": items,
        }

    def automation(self) -> dict[str, Any]:
        """Return bounded scheduler and Pi inbox state for the local UI."""

        jobs = list_automation_jobs(self.store.root_dir)
        inbox = list_gateway_inbox(
            self.store.root_dir,
            status="",
            limit=100,
        )
        pending = [
            _automation_message_view(item)
            for item in inbox["items"]
            if item.get("status") == "pending"
        ]
        recent = [
            _automation_message_view(item)
            for item in reversed(inbox["items"])
            if item.get("status") != "pending"
        ][:20]
        job_items = [_automation_job_view(item) for item in jobs["items"]]
        return {
            "generated_at": _now(),
            "summary": {
                "job_count": len(job_items),
                "active_job_count": sum(
                    str(item.get("status") or "active") == "active"
                    for item in job_items
                ),
                "pending_count": len(pending),
                "failed_job_count": sum(
                    str(item.get("last_status") or "") == "failed"
                    for item in job_items
                ),
            },
            "jobs": job_items,
            "pending": pending,
            "recent": recent,
            "gateway": gateway_status(self.store.root_dir),
            "workflow_options": [
                {
                    "workflow_type": workflow_type,
                    "label": spec["label"],
                    "skill_name": spec["skill_name"],
                }
                for workflow_type, spec in AUTOMATION_WORKFLOWS.items()
            ],
            "read_only": True,
        }

    def queue_automation_workflow(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        workflow_type = str(payload.get("workflow_type") or "").strip()
        spec = AUTOMATION_WORKFLOWS.get(workflow_type)
        if spec is None:
            raise MutationError(
                "invalid_argument",
                "workflow_type must be daily_review, weekly_review, or experiment_watch",
            )
        pending = list_gateway_inbox(
            self.store.root_dir,
            status="pending",
            limit=100,
        )
        existing = next(
            (
                item
                for item in reversed(pending["items"])
                if item.get("workflow_type") == workflow_type
                and item.get("source") == "workbench"
            ),
            None,
        )
        if existing is not None:
            return {
                **_automation_message_view(existing),
                "created": False,
            }
        item = enqueue_workflow_request(
            self.store.root_dir,
            skill_name=spec["skill_name"],
            payload={
                "workflow_type": workflow_type,
                "instruction": spec["instruction"],
            },
            source="workbench",
        )
        return {
            **_automation_message_view(item),
            "created": True,
        }

    def acknowledge_automation_message(
        self,
        message_id: str,
        payload: Mapping[str, Any],
        *,
        session_id: str,
    ) -> dict[str, Any]:
        outcome = str(payload.get("outcome") or "").strip().casefold()
        if outcome not in {"processed", "dismissed", "failed"}:
            raise MutationError(
                "invalid_argument",
                "outcome must be processed, dismissed, or failed",
            )
        note = str(payload.get("note") or "").strip()
        if len(note) > 1000:
            raise MutationError(
                "invalid_argument",
                "note exceeds the 1000 character limit",
            )
        try:
            item = acknowledge_gateway_message(
                self.store.root_dir,
                message_id=str(message_id or "").strip(),
                outcome=outcome,
                note=note or None,
                confirmed=True,
                confirmed_by=f"workbench:{session_id[:12]}",
            )
        except ValueError as exc:
            code = "not_found" if "was not found" in str(exc) else "invalid_argument"
            raise MutationError(code, str(exc)) from exc
        return _automation_message_view(item)

    def integrations(self) -> dict[str, Any]:
        zotero_client = build_zotero_client()
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
        zotero_capabilities = (
            ["read", "import"]
            if zotero_backend == "local"
            else ["read", "import", "propose_write", "apply_write"]
            if zotero_backend in {"web", "mcp"}
            else []
        )
        zotero["available"] = zotero_available
        zotero["collection_count"] = (
            len(zotero_probe.data)
            if zotero_available and isinstance(zotero_probe.data, list)
            else 0
        )
        vault_path = self.vault_root
        repository_root = Path(__file__).resolve().parents[1]
        knowledge_acceptance = audit_knowledge_engine_manifest(repository_root)
        try:
            knowledge_runtime = self.knowledge_health()
        except DomainQueryError as exc:
            knowledge_runtime = {
                "health": {
                    "status": "unavailable",
                    "available": False,
                    "error": {"code": exc.code, "message": str(exc)},
                },
                "source_count": 0,
                "read_only": True,
            }
        live_health = knowledge_runtime["health"]
        live_available = bool(live_health.get("available"))
        live_status = str(live_health.get("status") or "unknown")
        runtime = runtime_health_summary(self.store.root_dir, recent_limit=20)
        runtime_policy = _read_optional_json(
            self.repository_root
            / "packages"
            / "scholarflow-pi"
            / "compatibility"
            / "runtime-policy.json"
        )
        tested_pi_version = dict(runtime_policy.get("pi") or {}).get(
            "tested_version"
        )
        return {
            "items": [
                {
                    "integration_id": "zotero",
                    "name": "Zotero",
                    "status": (
                        "available"
                        if zotero_available
                        else "unavailable"
                        if zotero_backend != "null"
                        else "not_configured"
                    ),
                    "capabilities": zotero_capabilities,
                    "details": zotero,
                },
                {
                    "integration_id": "obsidian",
                    "name": "Obsidian",
                    "status": (
                        "configured"
                        if vault_path is not None and vault_path.exists()
                        else "not_configured"
                    ),
                    "capabilities": ["propose_write", "apply_write"],
                    "details": {
                        "vault_configured": vault_path is not None,
                        "vault_exists": (
                            vault_path.exists()
                            if vault_path is not None
                            else False
                        ),
                    },
                },
                {
                    "integration_id": "knowledge-engine",
                    "name": "AI Knowledge Engine",
                    "status": (
                        "available"
                        if live_available
                        else "not_configured"
                        if live_status == "disabled"
                        else "unavailable"
                    ),
                    "capabilities": (
                        ["health", "sources", "search", "import"]
                        if live_available
                        else ["health", "sources"]
                    ),
                    "details": {
                        "runtime": knowledge_runtime,
                        "acceptance": knowledge_acceptance,
                    },
                },
                {
                    "integration_id": "pi-runtime",
                    "name": "Pi Runtime",
                    "status": str(runtime.get("status") or "unknown"),
                    "capabilities": ["health", "read"],
                    "details": {
                        **runtime,
                        "tested_version": tested_pi_version,
                    },
                },
            ]
        }

    def test_integration(self, integration_id: str) -> dict[str, Any]:
        """Run one bounded, read-only integration diagnostic."""

        normalized = str(integration_id or "").strip().casefold()
        if normalized == "zotero":
            client = build_zotero_client()
            description = describe_zotero_client(client)
            backend = str(description.get("backend") or "null")
            configured = backend != "null"
            probe = client.list_collections() if configured else None
            reachable = bool(probe is not None and probe.status == "ok")
            collection_count = (
                len(probe.data)
                if reachable and isinstance(probe.data, list)
                else 0
            )
            checks = [
                _integration_check("configured", "已选择 Zotero 后端", configured, backend),
                _integration_check(
                    "reachable",
                    "本地或远程接口可读取",
                    reachable,
                    f"{collection_count} 个 collection" if reachable else "接口不可达",
                ),
            ]
            return _integration_test_result(
                normalized,
                checks,
                summary=(
                    f"Zotero {backend} 已连接，可读取 {collection_count} 个 collection。"
                    if reachable
                    else "Zotero 尚未形成可用的只读连接。"
                ),
                next_actions=(
                    []
                    if reachable
                    else ["启动 Zotero Desktop，并确认本地 API 已启用。"]
                    if backend == "local"
                    else ["配置一个受支持的 Zotero local、Web 或 MCP 后端。"]
                ),
            )
        if normalized == "obsidian":
            vault = self.vault_root
            configured = vault is not None
            exists = bool(vault is not None and vault.is_dir())
            readable = bool(exists and os.access(vault, os.R_OK))
            writable = bool(exists and os.access(vault, os.W_OK))
            checks = [
                _integration_check("configured", "Vault 根目录已配置", configured),
                _integration_check("exists", "Vault 目录存在", exists),
                _integration_check("readable", "批准范围可读取", readable),
                _integration_check("writable", "Proposal 确认后可写入", writable),
            ]
            return _integration_test_result(
                normalized,
                checks,
                summary=(
                    f"Obsidian vault “{vault.name}” 已就绪。"
                    if readable and writable and vault is not None
                    else "Obsidian vault 配置不完整或当前不可访问。"
                ),
                next_actions=(
                    []
                    if readable and writable
                    else ["检查 SCHOLARFLOW_VAULT_DIR 与目录权限。"]
                ),
            )
        if normalized == "knowledge-engine":
            try:
                knowledge = self.knowledge_health()
                health = dict(knowledge.get("health") or {})
                catalog_ok = True
            except DomainQueryError as exc:
                knowledge = {"source_count": 0}
                health = {"status": "unavailable", "available": False}
                catalog_ok = False
                error_detail = str(exc)
            else:
                error_detail = "来源目录可读取"
            runtime_status = str(health.get("status") or "unknown")
            available = bool(health.get("available"))
            checks = [
                _integration_check(
                    "catalog",
                    "来源目录可读取",
                    catalog_ok,
                    error_detail,
                ),
                _integration_check(
                    "runtime",
                    "LightRAG 当前可检索",
                    available,
                    runtime_status,
                    severity="attention" if runtime_status == "disabled" else "error",
                ),
            ]
            return _integration_test_result(
                normalized,
                checks,
                summary=(
                    f"AI 知识引擎在线，登记来源 {int(knowledge.get('source_count') or 0)} 项。"
                    if available
                    else "来源目录可用，但当前实例未提供在线检索。"
                    if catalog_ok and runtime_status == "disabled"
                    else "AI 知识引擎诊断失败。"
                ),
                next_actions=(
                    []
                    if available
                    else ["启动并配置 LightRAG 与 Embedding 服务后重新检测。"]
                ),
            )
        if normalized == "pi-runtime":
            runtime = runtime_health_summary(self.store.root_dir, recent_limit=20)
            lifecycle = _read_optional_json(
                self.repository_root
                / "packages"
                / "scholarflow-pi"
                / "compatibility"
                / "runtime-policy.json"
            )
            status = str(runtime.get("status") or "unknown")
            healthy = status in {"ok", "ready", "healthy"}
            active = dict(lifecycle.get("pi") or {}).get("tested_version")
            checks = [
                _integration_check("health", "Pi 运行时健康", healthy, status),
                _integration_check(
                    "policy",
                    "版本兼容策略可读取",
                    bool(active),
                    str(active or "未记录 tested_version"),
                ),
            ]
            return _integration_test_result(
                normalized,
                checks,
                summary=(
                    f"Pi 运行时健康，受测版本 {active}。"
                    if healthy and active
                    else "Pi 运行时或兼容策略需要检查。"
                ),
                next_actions=(
                    []
                    if healthy and active
                    else ["运行严格生命周期检查，并仅从通过 canary 的版本恢复。"]
                ),
            )
        raise MutationError(
            "not_found",
            f"integration '{normalized}' was not found",
        )

    def capabilities(self) -> dict[str, Any]:
        """Describe implemented Agent surfaces and their latest evidence."""
        package_root = self.repository_root / "packages" / "scholarflow-pi"
        policy = _read_optional_json(
            package_root / "compatibility" / "runtime-policy.json"
        )
        fixture_document = _read_optional_json(
            package_root / "quality" / "skill-fixtures.json"
        )
        fixtures = list(fixture_document.get("fixtures") or [])
        skill_names = sorted({
            str(item.get("skill") or "").strip()
            for item in fixtures
            if str(item.get("skill") or "").strip()
        })
        latest_live = _latest_live_evaluation(
            self.repository_root / ".tmp" / "eval"
        )
        runtime_policy = dict(policy.get("pi") or {})
        extension_policy = list(policy.get("extensions") or [])
        validation = {
            "status": str(latest_live.get("status") or "not_run"),
            "provider": (latest_live.get("runtime") or {}).get("provider"),
            "model": (latest_live.get("runtime") or {}).get("model"),
            "selection_verified": bool(
                (latest_live.get("runtime") or {}).get("selection_verified")
            ),
            "metrics": dict(latest_live.get("metrics") or {}),
            "report_name": latest_live.get("report_name"),
            "updated_at": latest_live.get("updated_at"),
        }
        groups = [
            {
                "group_id": "research-loop",
                "title": "研究闭环",
                "status": "ready",
                "items": [
                    "项目与阶段真值",
                    "研究 Intake 与任务分流",
                    "论文筛选与文献综合",
                    "证据约束的 Nature 风格论文草稿",
                    "实验记录与仿真结果分析",
                    "研究计划、日报与周报",
                ],
            },
            {
                "group_id": "knowledge",
                "title": "证据与 AI 知识库",
                "status": "ready",
                "items": [
                    "候选实体、声明与关系审核",
                    "来源与证据锚点追踪",
                    "科学 PDF 结构化解析",
                    "LightRAG 检索适配与知识源状态",
                ],
            },
            {
                "group_id": "governance",
                "title": "安全与治理",
                "status": "ready",
                "items": [
                    "受限 lane 与最小工具面",
                    "proposal-first 写入",
                    "人工确认、版本检查与幂等回放",
                    "审计事件与提示注入防护",
                ],
            },
            {
                "group_id": "integrations",
                "title": "本地应用联动",
                "status": "partial",
                "items": [
                    "Zotero 本地读取与受控导入",
                    "Obsidian 范围受限写入",
                    "Pi RPC 流式会话",
                    "Web/MCP 上游写入仍需独立配置",
                ],
            },
        ]
        return {
            "schema_version": 1,
            "generated_at": _now(),
            "summary": {
                "pi_version": runtime_policy.get("tested_version"),
                "skill_count": len(skill_names),
                "fixture_count": len(fixtures),
                "tool_count": latest_live.get("pi_tool_count"),
                "extension_count": len(extension_policy),
                "latest_live_status": validation["status"],
            },
            "skills": skill_names,
            "extensions": extension_policy,
            "groups": groups,
            "validation": validation,
        }

    def propose_candidate_review(
        self,
        candidate_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self.candidates.propose_review(
            candidate_id,
            decision=str(payload.get("decision") or ""),
            note=str(payload.get("note") or ""),
            request_id="workbench",
        )

    def capture_intake(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_input = str(payload.get("raw_input") or "").strip()
        if not raw_input:
            raise MutationError(
                "invalid_argument",
                "raw_input is required",
            )
        if len(raw_input) > 20_000:
            raise MutationError(
                "invalid_argument",
                "raw_input exceeds the 20000 character limit",
            )
        return self.intake.create_item(
            raw_input=raw_input,
            source="workbench",
        )

    def propose_resource_update(
        self,
        resource: str,
        object_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a governed update proposal for an editable Workbench object."""

        changes = payload.get("changes")
        if not isinstance(changes, Mapping):
            raise MutationError(
                "invalid_argument",
                "changes must be a JSON object",
            )
        if resource == "projects":
            return self.research.propose_project_update(
                object_id,
                changes,
                request_id="workbench",
            )
        if resource == "tasks":
            return self.tasks.propose_update(
                object_id,
                changes,
                request_id="workbench",
            )
        if resource == "papers":
            return self.research.propose_paper_intake(
                object_id,
                changes,
                request_id="workbench",
            )
        if resource == "experiments":
            return self.research.propose_experiment_log(
                object_id,
                changes,
                request_id="workbench",
            )
        if resource == "artifacts":
            return self.research.propose_research_artifact(
                object_id,
                changes,
                request_id="workbench",
            )
        raise MutationError(
            "invalid_argument",
            f"resource '{resource}' does not support Workbench updates",
        )

    def propose_project_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        changes = payload.get("changes")
        if not isinstance(changes, Mapping):
            raise MutationError("invalid_argument", "changes must be a JSON object")
        return self.research.propose_project_create(
            str(payload.get("stage_id") or ""),
            changes,
            request_id="workbench",
        )

    def propose_task_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        changes = payload.get("changes")
        if not isinstance(changes, Mapping):
            raise MutationError("invalid_argument", "changes must be a JSON object")
        return self.tasks.propose_create(
            str(payload.get("task_id") or ""),
            changes,
            request_id="workbench",
        )

    def propose_artifact_vault_write(
        self,
        artifact_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.kb is None:
            raise MutationError(
                "integration_unavailable",
                "Obsidian vault is not configured or does not exist",
            )
        related_links = payload.get("related_links") or []
        if not isinstance(related_links, list):
            raise MutationError("invalid_argument", "related_links must be a JSON array")
        return self.kb.propose_object_write(
            object_type="research_artifact",
            object_id=artifact_id,
            content_kind=str(payload.get("content_kind") or "synthesis"),
            title=str(payload.get("title") or "").strip() or None,
            target_path=str(payload.get("target_path") or "").strip() or None,
            related_links=[str(item) for item in related_links],
            scope=str(payload.get("scope") or "project"),
            request_id="workbench",
        )

    def confirm_proposal(
        self,
        proposal_id: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        receipt = {
            "proposal_id": proposal_id,
            "confirmed": True,
            "confirmed_by": "interactive_user",
            "confirmed_at": _now(),
            "pi_tool_call_id": f"workbench:{session_id[:12]}",
        }
        if self.candidates.has_review_proposal(proposal_id):
            result = self.candidates.apply_review(
                proposal_id,
                confirmation=receipt,
                request_id=receipt["pi_tool_call_id"],
            )
            result["receipt"] = _receipt_from_apply(
                result,
                lane="candidate",
                decided_by=receipt["confirmed_by"],
                decided_at=receipt["confirmed_at"],
            )
            result["project_knowledge"] = self._schedule_project_knowledge(
                operation=str(result.get("operation") or "candidate.review"),
            )
            return result
        state = load_proposal_state(self.store.root_dir)
        proposal = state["proposals"].get(proposal_id)
        if proposal is None:
            raise MutationError(
                "not_found",
                f"proposal '{proposal_id}' was not found",
            )
        operation = str(proposal.get("operation") or "")
        if operation.startswith("task."):
            result = self.tasks.apply_update(
                proposal_id,
                confirmation=receipt,
                request_id=receipt["pi_tool_call_id"],
            )
        elif operation in {
            "project_stage.create",
            "project_stage.update",
            "experiment.log",
            "paper.intake",
            "research_artifact.publish",
            "knowledge_relation.upsert",
        }:
            result = self.research.apply(
                proposal_id,
                confirmation=receipt,
                request_id=receipt["pi_tool_call_id"],
            )
        elif operation == "kb.object.write" and self.kb is not None:
            result = self.kb.apply(
                proposal_id,
                confirmation=receipt,
                request_id=receipt["pi_tool_call_id"],
            )
        elif operation in {"manuscript.create", "manuscript.patch"}:
            result = self.manuscripts.apply(
                proposal_id,
                confirmation=receipt,
                request_id=receipt["pi_tool_call_id"],
            )
        else:
            raise MutationError(
                "unsupported_operation",
                f"proposal operation '{operation}' is not supported by the workbench",
            )
        result["receipt"] = _receipt_from_apply(
            result,
            lane="manuscript" if operation.startswith("manuscript.") else "domain",
            decided_by=receipt["confirmed_by"],
            decided_at=receipt["confirmed_at"],
        )
        result["project_knowledge"] = self._schedule_project_knowledge(
            operation=operation,
        )
        return result

    def _schedule_project_knowledge(self, *, operation: str) -> dict[str, Any]:
        """Queue derived snapshots without weakening a committed mutation."""

        try:
            scheduled = self.project_knowledge.schedule(
                trigger=f"proposal_applied:{operation}",
            )
            return {
                "automatic": True,
                "scheduled": scheduled.get("scheduled", []),
                "counts": scheduled.get("counts", {}),
            }
        except Exception as exc:
            append_event(
                self.store.root_dir,
                event_type="knowledge.project.schedule.failed",
                payload={"operation": operation, "error": str(exc)[:1_000]},
            )
            return {
                "automatic": True,
                "scheduled": [],
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"[:1_000],
            }

    def reject_proposal(
        self,
        proposal_id: str,
        *,
        session_id: str,
        reason: str,
    ) -> dict[str, Any]:
        normalized_reason = str(reason or "").strip()
        if len(normalized_reason) > 1000:
            raise MutationError(
                "invalid_argument",
                "proposal rejection reason exceeds 1000 characters",
            )
        normalized_reason = (
            normalized_reason or "Declined in ScholarFlow Workbench"
        )
        rejected_at = _now()
        request_id = f"workbench:{session_id[:12]}"
        if self.candidates.has_review_proposal(proposal_id):
            return self.candidates.reject_review_proposal(
                proposal_id,
                rejected_by="interactive_user",
                rejected_at=rejected_at,
                reason=normalized_reason,
                request_id=request_id,
            )
        try:
            receipt = reject_proposal(
                self.store.root_dir,
                proposal_id,
                rejected_by="interactive_user",
                rejected_at=rejected_at,
                reason=normalized_reason,
                request_id=request_id,
            )
        except KeyError as exc:
            raise MutationError(
                "not_found",
                f"proposal '{proposal_id}' was not found",
            ) from exc
        except ValueError as exc:
            raise MutationError(
                "proposal_already_committed",
                "the proposal has already been applied",
            ) from exc
        if receipt.get("idempotent_replay"):
            return receipt
        event = append_event(
            self.store.root_dir,
            event_type="pi.domain.mutation.user_rejected",
            payload={
                "proposal_id": proposal_id,
                "operation": receipt.get("operation"),
                "target": receipt.get("target", {}),
                "reason": normalized_reason,
                "pi_tool_call_id": request_id,
            },
        )
        return record_rejection_audit_event(
            self.store.root_dir,
            proposal_id,
            event["event_id"],
        )


def _reader_translate_prompt(source: str) -> str:
    return "\n".join(
        [
            "你现在只做一个微任务：翻译一小段论文原文。",
            "要求：",
            "1. 先给出完整中文翻译。",
            "2. 再用 2-4 条简短 bullet 解释关键术语或难点。",
            "3. 不要扩写成长篇综述，不要编造原文没有的结论。",
            "4. 不要讨论 ScholarFlow、项目、提案、Intake、工作流或任何与翻译无关的话题。",
            "5. 本次调用是独立翻译 API，没有任何可继承的历史上下文；只围绕下面这段原文作答。",
            "",
            "原文：",
            source,
        ]
    )


def _reader_translate_command(
    repository_root: Path,
    *,
    name: str,
    session_dir: Path,
) -> tuple[str, ...]:
    cli = (
        repository_root
        / "packages"
        / "scholarflow-pi"
        / "node_modules"
        / "@earendil-works"
        / "pi-coding-agent"
        / "dist"
        / "cli.js"
    )
    if not cli.exists():
        raise MutationError(
            "runtime_unavailable",
            "project-local Pi CLI is missing; install packages/scholarflow-pi",
        )
    session_dir.mkdir(parents=True, exist_ok=True)
    return (
        node_command(),
        str(cli),
        "--provider",
        os.environ.get("SCHOLARFLOW_PI_PROVIDER", DEFAULT_PI_PROVIDER),
        "--model",
        os.environ.get("SCHOLARFLOW_PI_MODEL", DEFAULT_PI_MODEL),
        "--mode",
        "rpc",
        "--session-dir",
        str(session_dir),
        "--no-session",
        "--no-builtin-tools",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--approve",
        "--name",
        name,
    )


def _run_isolated_reader_translation(
    *,
    repository_root: Path,
    data_dir: Path,
    source: str,
    on_process: Callable[[Any], bool] | None = None,
) -> dict[str, Any]:
    process = SubprocessPiRpcProcess(
        PiRpcConfig(
            command=_reader_translate_command(
                repository_root,
                name=f"ScholarFlow PDF Translation {secrets.token_hex(4)}",
                session_dir=data_dir,
            ),
            cwd=repository_root,
            trust_mode="approved_readonly",
            request_timeout_seconds=READER_TRANSLATE_TIMEOUT_SECONDS,
        )
    )
    deltas: list[str] = []
    complete_text = ""

    def capture_event(event: Mapping[str, Any]) -> None:
        nonlocal complete_text
        if event.get("type") == "message_update":
            update = event.get("assistantMessageEvent") or {}
            if (
                isinstance(update, Mapping)
                and update.get("type") == "text_delta"
                and isinstance(update.get("delta"), str)
            ):
                deltas.append(update["delta"])
            return
        if event.get("type") != "message_end":
            return
        message = event.get("message") or {}
        if (
            isinstance(message, Mapping)
            and message.get("role") == "assistant"
        ):
            complete_text = message_text(message.get("content")).strip()

    try:
        if on_process is not None and not on_process(process):
            raise MutationError("translation_cancelled", "translation was cancelled")
        process.start()
        response = process.request_interactive(
            {
                "id": f"reader-translate-{secrets.token_hex(8)}",
                "type": "prompt",
                "message": _reader_translate_prompt(source),
            },
            timeout_seconds=READER_TRANSLATE_TIMEOUT_SECONDS,
            event_sink=capture_event,
            ui_responder=None,
        )
    except PiRpcHostError as exc:
        raise MutationError(
            "translation_unavailable",
            f"独立翻译 API 调用失败：{exc}",
        ) from exc
    finally:
        process.close()

    if not response.get("success", False):
        raise MutationError(
            "translation_unavailable",
            str(response.get("error") or "独立翻译 API 未接受请求"),
        )
    translation = complete_text or "".join(deltas).strip()
    if not translation:
        raise MutationError(
            "translation_empty",
            "独立翻译 API 没有返回有效结果，请重试。",
        )
    return {
        "schema_version": 1,
        "read_only": True,
        "isolated": True,
        "source": source,
        "translation": translation,
    }


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        application: WorkbenchApplication,
    ):
        super().__init__(server_address, WorkbenchRequestHandler)
        self.application = application
        self.sessions: set[str] = set()
        self.application.start_background_services()

    def server_close(self) -> None:
        self.application.agent_sessions.close()
        self.application.project_knowledge.close()
        self.application.reader_translations.close()
        super().server_close()


class WorkbenchRequestHandler(BaseHTTPRequestHandler):
    server: WorkbenchServer

    def do_OPTIONS(self) -> None:
        self.request_id = secrets.token_hex(12)
        if not self._origin_allowed():
            self._json_error(
                HTTPStatus.FORBIDDEN,
                "origin_forbidden",
                "request origin is not allowed",
            )
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-ScholarFlow-Intent, X-ScholarFlow-Filename, X-ScholarFlow-Project",
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        self.request_id = secrets.token_hex(12)
        try:
            path = urlparse(self.path).path
            if path == "/api/health":
                self._json({
                    "status": "ok",
                    "service": "scholarflow-workbench",
                    "loopback_only": True,
                })
            elif path == "/api/session":
                if not self._origin_allowed(required=True):
                    self._json_error(
                        HTTPStatus.FORBIDDEN,
                        "origin_forbidden",
                        "request origin is not allowed",
                    )
                    return
                existing_session = self._session_id()
                if existing_session in self.server.sessions:
                    self._json({"status": "ready", "reused": True})
                    return
                session_id = secrets.token_urlsafe(32)
                self.server.sessions.add(session_id)
                self._json(
                    {"status": "ready"},
                    cookie=(
                        f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; "
                        "SameSite=Strict"
                    ),
                )
            elif path == "/api/dashboard":
                self._json(self.server.application.dashboard())
            elif path.startswith("/api/research-loop/"):
                if not self._origin_allowed(required=True) or self._session_id() not in self.server.sessions:
                    self._json_error(HTTPStatus.FORBIDDEN,"confirmation_session_required","a local interactive workbench session is required")
                    return
                service=self.server.application.web_research
                params=_query_params(self.path);stage=_first(params,"stage_id") or ""
                if path=="/api/research-loop/projects":self._json(service.projects())
                elif path=="/api/research-loop/workspaces":self._json(service.list(stage))
                elif path.startswith("/api/research-loop/workspaces/"):
                    self._json(service.get(_path_segment(path,prefix="/api/research-loop/workspaces/"),stage))
                else:self._json_error(HTTPStatus.NOT_FOUND,"not_found","route was not found")
            elif path == "/api/search":
                params = _query_params(self.path)
                self._json(
                    self.server.application.global_search(
                        _first(params, "q") or "",
                        limit=_param_int(params, "limit", default=30),
                    )
                )
            elif path == "/api/evidence":
                params = _query_params(self.path)
                self._json(
                    self.server.application.object_evidence(
                        _first(params, "ref") or "",
                        limit=_param_int(params, "limit", default=50),
                    )
                )
            elif path == "/api/knowledge/health":
                self._json(self.server.application.knowledge_health())
            elif path == "/api/knowledge/project-sync":
                params = parse_qs(urlparse(self.path).query)
                self._json(
                    self.server.application.project_knowledge.status(
                        project_id=_first(params, "project_id"),
                    )
                )
            elif path.startswith("/api/knowledge/project-snapshots/"):
                project_id = _path_segment(
                    path,
                    prefix="/api/knowledge/project-snapshots/",
                )
                self._json(
                    self.server.application.project_knowledge.snapshot(project_id)
                )
            elif path == "/api/knowledge/sources":
                params = _query_params(self.path)
                self._json(
                    self.server.application.knowledge_sources(
                        scope=_first(params, "scope"),
                        limit=_param_int(params, "limit", default=100),
                    )
                )
            elif path == "/api/knowledge/search":
                params = _query_params(self.path)
                self._json(
                    self.server.application.knowledge_search(
                        _first(params, "q") or "",
                        mode=_first(params, "mode") or "auto",
                        limit=_param_int(params, "limit", default=5),
                        scope=_first(params, "scope"),
                        project_stage_id=_first(params, "project_stage_id"),
                    )
                )
            elif path == "/api/knowledge/sync-options":
                self._json(self.server.application.knowledge_sync_options())
            elif path == "/api/knowledge/sync-plans":
                self._json(self.server.application.knowledge_sync.list_jobs())
            elif path.startswith("/api/knowledge/sync-plans/"):
                self._json(self.server.application.knowledge_sync.get_plan(_path_segment(path, prefix="/api/knowledge/sync-plans/")))
            elif path == "/api/proposals":
                self._json(self.server.application.proposals())
            elif path == "/api/proposal-receipts":
                params = _query_params(self.path)
                self._json(
                    self.server.application.proposal_history(
                        limit=_param_int(params, "limit", default=50)
                    )
                )
            elif path.startswith("/api/proposals/"):
                self._json(
                    self.server.application.proposal_detail(
                        _path_segment(path, prefix="/api/proposals/")
                    )
                )
            elif path == "/api/candidates":
                self._json(self.server.application.candidates.list(limit=100))
            elif path == "/api/projects":
                params = _query_params(self.path)
                self._json(
                    self.server.application.queries.search_project_stages(
                        query=_first(params, "query"),
                        status=_first(params, "status"),
                        limit=_param_int(params, "limit", default=100),
                    )
                )
            elif path.startswith("/api/projects/") and path.endswith("/workspace"):
                self._json(
                    self.server.application.project_workspace(
                        _path_segment(
                            path,
                            prefix="/api/projects/",
                            suffix="/workspace",
                        )
                    )
                )
            elif path.startswith("/api/projects/"):
                self._json(
                    self.server.application.queries.get_project_stage(
                        _path_segment(path, prefix="/api/projects/")
                    )
                )
            elif path == "/api/tasks":
                params = _query_params(self.path)
                self._json(
                    self.server.application.queries.search_tasks(
                        query=_first(params, "query"),
                        status=_first(params, "status"),
                        project_stage_id=_first(params, "project_stage_id"),
                        limit=_param_int(params, "limit", default=100),
                    )
                )
            elif path.startswith("/api/tasks/"):
                self._json(
                    self.server.application.queries.get_task(
                        _path_segment(path, prefix="/api/tasks/")
                    )
                )
            elif path == "/api/papers":
                params = _query_params(self.path)
                self._json(
                    self.server.application.queries.search_papers(
                        query=_first(params, "query"),
                        status=_first(params, "status"),
                        limit=_param_int(params, "limit", default=100),
                        offset=_param_int(params, "offset", default=0),
                        author=_first(params, "author"),
                        year_from=_param_int(params, "year_from", default=0),
                        year_to=_param_int(params, "year_to", default=0),
                    )
                )
            elif path == "/api/literature/search":
                params = _query_params(self.path)
                self._json(
                    self.server.application.literature_search(
                        _first(params, "q"),
                        limit=_param_int(params, "limit", default=12),
                        page=_param_int(params, "page", default=1),
                        author=_first(params, "author"),
                        year_from=_param_int(params, "year_from", default=0),
                        year_to=_param_int(params, "year_to", default=0),
                        oa_only=_first(params, "oa_only") == "true",
                        provider=_first(params, "provider"),
                    )
                )
            elif path == "/api/reader/imports":
                self._json(self.server.application.reader_imports())
            elif path == "/api/reader/translations":
                self._json(self.server.application.reader_translations.list())
            elif path.startswith("/api/reader/translations/"):
                task_id = _path_segment(path, prefix="/api/reader/translations/")
                self._json(self.server.application.reader_translations.get(task_id))
            elif path == "/api/reader/annotations":
                params = _query_params(self.path)
                self._json(
                    self.server.application.reader_annotations(
                        _first(params, "target_ref") or ""
                    )
                )
            elif path.startswith("/api/reader/imports/"):
                if path.endswith("/pdf"):
                    filename, payload, metadata = self.server.application.reader_import_pdf(
                        _path_segment(path, prefix="/api/reader/imports/", suffix="/pdf")
                    )
                    self._bytes(
                        payload,
                        content_type="application/pdf",
                        filename=filename,
                        disposition="inline",
                        extra_headers={"X-ScholarFlow-Pdf-Source": str(metadata["source"])},
                    )
                elif path.endswith("/reader"):
                    params = _query_params(self.path)
                    self._json(
                        self.server.application.reader_import_reader(
                            _path_segment(path, prefix="/api/reader/imports/", suffix="/reader"),
                            offset=_param_int(params, "offset", default=0),
                            limit=_param_int(params, "limit", default=250),
                        )
                    )
                else:
                    self._json(
                        self.server.application.reader.get_import(
                            _path_segment(path, prefix="/api/reader/imports/")
                        )
                    )
            elif path == "/api/manuscripts":
                params = _query_params(self.path)
                self._json(
                    self.server.application.manuscripts.list(
                        project_stage_id=_first(params, "project_stage_id")
                    )
                )
            elif path.startswith("/api/manuscripts/") and path.endswith("/validation"):
                self._json(
                    self.server.application.manuscripts.validate(
                        _path_segment(path, prefix="/api/manuscripts/", suffix="/validation")
                    )
                )
            elif path.startswith("/api/manuscripts/") and path.endswith("/upload-bundle"):
                filename, payload, metadata = self.server.application.manuscripts.export_upload_bundle(
                    _path_segment(path, prefix="/api/manuscripts/", suffix="/upload-bundle")
                )
                self._bytes(
                    payload,
                    content_type="application/zip",
                    filename=filename,
                    extra_headers={
                        "X-ScholarFlow-Source-Commit": str(metadata["source_commit"]),
                        "X-ScholarFlow-File-Count": str(metadata["file_count"]),
                    },
                )
            elif path.startswith("/api/manuscripts/") and path.endswith("/pdf"):
                filename, payload, report = self.server.application.manuscripts.compiled_pdf(
                    _path_segment(path, prefix="/api/manuscripts/", suffix="/pdf")
                )
                self._bytes(
                    payload,
                    content_type="application/pdf",
                    filename=filename,
                    disposition="inline",
                    extra_headers={"X-ScholarFlow-Source-Commit": str(report["source_commit"])},
                )
            elif path.startswith("/api/manuscripts/") and "/sections/" in path:
                remainder = path.removeprefix("/api/manuscripts/")
                manuscript_id, section = remainder.split("/sections/", 1)
                self._json(
                    self.server.application.manuscripts.read_section(
                        unquote(manuscript_id.strip("/")),
                        unquote(section.strip("/")),
                    )
                )
            elif path.startswith("/api/manuscripts/"):
                self._json(
                    self.server.application.manuscripts.get(
                        _path_segment(path, prefix="/api/manuscripts/")
                    )
                )
            elif path.startswith("/api/papers/"):
                if path.endswith("/pdf"):
                    filename, payload, metadata = self.server.application.paper_pdf(
                        _path_segment(path, prefix="/api/papers/", suffix="/pdf")
                    )
                    self._bytes(
                        payload,
                        content_type="application/pdf",
                        filename=filename,
                        disposition="inline",
                        extra_headers={
                            "X-ScholarFlow-Pdf-Source": str(metadata["source"]),
                        },
                    )
                elif path.endswith("/reader"):
                    params = _query_params(self.path)
                    self._json(
                        self.server.application.paper_reader(
                            _path_segment(path, prefix="/api/papers/", suffix="/reader"),
                            offset=_param_int(params, "offset", default=0),
                            limit=_param_int(params, "limit", default=250),
                        )
                    )
                else:
                    self._json(
                        self.server.application.queries.get_paper(
                            _path_segment(path, prefix="/api/papers/")
                        )
                    )
            elif path == "/api/experiments":
                params = _query_params(self.path)
                self._json(
                    self.server.application.queries.search_experiments(
                        query=_first(params, "query"),
                        status=_first(params, "status"),
                        project_stage_id=_first(params, "project_stage_id"),
                        limit=_param_int(params, "limit", default=100),
                    )
                )
            elif path.startswith("/api/experiments/"):
                self._json(
                    self.server.application.queries.get_experiment(
                        _path_segment(path, prefix="/api/experiments/")
                    )
                )
            elif path == "/api/artifacts":
                params = _query_params(self.path)
                self._json(
                    self.server.application.queries.search_research_artifacts(
                        query=_first(params, "query"),
                        status=_first(params, "status"),
                        kind=_first(params, "kind"),
                        project_stage_id=_first(params, "project_stage_id"),
                        limit=_param_int(params, "limit", default=100),
                    )
                )
            elif path.startswith("/api/artifacts/"):
                self._json(
                    self.server.application.queries.get_research_artifact(
                        _path_segment(path, prefix="/api/artifacts/")
                    )
                )
            elif path == "/api/relations":
                params = _query_params(self.path)
                self._json(
                    self.server.application.queries.search_knowledge_relations(
                        query=_first(params, "query"),
                        predicate=_first(params, "predicate"),
                        subject_ref=_first(params, "subject_ref"),
                        object_ref=_first(params, "object_ref"),
                        limit=_param_int(params, "limit", default=100),
                    )
                )
            elif path.startswith("/api/relations/"):
                self._json(
                    self.server.application.queries.get_knowledge_relation(
                        _path_segment(path, prefix="/api/relations/")
                    )
                )
            elif path == "/api/audit":
                params = _query_params(self.path)
                self._json(
                    self.server.application.audit(
                        limit=_param_int(params, "limit", default=100)
                    )
                )
            elif path == "/api/automation":
                self._json(self.server.application.automation())
            elif path == "/api/integrations":
                self._json(self.server.application.integrations())
            elif path == "/api/capabilities":
                self._json(self.server.application.capabilities())
            elif path == "/api/agent/sessions":
                self._json(self.server.application.agent_sessions.list())
            elif path == "/api/agent/checkpoints":
                params = parse_qs(urlparse(self.path).query)
                self._json(
                    self.server.application.agent_sessions.checkpoints(
                        session_id=_first(params, "session_id"),
                        project_id=_first(params, "project_id"),
                    )
                )
            elif (
                path.startswith("/api/agent/sessions/")
                and path.endswith("/context-manifests")
            ):
                session_id = _path_segment(
                    path,
                    prefix="/api/agent/sessions/",
                    suffix="/context-manifests",
                )
                params = parse_qs(urlparse(self.path).query)
                self._json(
                    self.server.application.agent_sessions.context_manifests(
                        session_id,
                        request_id=_first(params, "request_id"),
                    )
                )
            elif (
                path.startswith("/api/agent/sessions/")
                and path.endswith("/observations")
            ):
                session_id = _path_segment(
                    path,
                    prefix="/api/agent/sessions/",
                    suffix="/observations",
                )
                params = parse_qs(urlparse(self.path).query)
                self._json(
                    self.server.application.agent_sessions.observations(
                        session_id,
                        request_id=_first(params, "request_id"),
                    )
                )
            elif (
                path.startswith("/api/agent/sessions/")
                and path.endswith("/events")
            ):
                session_id = _path_segment(
                    path,
                    prefix="/api/agent/sessions/",
                    suffix="/events",
                )
                query = urlparse(self.path).query
                after = _query_int(query, "after", default=0)
                self._json(
                    self.server.application.agent_sessions.events(
                        session_id,
                        after=after,
                    )
                )
            elif path.startswith("/api/agent/sessions/") and path.endswith("/messages"):
                session_id = _path_segment(path, prefix="/api/agent/sessions/", suffix="/messages")
                self._json(self.server.application.agent_sessions.messages(session_id, before=_query_int(urlparse(self.path).query, "before", default=0), limit=_query_int(urlparse(self.path).query, "limit", default=50)))
            elif (
                path.startswith("/api/agent/sessions/")
                and path.endswith("/checkpoints")
            ):
                session_id = _path_segment(
                    path,
                    prefix="/api/agent/sessions/",
                    suffix="/checkpoints",
                )
                self._json(
                    self.server.application.agent_sessions.checkpoints(
                        session_id=session_id,
                    )
                )
            elif path.startswith("/api/agent/sessions/"):
                session_id = _path_segment(
                    path,
                    prefix="/api/agent/sessions/",
                )
                self._json(
                    self.server.application.agent_sessions.get(session_id)
                )
            else:
                self._json_error(
                    HTTPStatus.NOT_FOUND,
                    "not_found",
                    "route was not found",
                )
        except Exception as exc:
            self._handle_exception(exc)

    def do_POST(self) -> None:
        self.request_id = secrets.token_hex(12)
        try:
            if not self._valid_write_session():
                self._json_error(
                    HTTPStatus.FORBIDDEN,
                    "confirmation_session_required",
                    "a local interactive workbench session is required",
                )
                return
            path = urlparse(self.path).path
            if path == "/api/reader/imports":
                filename, raw_pdf = self._read_pdf_upload()
                self._json(
                    self.server.application.import_reader_pdf(filename, raw_pdf, project_stage_ids=([unquote(self.headers.get("X-ScholarFlow-Project", ""))] if self.headers.get("X-ScholarFlow-Project") else [])),
                    status=HTTPStatus.CREATED,
                )
                return
            payload = self._read_json()
            if path=="/api/research-loop/workspaces":
                self._json(self.server.application.web_research.create(payload),status=HTTPStatus.CREATED)
                return
            if path.startswith("/api/research-loop/workspaces/"):
                service=self.server.application.web_research
                if path.endswith("/turns"):
                    self._json(service.prompt(_path_segment(path,prefix="/api/research-loop/workspaces/",suffix="/turns"),payload),status=HTTPStatus.ACCEPTED);return
                if path.endswith("/approve"):
                    self._json(service.decide(_path_segment(path,prefix="/api/research-loop/workspaces/",suffix="/approve"),payload));return
                if path.endswith("/index"):
                    self._json(service.index(_path_segment(path,prefix="/api/research-loop/workspaces/",suffix="/index"),payload),status=HTTPStatus.ACCEPTED);return
            if path == "/api/reader/imports/from-url":
                self._json(
                    self.server.application.import_reader_pdf_from_url(payload),
                    status=HTTPStatus.CREATED,
                )
                return
            if path == "/api/reader/translations":
                request_task_id = str(payload.get("task_id") or "").strip() or None
                task = self.server.application.reader_translations.create(str(payload.get("source") or ""), task_id=request_task_id)
                self._json(task, status=HTTPStatus.ACCEPTED)
                return
            if path.startswith("/api/reader/translations/") and path.endswith("/cancel"):
                task_id = _path_segment(path, prefix="/api/reader/translations/", suffix="/cancel")
                self._json(self.server.application.reader_translations.cancel(task_id), status=HTTPStatus.ACCEPTED)
                return
            if path.startswith("/api/reader/imports/") and path.endswith("/projects"):
                self._json(self.server.application.reader_import_projects(_path_segment(path, prefix="/api/reader/imports/", suffix="/projects"), payload))
                return
            if path.startswith("/api/reader/imports/") and path.endswith("/reparse"):
                self._json(self.server.application.reparse_reader_import(_path_segment(path, prefix="/api/reader/imports/", suffix="/reparse")))
                return
            if path == "/api/agent/sessions":
                result = self.server.application.agent_sessions.create(
                    name=str(payload.get("name") or ""),
                    resume_session_id=(
                        str(payload.get("resume_session_id") or "").strip()
                        or None
                    ),
                    reuse_latest=bool(payload.get("reuse_latest", True)),
                    scope=str(payload.get("scope") or "global"),
                    project_id=(str(payload.get("project_id") or "").strip() or None),
                    parent_session_id=(
                        str(payload.get("parent_session_id") or "").strip() or None
                    ),
                    title=(str(payload.get("title") or "").strip() or None),
                    objective=(str(payload.get("objective") or "").strip() or None),
                )
                self._json(result, status=HTTPStatus.CREATED)
                return
            if (
                path.startswith("/api/agent/sessions/")
                and path.endswith("/prompts")
            ):
                session_id = _path_segment(
                    path,
                    prefix="/api/agent/sessions/",
                    suffix="/prompts",
                )
                context_refs = payload.get("context_refs") or []
                if not isinstance(context_refs, list):
                    raise MutationError(
                        "invalid_argument",
                        "context_refs must be a JSON array",
                    )
                result = self.server.application.agent_sessions.prompt(
                    session_id,
                    message=str(payload.get("message") or ""),
                    context_refs=[str(item) for item in context_refs],
                )
                self._json(result, status=HTTPStatus.ACCEPTED)
                return
            if (
                path.startswith("/api/agent/sessions/")
                and path.endswith("/checkpoints")
            ):
                session_id = _path_segment(
                    path,
                    prefix="/api/agent/sessions/",
                    suffix="/checkpoints",
                )
                list_fields = (
                    "decisions",
                    "open_questions",
                    "next_actions",
                    "evidence_refs",
                )
                for field_name in list_fields:
                    if field_name in payload and not isinstance(payload[field_name], list):
                        raise MutationError(
                            "invalid_argument",
                            f"{field_name} must be a JSON array",
                        )
                result = self.server.application.agent_sessions.create_checkpoint(
                    session_id,
                    title=str(payload.get("title") or "") or None,
                    summary=str(payload.get("summary") or "") or None,
                    decisions=payload.get("decisions"),
                    open_questions=payload.get("open_questions"),
                    next_actions=payload.get("next_actions"),
                    evidence_refs=payload.get("evidence_refs"),
                )
                self._json(result, status=HTTPStatus.CREATED)
                return
            if (
                path.startswith("/api/agent/sessions/")
                and "/ui/" in path
                and path.endswith("/responses")
            ):
                remainder = path.removeprefix("/api/agent/sessions/")
                session_part, ui_part = remainder.split("/ui/", 1)
                request_part = ui_part.removesuffix("/responses").strip("/")
                result = self.server.application.agent_sessions.respond_ui(
                    unquote(session_part.strip("/")),
                    unquote(request_part),
                    payload,
                )
                self._json(result, status=HTTPStatus.ACCEPTED)
                return
            if (
                path.startswith("/api/agent/sessions/")
                and path.endswith("/stop")
            ):
                session_id = _path_segment(
                    path,
                    prefix="/api/agent/sessions/",
                    suffix="/stop",
                )
                self._json(
                    self.server.application.agent_sessions.stop(session_id)
                )
                return
            if path == "/api/intake":
                result = self.server.application.capture_intake(payload)
                self._json(result, status=HTTPStatus.CREATED)
                return
            if path == "/api/reader/translate":
                self._json(
                    self.server.application.reader_translate(payload),
                    status=HTTPStatus.CREATED,
                )
                return
            if path == "/api/reader/annotations":
                self._json(
                    self.server.application.create_reader_annotation(payload),
                    status=HTTPStatus.CREATED,
                )
                return
            if path.startswith("/api/reader/annotations/") and path.endswith("/delete"):
                self._json(
                    self.server.application.delete_reader_annotation(
                        _path_segment(
                            path,
                            prefix="/api/reader/annotations/",
                            suffix="/delete",
                        )
                    )
                )
                return
            if path.startswith("/api/reader/imports/") and path.endswith("/paper-proposals"):
                self._json(
                    self.server.application.propose_reader_import_paper(
                        _path_segment(
                            path,
                            prefix="/api/reader/imports/",
                            suffix="/paper-proposals",
                        ),
                        payload,
                    ),
                    status=HTTPStatus.CREATED,
                )
                return
            if path.startswith("/api/papers/") and path.endswith("/reader-imports"):
                self._json(
                    self.server.application.stage_paper_pdf(
                        _path_segment(
                            path,
                            prefix="/api/papers/",
                            suffix="/reader-imports",
                        ),
                        project_stage_ids=payload.get("project_stage_ids"),
                    ),
                    status=HTTPStatus.CREATED,
                )
                return
            if path == "/api/projects/create-proposals":
                result = self.server.application.propose_project_create(payload)
                self._json(result, status=HTTPStatus.CREATED)
                return
            if path == "/api/tasks/create-proposals":
                result = self.server.application.propose_task_create(payload)
                self._json(result, status=HTTPStatus.CREATED)
                return
            if path == "/api/manuscripts/create-proposals":
                result = self.server.application.propose_manuscript_create(payload)
                self._json(result, status=HTTPStatus.CREATED)
                return
            if path.startswith("/api/manuscripts/") and path.endswith("/compile"):
                manuscript_id = _path_segment(path, prefix="/api/manuscripts/", suffix="/compile")
                self._json(self.server.application.manuscripts.compile(manuscript_id))
                return
            if path.startswith("/api/manuscripts/") and path.endswith("/open-vscode"):
                manuscript_id = _path_segment(path, prefix="/api/manuscripts/", suffix="/open-vscode")
                self._json(self.server.application.manuscripts.open_in_vscode(manuscript_id))
                return
            if path == "/api/automation/workflows":
                result = self.server.application.queue_automation_workflow(
                    payload
                )
                self._json(result, status=HTTPStatus.CREATED)
                return
            if (
                path.startswith("/api/automation/messages/")
                and path.endswith("/acknowledge")
            ):
                message_id = _path_segment(
                    path,
                    prefix="/api/automation/messages/",
                    suffix="/acknowledge",
                )
                self._json(
                    self.server.application.acknowledge_automation_message(
                        message_id,
                        payload,
                        session_id=self._session_id() or "",
                    )
                )
                return
            if (
                path.startswith("/api/integrations/")
                and path.endswith("/tests")
            ):
                integration_id = _path_segment(
                    path,
                    prefix="/api/integrations/",
                    suffix="/tests",
                )
                self._json(
                    self.server.application.test_integration(integration_id)
                )
                return
            if path == "/api/knowledge/sync-plans":
                result = self.server.application.create_knowledge_sync_plan(payload)
                self._json(result, status=HTTPStatus.CREATED)
                return
            if path == "/api/knowledge/project-sync/retry":
                wait_value = payload.get("wait", False)
                if not isinstance(wait_value, bool):
                    raise MutationError("invalid_argument", "wait must be a JSON boolean")
                result = self.server.application.project_knowledge.retry(
                    project_id=(str(payload.get("project_id") or "").strip() or None),
                    wait=wait_value,
                )
                self._json(result, status=HTTPStatus.ACCEPTED)
                return
            if (
                path.startswith("/api/knowledge/sync-plans/")
                and path.endswith("/apply")
            ):
                plan_id = _path_segment(
                    path,
                    prefix="/api/knowledge/sync-plans/",
                    suffix="/apply",
                )
                result = self.server.application.apply_knowledge_sync_plan(
                    plan_id,
                    payload,
                )
                self._json(result)
                return
            for resource in ("projects", "tasks", "papers", "experiments", "artifacts"):
                prefix = f"/api/{resource}/"
                suffix = "/update-proposals"
                if path.startswith(prefix) and path.endswith(suffix):
                    object_id = _path_segment(
                        path,
                        prefix=prefix,
                        suffix=suffix,
                    )
                    result = self.server.application.propose_resource_update(
                        resource,
                        object_id,
                        payload,
                    )
                    self._json(result, status=HTTPStatus.CREATED)
                    return
            if (
                path.startswith("/api/manuscripts/")
                and path.endswith("/patch-proposals")
            ):
                manuscript_id = _path_segment(
                    path,
                    prefix="/api/manuscripts/",
                    suffix="/patch-proposals",
                )
                result = self.server.application.propose_manuscript_patch(
                    manuscript_id,
                    payload,
                )
                self._json(result, status=HTTPStatus.CREATED)
                return
            if (
                path.startswith("/api/artifacts/")
                and path.endswith("/vault-write-proposals")
            ):
                artifact_id = _path_segment(
                    path,
                    prefix="/api/artifacts/",
                    suffix="/vault-write-proposals",
                )
                result = self.server.application.propose_artifact_vault_write(
                    artifact_id,
                    payload,
                )
                self._json(result, status=HTTPStatus.CREATED)
                return
            if (
                path.startswith("/api/candidates/")
                and path.endswith("/review-proposals")
            ):
                candidate_id = unquote(
                    path.removeprefix("/api/candidates/").removesuffix(
                        "/review-proposals"
                    )
                ).strip("/")
                result = self.server.application.propose_candidate_review(
                    candidate_id,
                    payload,
                )
                self._json(result, status=HTTPStatus.CREATED)
                return
            if (
                path.startswith("/api/proposals/")
                and path.endswith("/confirm")
            ):
                proposal_id = unquote(
                    path.removeprefix("/api/proposals/").removesuffix(
                        "/confirm"
                    )
                ).strip("/")
                result = self.server.application.confirm_proposal(
                    proposal_id,
                    session_id=self._session_id() or "",
                )
                self._json(result)
                return
            if (
                path.startswith("/api/proposals/")
                and path.endswith("/reject")
            ):
                proposal_id = unquote(
                    path.removeprefix("/api/proposals/").removesuffix(
                        "/reject"
                    )
                ).strip("/")
                result = self.server.application.reject_proposal(
                    proposal_id,
                    session_id=self._session_id() or "",
                    reason=str(payload.get("reason") or ""),
                )
                self._json(result)
                return
            self._json_error(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "route was not found",
            )
        except Exception as exc:
            self._handle_exception(exc)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 100_000:
            raise MutationError(
                "invalid_argument",
                "request body exceeds the 100000 byte limit",
            )
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MutationError(
                "invalid_argument",
                "request body must be a UTF-8 JSON object",
            ) from exc
        if not isinstance(payload, dict):
            raise MutationError(
                "invalid_argument",
                "request body must be a JSON object",
            )
        return payload

    def _read_pdf_upload(self) -> tuple[str, bytes]:
        content_type = (
            str(self.headers.get("Content-Type") or "")
            .split(";", 1)[0]
            .strip()
            .casefold()
        )
        if content_type != "application/pdf":
            raise MutationError(
                "invalid_argument",
                "PDF uploads require application/pdf",
            )
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise MutationError(
                "invalid_argument",
                "PDF upload length is invalid",
            ) from exc
        if length <= 0 or length > MAX_READER_PDF_BYTES:
            raise MutationError(
                "invalid_argument",
                "PDF upload must be between 1 byte and 80 MB",
            )
        payload = self.rfile.read(length)
        if len(payload) != length:
            raise MutationError(
                "invalid_argument",
                "PDF upload ended before Content-Length",
            )
        filename = unquote(
            str(self.headers.get("X-ScholarFlow-Filename") or "document.pdf")
        )
        return filename, payload

    def _valid_write_session(self) -> bool:
        return bool(
            self._origin_allowed(required=True)
            and self.headers.get("X-ScholarFlow-Intent") == "confirm"
            and self._session_id() in self.server.sessions
        )

    def _session_id(self) -> str | None:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel is not None else None

    def _origin_allowed(self, *, required: bool = False) -> bool:
        origin = str(self.headers.get("Origin") or "")
        if not origin:
            return not required
        return origin in _allowed_origins()

    def _json(
        self,
        payload: Any,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        cookie: str | None = None,
    ) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-ScholarFlow-Request-Id", self.request_id)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(encoded)

    def _json_error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        record_http_error(self.server.application.store.root_dir, request_id=self.request_id, method=self.command, code=code, status=int(status))
        self._json(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "details": details or {},
                    "request_id": self.request_id,
                }
            },
            status=status,
        )

    def _bytes(
        self,
        payload: bytes,
        *,
        content_type: str,
        filename: str,
        disposition: str = "attachment",
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        safe_filename = "".join(char for char in filename if char.isalnum() or char in {"-", "_", "."})
        self.send_response(HTTPStatus.OK)
        self._cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'{disposition}; filename="{safe_filename}"')
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _handle_exception(self, exc: Exception) -> None:
        if not isinstance(exc, (MutationError, DomainQueryError, WorkbenchAgentError, PiRpcHostError)):
            record_http_error(self.server.application.store.root_dir, request_id=self.request_id, method=self.command, code="internal_error", status=500, exception=exc)
        if isinstance(
            exc,
            (
                MutationError,
                DomainQueryError,
                WorkbenchAgentError,
                PiRpcHostError,
            ),
        ):
            code = str(getattr(exc, "code", "invalid_argument"))
            status = (
                HTTPStatus.NOT_FOUND
                if code == "not_found"
                else HTTPStatus.CONFLICT
                if code
                in {
                    "stale_proposal",
                    "invalid_state",
                    "expired_proposal",
                    "session_busy",
                    "stale_knowledge_sync_plan",
                }
                else HTTPStatus.NOT_FOUND
                if code == "knowledge_sync_plan_not_found"
                else HTTPStatus.TOO_MANY_REQUESTS
                if code == "reader_translation_queue_full"
                else HTTPStatus.SERVICE_UNAVAILABLE
                if code
                in {
                    "runtime_unavailable",
                    "session_unavailable",
                    "process_unavailable",
                    "process_exited",
                    "timeout",
                    "knowledge_engine_unavailable",
                    "knowledge_configuration_error",
                    "knowledge_engine_busy",
                    "knowledge_source_unavailable",
                    "literature_search_unavailable",
                    "literature_search_rate_limited",
                    "literature_search_invalid_response",
                    "remote_pdf_unavailable",
                    "paper_pdf_zotero_unavailable",
                }
                else HTTPStatus.BAD_REQUEST
            )
            self._json_error(
                status,
                code,
                str(exc),
                details=getattr(exc, "details", {}),
            )
            return
        self._json_error(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "internal_error",
            "the local workbench request failed",
        )

    def _cors_headers(self) -> None:
        origin = str(self.headers.get("Origin") or "")
        if origin in _allowed_origins():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Access-Control-Expose-Headers", "X-ScholarFlow-Request-Id")
            self.send_header("Vary", "Origin")

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


def _proposal_expired(proposal: Mapping[str, Any]) -> bool:
    value = proposal.get("expires_at")
    if not value:
        return False
    try:
        deadline = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


def _proposal_view(
    proposal: dict[str, Any],
    *,
    lane: str,
) -> dict[str, Any]:
    return {
        "proposal_id": proposal.get("proposal_id"),
        "operation": proposal.get("operation"),
        "target": proposal.get("target", {}),
        "diff": proposal.get("diff", {}),
        "policy": proposal.get("policy", {}),
        "created_at": proposal.get("created_at"),
        "expires_at": proposal.get("expires_at"),
        "lane": lane,
        "status": "pending",
    }


def _sanitized_reader_source(record: Mapping[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata")
    metadata_map = metadata if isinstance(metadata, Mapping) else {}
    return {
        "source_ref": record.get("source_ref"),
        "source_type": record.get("source_type"),
        "title": record.get("title"),
        "status": record.get("status"),
        "source_version": record.get("source_version"),
        "document_id": record.get("document_id"),
        "attachment_key": metadata_map.get("attachment_key"),
        "file_name": metadata_map.get("file_name") or metadata_map.get("relative_path"),
        "structured": bool(record.get("structured_document_path")),
    }


def _sanitized_document_metadata(value: Any) -> dict[str, Any]:
    metadata = value if isinstance(value, Mapping) else {}
    allowed = ("file_name", "subject", "language", "doi", "journal")
    return {key: metadata.get(key) for key in allowed if metadata.get(key)}


def _scientific_document_toc(blocks: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    sections: dict[tuple[str, ...], dict[str, Any]] = {}
    for block in blocks:
        raw_path = block.get("section_path")
        path = tuple(str(item) for item in raw_path) if isinstance(raw_path, list) else ()
        if not path:
            continue
        entry = sections.setdefault(
            path,
            {
                "section_path": list(path),
                "title": path[-1],
                "page_start": block.get("page_start"),
                "page_end": block.get("page_end"),
                "block_count": 0,
            },
        )
        entry["block_count"] += 1
        page_end = block.get("page_end")
        if isinstance(page_end, int):
            entry["page_end"] = max(int(entry.get("page_end") or page_end), page_end)
    return list(sections.values())


def _project_item_matches(
    item: Mapping[str, Any],
    stage_id: str,
    related_objects: Mapping[str, set[str]],
) -> bool:
    """Return whether a proposal or receipt belongs in one project timeline."""

    object_type = str(item.get("object_type") or "")
    object_id = str(item.get("object_id") or "")
    if object_id and object_id in related_objects.get(object_type, set()):
        return True
    target = item.get("target")
    target_map = target if isinstance(target, Mapping) else {}
    target_types = {
        "stage_id": "project_stage",
        "project_stage_id": "project_stage",
        "task_id": "task",
        "paper_id": "paper",
        "experiment_id": "experiment",
        "artifact_id": "research_artifact",
    }
    for key, value in target_map.items():
        normalized = str(value or "")
        if normalized == stage_id or normalized in related_objects.get(
            target_types.get(str(key), str(key).removesuffix("_id")),
            set(),
        ):
            return True
    diff = item.get("diff")
    diff_map = diff if isinstance(diff, Mapping) else {}
    stage_change = diff_map.get("project_stage_id")
    stage_change_map = stage_change if isinstance(stage_change, Mapping) else {}
    return str(stage_change_map.get("after") or "") == stage_id


def _receipt_from_apply(
    result: Mapping[str, Any],
    *,
    lane: str,
    decided_by: str,
    decided_at: str,
) -> dict[str, Any]:
    return {
        "proposal_id": result.get("proposal_id"),
        "operation": result.get("operation"),
        "decision": "applied",
        "status": "applied",
        "lane": lane,
        "decided_by": decided_by,
        "decided_at": decided_at,
        "audit_event_id": result.get("audit_event_id"),
        "idempotent_replay": bool(result.get("idempotent_replay")),
        **_receipt_object(result),
    }


def _applied_receipt(
    stored: Mapping[str, Any],
    *,
    lane: str,
) -> dict[str, Any]:
    response = stored.get("response")
    result = response if isinstance(response, Mapping) else stored
    record = result.get("record")
    record_map = record if isinstance(record, Mapping) else {}
    review = record_map.get("review")
    review_map = review if isinstance(review, Mapping) else {}
    decided_at = (
        stored.get("committed_at")
        or result.get("confirmed_at")
        or review_map.get("confirmed_at")
        or record_map.get("updated_at")
        or ""
    )
    return {
        "proposal_id": result.get("proposal_id"),
        "operation": result.get("operation"),
        "decision": "applied",
        "status": "applied",
        "lane": lane,
        "decided_by": review_map.get("confirmed_by"),
        "decided_at": decided_at,
        "audit_event_id": (
            result.get("audit_event_id")
            or stored.get("event_id")
        ),
        "idempotent_replay": False,
        **_receipt_object(result),
    }


def _receipt_object(result: Mapping[str, Any]) -> dict[str, Any]:
    record = result.get("record")
    item = record if isinstance(record, Mapping) else {}
    operation = str(result.get("operation") or "")
    projections = {
        "project_stage.create": ("project_stage", "stage_id", "project_name"),
        "project_stage.update": ("project_stage", "stage_id", "project_name"),
        "task.create": ("task", "task_id", "title"),
        "task.update": ("task", "task_id", "title"),
        "experiment.log": ("experiment", "experiment_id", "title"),
        "paper.intake": ("paper", "paper_id", "title"),
        "research_artifact.publish": ("research_artifact", "artifact_id", "title"),
    }
    projection = projections.get(operation)
    if projection is None:
        return {}
    object_type, id_field, label_field = projection
    object_id = str(item.get(id_field) or "").strip()
    if not object_id:
        return {}
    return {
        "object_type": object_type,
        "object_id": object_id,
        "object_label": str(item.get(label_field) or object_id),
    }


def _rejection_receipt(
    stored: Mapping[str, Any],
    *,
    lane: str,
) -> dict[str, Any]:
    return {
        **dict(stored),
        "decision": "rejected",
        "status": "rejected",
        "lane": lane,
        "decided_at": stored.get("rejected_at"),
    }


def _safe_runtime_status(root_dir: Path) -> dict[str, Any]:
    try:
        return get_pi_host_status(root_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"running": False, "status": "unavailable"}


def _read_optional_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _latest_live_evaluation(eval_root: Path) -> dict[str, Any]:
    if not eval_root.exists():
        return {}
    for path in sorted(
        eval_root.glob("*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    ):
        payload = _read_optional_json(path)
        if payload.get("mode") != "live" or payload.get("status") == "running":
            continue
        return {
            **payload,
            "report_name": path.name,
            "updated_at": datetime.fromtimestamp(
                path.stat().st_mtime,
                tz=timezone.utc,
            ).isoformat(),
        }
    return {}


def _automation_message_view(item: Mapping[str, Any]) -> dict[str, Any]:
    """Expose workflow metadata without leaking arbitrary gateway payloads."""

    return {
        "message_id": str(item.get("message_id") or ""),
        "workflow_type": str(item.get("workflow_type") or "scheduled_workflow"),
        "skill_name": str(item.get("skill_name") or ""),
        "instruction": str(item.get("raw_input") or "")[:2000],
        "source": str(item.get("source") or ""),
        "status": str(item.get("status") or "pending"),
        "received_at": item.get("received_at"),
        "acknowledged_at": item.get("acknowledged_at"),
        "outcome_note": str(item.get("outcome_note") or "")[:1000],
    }


def _string_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for raw in value:
        normalized = str(raw or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        items.append(normalized)
        if len(items) >= limit:
            break
    return items


def _automation_job_view(item: Mapping[str, Any]) -> dict[str, Any]:
    """Expose operational job state without rendering job payloads."""

    return {
        "job_id": str(item.get("job_id") or ""),
        "job_type": str(item.get("job_type") or ""),
        "status": str(item.get("status") or "active"),
        "schedule_mode": str(item.get("schedule_mode") or ""),
        "interval_minutes": item.get("interval_minutes"),
        "next_run_at": item.get("next_run_at"),
        "last_run_at": item.get("last_run_at"),
        "last_status": item.get("last_status"),
        "throttle_reason": str(item.get("throttle_reason") or "")[:500],
    }


def _integration_check(
    check_id: str,
    label: str,
    passed: bool,
    detail: str = "",
    *,
    severity: str = "error",
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "label": label,
        "status": "pass" if passed else severity,
        "detail": str(detail or ""),
    }


def _integration_test_result(
    integration_id: str,
    checks: list[dict[str, Any]],
    *,
    summary: str,
    next_actions: list[str],
) -> dict[str, Any]:
    statuses = {str(item.get("status") or "error") for item in checks}
    status = (
        "fail"
        if "error" in statuses
        else "attention"
        if "attention" in statuses
        else "pass"
    )
    return {
        "integration_id": integration_id,
        "tested_at": _now(),
        "status": status,
        "summary": summary,
        "checks": checks,
        "next_actions": next_actions,
        "read_only": True,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized_doi(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    return normalized.strip()


def _normalized_title(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _payload_bool(
    payload: Mapping[str, Any],
    name: str,
    *,
    default: bool,
) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise MutationError(
            "invalid_argument",
            f"{name} must be a JSON boolean",
        )
    return value


def _path_segment(
    path: str,
    *,
    prefix: str,
    suffix: str = "",
) -> str:
    value = path.removeprefix(prefix)
    if suffix:
        value = value.removesuffix(suffix)
    normalized = unquote(value.strip("/"))
    if not normalized or "/" in normalized:
        raise MutationError("invalid_argument", "route identifier is invalid")
    return normalized


def _query_int(query: str, name: str, *, default: int) -> int:
    raw = (parse_qs(query).get(name) or [str(default)])[0]
    try:
        value = int(raw)
    except ValueError as exc:
        raise MutationError(
            "invalid_argument",
            f"{name} must be an integer",
        ) from exc
    return max(0, value)


def _query_params(raw_path: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(raw_path).query)


def _first(
    params: Mapping[str, list[str]],
    name: str,
) -> str | None:
    value = (params.get(name) or [None])[0]
    normalized = str(value or "").strip()
    return normalized or None


def _param_int(
    params: Mapping[str, list[str]],
    name: str,
    *,
    default: int,
) -> int:
    raw = _first(params, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise MutationError(
            "invalid_argument",
            f"{name} must be an integer",
        ) from exc


def build_server(
    data_dir: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    agent_sessions: WorkbenchAgentSessionService | None = None,
) -> WorkbenchServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("workbench API must bind to a loopback host")
    if port < 0 or port > 65535:
        raise ValueError("port must be between 0 and 65535")
    return WorkbenchServer(
        (host, port),
        WorkbenchApplication(
            Path(data_dir),
            agent_sessions=agent_sessions,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    server = build_server(
        Path(args.data_dir),
        host=args.host,
        port=args.port,
    )
    print(
        json.dumps({
            "status": "ready",
            "url": f"http://{args.host}:{args.port}",
            "loopback_only": True,
        }),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
