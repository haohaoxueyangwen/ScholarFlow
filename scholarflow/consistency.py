"""Cross-store referential consistency checks for the Pi-native domain."""

from __future__ import annotations

from typing import Any, Iterable

from .storage import ScholarFlowStore


def audit_domain_consistency(store: ScholarFlowStore) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    stages = {item.stage_id for item in store.list_project_stages()}
    tasks = {item.task_id for item in store.list_tasks()}
    experiments = {item.experiment_id for item in store.list_experiments()}
    papers = {item.paper_id for item in store.list_papers()}
    artifacts = {item.artifact_id for item in store.list_research_artifacts()}
    relations = {item.relation_id for item in store.list_knowledge_relations()}
    ids = {
        "project_stage": stages,
        "stage": stages,
        "task": tasks,
        "experiment": experiments,
        "paper": papers,
        "research_artifact": artifacts,
        "artifact": artifacts,
        "knowledge_relation": relations,
        "relation": relations,
    }

    def require(owner: str, field: str, refs: Iterable[str], expected: set[str]) -> None:
        for ref in refs:
            if ref not in expected:
                issues.append({"severity": "warn", "owner": owner, "field": field, "missing_ref": ref})

    for stage in store.list_project_stages():
        require(f"project_stage:{stage.stage_id}", "related_tasks", stage.related_tasks, tasks)
        require(f"project_stage:{stage.stage_id}", "related_experiments", stage.related_experiments, experiments)
    for task in store.list_tasks():
        require(f"task:{task.task_id}", "project_stage_id", [task.project_stage_id] if task.project_stage_id else [], stages)
        require(f"task:{task.task_id}", "related_experiments", task.related_experiments, experiments)
        require(f"task:{task.task_id}", "related_papers", task.related_papers, papers)
    for experiment in store.list_experiments():
        require(f"experiment:{experiment.experiment_id}", "project_stage_id", [experiment.project_stage_id] if experiment.project_stage_id else [], stages)
        require(f"experiment:{experiment.experiment_id}", "related_tasks", experiment.related_tasks, tasks)
    for paper in store.list_papers():
        require(f"paper:{paper.paper_id}", "related_tasks", paper.related_tasks, tasks)
        require(f"paper:{paper.paper_id}", "related_experiments", paper.related_experiments, experiments)
    for artifact in store.list_research_artifacts():
        require(f"research_artifact:{artifact.artifact_id}", "project_stage_id", [artifact.project_stage_id] if artifact.project_stage_id else [], stages)
        require(f"research_artifact:{artifact.artifact_id}", "related_tasks", artifact.related_tasks, tasks)
        require(f"research_artifact:{artifact.artifact_id}", "related_experiments", artifact.related_experiments, experiments)
        require(f"research_artifact:{artifact.artifact_id}", "related_papers", artifact.related_papers, papers)
        _check_typed_refs(issues, ids, f"research_artifact:{artifact.artifact_id}", "evidence_refs", artifact.evidence_refs)
    for relation in store.list_knowledge_relations():
        _check_typed_refs(issues, ids, f"knowledge_relation:{relation.relation_id}", "subject_ref", [relation.subject_ref])
        _check_typed_refs(issues, ids, f"knowledge_relation:{relation.relation_id}", "object_ref", [relation.object_ref])
        _check_typed_refs(issues, ids, f"knowledge_relation:{relation.relation_id}", "evidence_refs", relation.evidence_refs)

    return {
        "status": "ok" if not issues else "warn",
        "object_counts": {
            "project_stages": len(stages),
            "tasks": len(tasks),
            "experiments": len(experiments),
            "papers": len(papers),
            "research_artifacts": len(artifacts),
            "knowledge_relations": len(relations),
        },
        "issue_count": len(issues),
        "issues": issues,
    }


def _check_typed_refs(
    issues: list[dict[str, Any]],
    ids: dict[str, set[str]],
    owner: str,
    field: str,
    refs: Iterable[str],
) -> None:
    for ref in refs:
        text = str(ref).strip()
        if not text or "://" in text or ":" not in text:
            continue
        ref_type, _, ref_id = text.partition(":")
        known = ids.get(ref_type)
        if known is not None and ref_id not in known:
            issues.append({"severity": "warn", "owner": owner, "field": field, "missing_ref": text})
