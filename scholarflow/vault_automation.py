"""Recurring Obsidian/vault maintenance helpers for ScholarFlow."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .kb_constitution import write_log_only_entry
from .obsidian_sync import plan_object_sync, sync_object_to_vault
from .paper_service import summarize_collection
from .research_workflow import infer_default_mind_map_topic, write_topic_mind_map
from .storage import ScholarFlowStore


def run_kb_backfill(
    store: ScholarFlowStore,
    *,
    vault_root: Path,
    include_tasks: bool,
    overwrite_existing: bool,
    include_papers: bool = True,
    source_tag: str = "kb_backfill",
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for object_type, object_id, content_kind, title in iter_kb_backfill_candidates(
        store,
        include_tasks=include_tasks,
        include_papers=include_papers,
    ):
        object_ref = f"{object_type}:{object_id}"
        source_paths = [f"scholarflow:{source_tag}:{object_ref}"]
        if object_type == "task":
            result = write_log_only_entry(
                vault_root,
                title=title,
                reason="KB backfill recorded an operational task as log-only context.",
                object_ref=object_ref,
                source_paths=source_paths,
            )
            items.append(
                {
                    "object_ref": object_ref,
                    "content_kind": content_kind,
                    "status": "logged_only",
                    "decision": result,
                }
            )
            continue
        if object_type == "paper" and content_kind == "log_only":
            result = write_log_only_entry(
                vault_root,
                title=title,
                reason="KB backfill recorded a paper stub as log-only context.",
                object_ref=object_ref,
                source_paths=source_paths,
            )
            items.append(
                {
                    "object_ref": object_ref,
                    "content_kind": "log_only",
                    "status": "logged_only",
                    "decision": result,
                }
            )
            continue

        decision = plan_object_sync(
            vault_root,
            store,
            object_type=object_type,
            object_id=object_id,
            content_kind=content_kind,
            title=title,
            scope=source_tag,
            related_links=["[[wiki/entities/ScholarFlow]]"],
            source_paths=source_paths,
        )
        target_path = Path(decision.target_path)
        if target_path.exists() and not overwrite_existing:
            items.append(
                {
                    "object_ref": object_ref,
                    "content_kind": content_kind,
                    "status": "skipped_existing",
                    "decision": decision.to_dict(),
                }
            )
            continue
        if not decision.allowed:
            items.append(
                {
                    "object_ref": object_ref,
                    "content_kind": content_kind,
                    "status": "blocked",
                    "decision": decision.to_dict(),
                }
            )
            continue
        applied = sync_object_to_vault(
            vault_root,
            store,
            object_type=object_type,
            object_id=object_id,
            content_kind=content_kind,
            title=title,
            scope=source_tag,
            related_links=["[[wiki/entities/ScholarFlow]]"],
            source_paths=source_paths,
            auto_side_effects=True,
        )
        items.append(
            {
                "object_ref": object_ref,
                "content_kind": content_kind,
                "status": "written",
                "decision": applied.to_dict(),
            }
        )
    return {
        "vault_root": str(vault_root),
        "count": len(items),
        "written_count": sum(1 for item in items if item["status"] == "written"),
        "logged_only_count": sum(1 for item in items if item["status"] == "logged_only"),
        "skipped_existing_count": sum(1 for item in items if item["status"] == "skipped_existing"),
        "blocked_count": sum(1 for item in items if item["status"] == "blocked"),
        "items": items,
    }


def maintain_vault_views(
    store: ScholarFlowStore,
    *,
    vault_root: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    effective_now = now or datetime.now(timezone.utc)
    hub_path = vault_root / "wiki" / "entities" / "ScholarFlow.md"
    index_path = vault_root / "wiki" / "reference" / "ScholarFlow Knowledge Index.md"
    paper_index_path = vault_root / "wiki" / "reference" / "Paper Topic Index.md"
    mind_map_topic = infer_default_mind_map_topic(store)

    stages = store.list_project_stages()
    experiments = store.list_experiments()
    papers = store.list_papers()
    note_sections = _scan_vault_note_sections(vault_root)
    hub_body = _build_hub_body(stages, experiments, papers, note_sections, effective_now, mind_map_topic=mind_map_topic)
    index_body = _build_index_body(note_sections, effective_now)
    paper_index_body = _build_paper_topic_index(papers, effective_now)
    mind_map_result = write_topic_mind_map(store, vault_root=vault_root, topic=mind_map_topic)

    return {
        "hub": _write_generated_markdown(hub_path, hub_body),
        "index": _write_generated_markdown(index_path, index_body),
        "paper_index": _write_generated_markdown(paper_index_path, paper_index_body),
        "mind_map": mind_map_result,
    }


def append_daily_digest(
    store: ScholarFlowStore,
    *,
    vault_root: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    effective_now = now or datetime.now(timezone.utc)
    current_date = effective_now.date().isoformat()
    journal_path = resolve_journal_path(vault_root, now=effective_now)
    journal_path.parent.mkdir(parents=True, exist_ok=True)

    active_stages = [
        stage
        for stage in store.list_project_stages()
        if str(stage.status.value).lower() in {"active", "blocked", "completed"}
    ]
    recent_experiments = sorted(
        store.list_experiments(),
        key=lambda item: item.updated_at,
        reverse=True,
    )[:5]
    marker = f"## ScholarFlow Daily Digest - {current_date}"
    lines = [marker, ""]
    if active_stages:
        lines.append("### Project Stages")
        for stage in active_stages[:8]:
            next_action = stage.next_actions[0] if stage.next_actions else "无"
            lines.append(f"- {stage.title} | {stage.status.value} | next: {next_action}")
        lines.append("")
    if recent_experiments:
        lines.append("### Experiments")
        for experiment in recent_experiments:
            outcome = experiment.result_summary or experiment.failure_reason or experiment.status.value
            lines.append(f"- {experiment.title} | {experiment.status.value} | {outcome}")
        lines.append("")
    if len(lines) == 2:
        lines.append("- 当前没有可汇总的 stage / experiment 记录。")
        lines.append("")
    block = "\n".join(lines).rstrip() + "\n"

    if journal_path.exists():
        content = journal_path.read_text(encoding="utf-8")
        if marker in content:
            return {
                "target_path": str(journal_path),
                "status": "up_to_date",
                "marker": marker,
            }
        suffix = "" if content.endswith("\n") else "\n"
        journal_path.write_text(content + suffix + "\n" + block, encoding="utf-8")
    else:
        journal_path.write_text(f"# {current_date} work log\n\n{block}", encoding="utf-8")
    return {
        "target_path": str(journal_path),
        "status": "written",
        "marker": marker,
    }


def resolve_journal_path(vault_root: Path, *, now: datetime | None = None) -> Path:
    effective_now = now or datetime.now(timezone.utc)
    current_date = effective_now.date().isoformat()
    return vault_root / "30_Journal" / f"{current_date} work log.md"


def iter_kb_backfill_candidates(
    store: ScholarFlowStore,
    *,
    include_tasks: bool,
    include_papers: bool = True,
) -> list[tuple[str, str, str, str]]:
    candidates: list[tuple[str, str, str, str]] = []
    for stage in store.list_project_stages():
        record = stage.to_dict()
        candidates.append(
            (
                "project_stage",
                stage.stage_id,
                infer_project_stage_backfill_kind(record),
                stage.title or stage.stage_id,
            )
        )
    for experiment in store.list_experiments():
        record = experiment.to_dict()
        candidates.append(
            (
                "experiment",
                experiment.experiment_id,
                infer_experiment_backfill_kind(record),
                experiment.title or experiment.experiment_id,
            )
        )
    if include_tasks:
        for task in store.list_tasks():
            candidates.append(("task", task.task_id, "reference", task.title or task.task_id))
    if include_papers:
        for paper in store.list_papers():
            kind = infer_paper_backfill_kind(paper.to_dict())
            candidates.append(
                (
                    "paper",
                    paper.paper_id,
                    kind,
                    paper.title or paper.paper_id,
                )
            )
    return candidates


def infer_paper_backfill_kind(record: dict[str, Any]) -> str:
    has_authors = bool(record.get("authors"))
    has_summary = bool(record.get("summary") or record.get("relevance"))
    has_links = bool(record.get("related_tasks") or record.get("related_experiments"))
    status = str(record.get("status") or "").lower()
    if not has_authors and not has_summary and not has_links and status in {"to_read", "reading"}:
        return "log_only"
    if has_summary or has_links or status in {"done", "archived"}:
        return "synthesis"
    return "reference"


def infer_project_stage_backfill_kind(record: dict[str, Any]) -> str:
    status = str(record.get("status") or "").lower()
    has_progress = bool(record.get("progress_note"))
    has_next_actions = bool(record.get("next_actions"))
    has_risks = bool(record.get("risks"))
    has_links = bool(record.get("related_tasks") or record.get("related_experiments"))
    if status == "not_started" and not has_progress and not has_next_actions and not has_risks and not has_links:
        return "spec"
    if has_progress or has_next_actions or has_risks or has_links or status in {"active", "blocked", "completed"}:
        return "synthesis"
    return "reference"


def infer_experiment_backfill_kind(record: dict[str, Any]) -> str:
    status = str(record.get("status") or "").lower()
    has_outcome = bool(record.get("result_summary") or record.get("failure_reason"))
    has_plan_shape = bool(record.get("goal") or record.get("variables") or record.get("metrics"))
    if status in {"planned", "queued", "running"} and has_plan_shape and not has_outcome:
        return "spec"
    return "reference"


def _scan_vault_note_sections(vault_root: Path) -> dict[str, list[str]]:
    wiki_root = vault_root / "wiki"
    sections: dict[str, list[str]] = {}
    if not wiki_root.exists():
        return sections
    for path in sorted(wiki_root.rglob("*.md")):
        relative = path.relative_to(vault_root).as_posix()
        if relative == "wiki/log.md":
            continue
        section = path.parent.name
        sections.setdefault(section, []).append(relative)
    return sections


def _build_hub_body(
    stages: list[Any],
    experiments: list[Any],
    papers: list[Any],
    note_sections: dict[str, list[str]],
    now: datetime,
    *,
    mind_map_topic: str,
) -> str:
    lines = [
        "---",
        'title: "ScholarFlow"',
        "type: entity",
        "status: active",
        "---",
        "",
        "# ScholarFlow",
        "",
        "一句话定义：面向研究生日常管理、记忆治理与知识沉淀的长期代理内核。",
        "",
        f"- Generated at: {now.isoformat()}",
        f"- Project stages: {len(stages)}",
        f"- Experiments: {len(experiments)}",
        f"- Papers: {len(papers)}",
        f"- Vault note sections: {len(note_sections)}",
        "",
        "## Research Overview",
        f"- [[wiki/concepts/{mind_map_topic} Mind Map.md]]",
        "",
        "## Active Stages",
    ]
    if stages:
        for stage in stages[:10]:
            lines.append(f"- [[wiki/syntheses/{stage.title}.md]] | {stage.status.value} | {stage.goal}")
    else:
        lines.append("- 暂无阶段记录。")
    lines.extend(["", "## Experiments"])
    if experiments:
        for experiment in experiments[:10]:
            lines.append(
                f"- [[wiki/reference/{experiment.title}.md]] | {experiment.status.value} | {experiment.goal}"
            )
    else:
        lines.append("- 暂无实验记录。")
    lines.extend(["", "## Paper Library"])
    if papers:
        status_counts = Counter(str(paper.status.value) for paper in papers)
        lines.append("- 状态分布：" + " / ".join(f"{key}={value}" for key, value in sorted(status_counts.items())))
        for paper in papers[:10]:
            lines.append(
                f"- [[wiki/reference/{paper.title}.md]] | {paper.status.value} | {paper.year or 'unknown'}"
            )
    else:
        lines.append("- 暂无文献记录。")
    lines.extend(["", "## Vault Sections"])
    if note_sections:
        for section, items in sorted(note_sections.items()):
            lines.append(f"- {section}: {len(items)}")
    else:
        lines.append("- 暂无知识页。")
    lines.append("")
    return "\n".join(lines)


def _build_paper_topic_index(papers: list[Any], now: datetime) -> str:
    if not papers:
        return "# Paper Topic Index\n\n- 暂无文献记录。\n"
    summary = summarize_collection(
        {
            "item_key": _extract_paper_item_key(paper.paper_id),
            "item_type": "paper",
            "title": paper.title,
            "year": paper.year,
            "authors": list(paper.authors),
            "tags": _tags_from_paper(paper),
        }
        for paper in papers
    )
    status_counts = Counter(str(paper.status.value) for paper in papers)
    lines = [
        "# Paper Topic Index",
        "",
        f"- Generated at: {now.isoformat()}",
        f"- Total papers: {summary['count']}",
        f"- Status distribution: "
        + " / ".join(f"{key}={value}" for key, value in sorted(status_counts.items())),
        "",
        "## Top Years",
    ]
    if summary["top_years"]:
        for entry in summary["top_years"]:
            lines.append(f"- {entry['name']} ({entry['count']})")
    else:
        lines.append("- 暂无年代数据。")
    lines.extend(["", "## Top Tags"])
    if summary["top_tags"]:
        for entry in summary["top_tags"]:
            lines.append(f"- {entry['name']} ({entry['count']})")
    else:
        lines.append("- 暂无 tag 数据。")
    lines.extend(["", "## Top Authors"])
    if summary["top_authors"]:
        for entry in summary["top_authors"]:
            lines.append(f"- {entry['name']} ({entry['count']})")
    else:
        lines.append("- 暂无作者数据。")
    lines.extend(["", "## Paper List"])
    for paper in papers:
        marker = f"{paper.status.value} | {paper.year or 'unknown'}"
        lines.append(f"- {paper.title} | {marker} | {paper.paper_id}")
    lines.append("")
    return "\n".join(lines)


def _extract_paper_item_key(paper_id: str) -> str:
    if paper_id.startswith("zotero:"):
        return paper_id.split(":", 1)[1]
    return paper_id


def _tags_from_paper(paper: Any) -> list[str]:
    source = getattr(paper, "source", None) or ""
    if "#tags=" not in source:
        return []
    tail = source.split("#tags=", 1)[1]
    return [tag for tag in tail.split(",") if tag]


def _build_index_body(note_sections: dict[str, list[str]], now: datetime) -> str:
    lines = [
        "# ScholarFlow Knowledge Index",
        "",
        f"- Generated at: {now.isoformat()}",
        "",
    ]
    if not note_sections:
        lines.append("- 暂无知识页。")
        lines.append("")
        return "\n".join(lines)
    for section, items in sorted(note_sections.items()):
        lines.append(f"## {section}")
        lines.append("")
        for item in items:
            lines.append(f"- [[{item}]]")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_generated_markdown(target_path: Path, content: str) -> dict[str, Any]:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    status = "written"
    if target_path.exists() and target_path.read_text(encoding="utf-8") == content:
        status = "up_to_date"
    else:
        target_path.write_text(content, encoding="utf-8")
    return {
        "target_path": str(target_path),
        "status": status,
    }
