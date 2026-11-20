"""Controlled Obsidian sync helpers built on top of KB Constitution Layer."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .contracts import KBWriteDecision, KBWriteRequest
from .exporters import (
    export_experiment_markdown,
    export_paper_markdown,
    export_project_stage_markdown,
    export_research_artifact_markdown,
    export_task_markdown,
)
from .kb_constitution import plan_kb_write, write_kb_note
from .storage import ScholarFlowStore


def build_object_kb_request(
    store: ScholarFlowStore,
    *,
    object_type: str,
    object_id: str,
    content_kind: str,
    title: Optional[str] = None,
    target_path: Optional[str] = None,
    scope: str = "project",
    related_links: Optional[list[str]] = None,
    source_paths: Optional[list[str]] = None,
    journal_required: bool = False,
) -> KBWriteRequest:
    body, resolved_title = _export_object(store, object_type, object_id, title, content_kind)
    return KBWriteRequest(
        title=resolved_title,
        content_kind=content_kind,
        body=body,
        source_context={"scope": scope, "journal_required": journal_required},
        target_path=target_path,
        related_links=list(related_links or []),
        source_paths=list(source_paths or [f"scholarflow:{object_type}:{object_id}"]),
        side_effects_acknowledged=[],
    )


def sync_object_to_vault(
    vault_root: Path,
    store: ScholarFlowStore,
    *,
    object_type: str,
    object_id: str,
    content_kind: str,
    title: Optional[str] = None,
    target_path: Optional[str] = None,
    scope: str = "project",
    related_links: Optional[list[str]] = None,
    source_paths: Optional[list[str]] = None,
    journal_required: bool = False,
    auto_side_effects: bool = True,
) -> KBWriteDecision:
    request = build_object_kb_request(
        store,
        object_type=object_type,
        object_id=object_id,
        content_kind=content_kind,
        title=title,
        target_path=target_path,
        scope=scope,
        related_links=related_links,
        source_paths=source_paths,
        journal_required=journal_required,
    )
    return write_kb_note(vault_root, request, auto_side_effects=auto_side_effects)


def plan_object_sync(
    vault_root: Path,
    store: ScholarFlowStore,
    *,
    object_type: str,
    object_id: str,
    content_kind: str,
    title: Optional[str] = None,
    target_path: Optional[str] = None,
    scope: str = "project",
    related_links: Optional[list[str]] = None,
    source_paths: Optional[list[str]] = None,
    journal_required: bool = False,
) -> KBWriteDecision:
    request = build_object_kb_request(
        store,
        object_type=object_type,
        object_id=object_id,
        content_kind=content_kind,
        title=title,
        target_path=target_path,
        scope=scope,
        related_links=related_links,
        source_paths=source_paths,
        journal_required=journal_required,
    )
    return plan_kb_write(vault_root, request, auto_side_effects=True)


def _export_object(
    store: ScholarFlowStore,
    object_type: str,
    object_id: str,
    title: Optional[str],
    content_kind: str,
) -> tuple[str, str]:
    if object_type == "experiment":
        record = store.get_experiment(object_id)
        if record is None:
            raise ValueError(f"experiment '{object_id}' was not found")
        return export_experiment_markdown(record, content_kind=content_kind), title or record.title or record.experiment_id
    if object_type == "task":
        record = store.get_task(object_id)
        if record is None:
            raise ValueError(f"task '{object_id}' was not found")
        return export_task_markdown(record, content_kind=content_kind), title or record.title or record.task_id
    if object_type == "project_stage":
        record = store.get_project_stage(object_id)
        if record is None:
            raise ValueError(f"project_stage '{object_id}' was not found")
        return (
            export_project_stage_markdown(record, content_kind=content_kind),
            title or record.title or record.stage_id,
        )
    if object_type == "paper":
        record = store.get_paper(object_id)
        if record is None:
            raise ValueError(f"paper '{object_id}' was not found")
        return (
            export_paper_markdown(record, content_kind=content_kind),
            title or record.title or record.paper_id,
        )
    if object_type == "research_artifact":
        record = store.get_research_artifact(object_id)
        if record is None:
            raise ValueError(f"research_artifact '{object_id}' was not found")
        return (
            export_research_artifact_markdown(record, content_kind=content_kind),
            title or record.title or record.artifact_id,
        )
    raise ValueError(f"unsupported object_type: {object_type}")
