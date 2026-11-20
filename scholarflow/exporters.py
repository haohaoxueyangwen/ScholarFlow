"""Markdown exporters for ScholarFlow Wave 1 objects."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .models import Experiment, Paper, ProjectStage, ResearchArtifact, Task


def export_experiment_markdown(experiment: Experiment, *, content_kind: str = "reference") -> str:
    if content_kind == "spec":
        return _export_experiment_spec_markdown(experiment)
    lines = [
        f"# Experiment {experiment.experiment_id}",
        "",
        f"- Title: {experiment.title}",
        f"- Goal: {experiment.goal}",
        f"- Status: {experiment.status.value}",
        f"- Project Stage: {experiment.project_stage_id or ''}",
        f"- Related Tasks: {', '.join(experiment.related_tasks)}",
        f"- Updated At: {experiment.updated_at}",
    ]
    if experiment.result_summary:
        lines.extend(["", "## Result Summary", "", experiment.result_summary])
    if experiment.failure_reason:
        lines.extend(["", "## Failure Reason", "", experiment.failure_reason])
    if experiment.next_actions:
        lines.extend(["", "## Next Actions", ""] + [f"- {item}" for item in experiment.next_actions])
    if experiment.command or experiment.server or experiment.log_path or experiment.checkpoint_path:
        lines.extend(["", "## Runtime References", ""])
        if experiment.command:
            lines.append(f"- Command: {experiment.command}")
        if experiment.server:
            lines.append(f"- Server: {experiment.server}")
        if experiment.log_path:
            lines.append(f"- Log Path: {experiment.log_path}")
        if experiment.checkpoint_path:
            lines.append(f"- Checkpoint Path: {experiment.checkpoint_path}")
    if experiment.variables:
        lines.extend(["", "## Variables", ""])
        lines.extend(_render_mapping_items(experiment.variables))
    if experiment.metrics:
        lines.extend(["", "## Metrics", ""])
        lines.extend(_render_mapping_items(experiment.metrics))
    return "\n".join(lines).rstrip() + "\n"


def export_task_markdown(task: Task, *, content_kind: str = "reference") -> str:
    lines = [
        f"# Task {task.task_id}",
        "",
        f"- Title: {task.title}",
        f"- Status: {task.status.value}",
        f"- Project Stage: {task.project_stage_id or ''}",
        f"- Priority: {task.priority or ''}",
        f"- Source: {task.source or ''}",
        f"- Updated At: {task.updated_at}",
    ]
    if task.description:
        lines.extend(["", "## Description", "", task.description])
    if task.related_experiments:
        lines.extend(["", "## Related Experiments", ""] + [f"- {item}" for item in task.related_experiments])
    return "\n".join(lines).rstrip() + "\n"


def export_project_stage_markdown(stage: ProjectStage, *, content_kind: str = "reference") -> str:
    if content_kind == "synthesis":
        return _export_project_stage_synthesis_markdown(stage)
    if content_kind == "spec":
        return _export_project_stage_spec_markdown(stage)
    lines = [
        f"# Project Stage {stage.stage_id}",
        "",
        f"- Project: {stage.project_name}",
        f"- Title: {stage.title}",
        f"- Goal: {stage.goal}",
        f"- Status: {stage.status.value}",
        f"- Updated At: {stage.updated_at}",
    ]
    if stage.progress_note:
        lines.extend(["", "## Progress Note", "", stage.progress_note])
    if stage.related_tasks:
        lines.extend(["", "## Related Tasks", ""] + [f"- {item}" for item in stage.related_tasks])
    if stage.related_experiments:
        lines.extend(["", "## Related Experiments", ""] + [f"- {item}" for item in stage.related_experiments])
    if stage.next_actions:
        lines.extend(["", "## Next Actions", ""] + [f"- {item}" for item in stage.next_actions])
    return "\n".join(lines).rstrip() + "\n"


def export_collection_markdown(title: str, items: Iterable[str]) -> str:
    body = [f"# {title}", ""]
    body.extend(items)
    return "\n".join(body).rstrip() + "\n"


def export_paper_markdown(paper: Paper, *, content_kind: str = "reference") -> str:
    if content_kind == "spec":
        return _export_paper_spec_markdown(paper)
    if content_kind == "synthesis":
        return _export_paper_synthesis_markdown(paper)
    lines = [
        f"# Paper {paper.paper_id}",
        "",
        f"- Title: {paper.title}",
        f"- Source: {paper.source or ''}",
        f"- Year: {paper.year if paper.year is not None else ''}",
        f"- Status: {paper.status.value}",
        f"- Authors: {', '.join(paper.authors)}",
    ]
    if paper.summary:
        lines.extend(["", "## Summary", "", paper.summary])
    if paper.relevance:
        lines.extend(["", "## Relevance", "", paper.relevance])
    if paper.related_tasks:
        lines.extend(["", "## Related Tasks", ""] + [f"- {item}" for item in paper.related_tasks])
    if paper.related_experiments:
        lines.extend(
            ["", "## Related Experiments", ""] + [f"- {item}" for item in paper.related_experiments]
        )
    if paper.obsidian_note:
        lines.extend(["", f"## Obsidian Note", "", paper.obsidian_note])
    return "\n".join(lines).rstrip() + "\n"


def export_research_artifact_markdown(artifact: ResearchArtifact, *, content_kind: str = "synthesis") -> str:
    lines = [
        f"# {artifact.title}",
        "",
        f"- Artifact ID: {artifact.artifact_id}",
        f"- Kind: {artifact.kind.value}",
        f"- Status: {artifact.status.value}",
        f"- Project Stage: {artifact.project_stage_id or ''}",
        f"- Period: {artifact.period_start or ''} — {artifact.period_end or ''}",
        f"- Updated At: {artifact.updated_at}",
        "",
        artifact.content,
    ]
    if artifact.evidence_refs:
        lines.extend(["", "## Evidence", ""] + [f"- {item}" for item in artifact.evidence_refs])
    related = (
        [("Paper", item) for item in artifact.related_papers]
        + [("Task", item) for item in artifact.related_tasks]
        + [("Experiment", item) for item in artifact.related_experiments]
    )
    if related:
        lines.extend(["", "## Related records", ""] + [f"- {kind}: {item}" for kind, item in related])
    return "\n".join(lines).rstrip() + "\n"


def _export_experiment_spec_markdown(experiment: Experiment) -> str:
    lines = [
        f"# {experiment.title}",
        "",
        "## 输入",
        "",
        f"- experiment_id: {experiment.experiment_id}",
        f"- goal: {experiment.goal}",
        f"- project_stage_id: {experiment.project_stage_id or ''}",
        f"- status: {experiment.status.value}",
    ]
    if experiment.related_tasks:
        lines.extend(["", "## 依赖任务", ""] + [f"- {item}" for item in experiment.related_tasks])
    if experiment.variables:
        lines.extend(["", "## 输入字段", ""])
        lines.extend(_render_mapping_items(experiment.variables))
    if experiment.metrics:
        lines.extend(["", "## 输出字段", ""])
        lines.extend(_render_mapping_items(experiment.metrics))
    if experiment.next_actions:
        lines.extend(["", "## 下一步", ""] + [f"- {item}" for item in experiment.next_actions])
    return "\n".join(lines).rstrip() + "\n"


def _export_project_stage_synthesis_markdown(stage: ProjectStage) -> str:
    lines = [
        f"# {stage.title}",
        "",
        f"- Project: {stage.project_name}",
        f"- Stage ID: {stage.stage_id}",
        f"- Status: {stage.status.value}",
        f"- Updated At: {stage.updated_at}",
        "",
        "## 决策摘要",
        "",
        stage.progress_note or stage.goal,
    ]
    if stage.risks:
        lines.extend(["", "## 当前阻塞", ""] + [f"- {item}" for item in stage.risks])
    if stage.next_actions:
        lines.extend(["", "## 优先级与下一步", ""] + [f"- {item}" for item in stage.next_actions])
    if stage.related_tasks or stage.related_experiments:
        lines.extend(["", "## 依赖与关联", ""])
        if stage.related_tasks:
            lines.extend([f"- Task: {item}" for item in stage.related_tasks])
        if stage.related_experiments:
            lines.extend([f"- Experiment: {item}" for item in stage.related_experiments])
    return "\n".join(lines).rstrip() + "\n"


def _export_project_stage_spec_markdown(stage: ProjectStage) -> str:
    lines = [
        f"# {stage.title}",
        "",
        "## 输入",
        "",
        f"- project_name: {stage.project_name}",
        f"- stage_id: {stage.stage_id}",
        "",
        "## 输出",
        "",
        stage.goal,
    ]
    if stage.next_actions:
        lines.extend(["", "## 下一步", ""] + [f"- {item}" for item in stage.next_actions])
    return "\n".join(lines).rstrip() + "\n"


def _export_paper_spec_markdown(paper: Paper) -> str:
    lines = [
        f"# {paper.title}",
        "",
        "## 输入",
        "",
        f"- paper_id: {paper.paper_id}",
        f"- source: {paper.source or ''}",
        f"- year: {paper.year if paper.year is not None else ''}",
        f"- status: {paper.status.value}",
    ]
    if paper.authors:
        lines.extend(["", "## 作者", ""] + [f"- {author}" for author in paper.authors])
    if paper.related_tasks or paper.related_experiments:
        lines.extend(["", "## 关联", ""])
        if paper.related_tasks:
            lines.extend([f"- Task: {item}" for item in paper.related_tasks])
        if paper.related_experiments:
            lines.extend([f"- Experiment: {item}" for item in paper.related_experiments])
    return "\n".join(lines).rstrip() + "\n"


def _export_paper_synthesis_markdown(paper: Paper) -> str:
    lines = [
        f"# {paper.title}",
        "",
        f"- Paper ID: {paper.paper_id}",
        f"- Source: {paper.source or ''}",
        f"- Year: {paper.year if paper.year is not None else ''}",
        f"- Status: {paper.status.value}",
        "",
        "## 摘要",
        "",
        paper.summary or paper.relevance or "（暂无摘要）",
    ]
    if paper.authors:
        lines.extend(["", "## 作者", ""] + [f"- {author}" for author in paper.authors])
    if paper.relevance:
        lines.extend(["", "## 与本研究方向的关系", "", paper.relevance])
    if paper.related_tasks or paper.related_experiments:
        lines.extend(["", "## 关联", ""])
        if paper.related_tasks:
            lines.extend([f"- Task: {item}" for item in paper.related_tasks])
        if paper.related_experiments:
            lines.extend([f"- Experiment: {item}" for item in paper.related_experiments])
    return "\n".join(lines).rstrip() + "\n"


def _render_mapping_items(values: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, value in values.items():
        lines.append(f"- {key}: {value}")
    return lines
