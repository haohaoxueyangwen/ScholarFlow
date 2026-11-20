"""Research-oriented helpers inspired by perspective-guided agent systems."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .storage import ScholarFlowStore

DEFAULT_RESEARCH_PERSPECTIVES = [
    {
        "perspective_id": "advisor",
        "title": "Advisor",
        "questions": [
            "这个方向的研究目标是否足够清晰？",
            "当前阶段最缺的证据是什么？",
        ],
    },
    {
        "perspective_id": "reviewer",
        "title": "Reviewer",
        "questions": [
            "这个方案的 novelty 和 baseline 对比是否成立？",
            "还有哪些威胁有效性的角度尚未覆盖？",
        ],
    },
    {
        "perspective_id": "reproducer",
        "title": "Reproducer",
        "questions": [
            "如果别人来复现，最容易卡在哪一步？",
            "哪些实验设置需要显式记录到知识库？",
        ],
    },
    {
        "perspective_id": "baseline_challenger",
        "title": "Baseline Challenger",
        "questions": [
            "当前 baseline 是否过弱？",
            "是否已经准备好最小但可信的对照实验？",
        ],
    },
    {
        "perspective_id": "implementation_owner",
        "title": "Implementation Owner",
        "questions": [
            "本周内最值得落地的实现路径是什么？",
            "哪些任务与文献之间需要建立显式链接？",
        ],
    },
]


def list_research_perspectives() -> dict[str, Any]:
    return {"count": len(DEFAULT_RESEARCH_PERSPECTIVES), "items": list(DEFAULT_RESEARCH_PERSPECTIVES)}


def select_research_perspectives(
    *,
    raw_input: str,
    retrieval_hits: Iterable[Any] | None = None,
    max_items: int = 3,
) -> list[dict[str, Any]]:
    text = str(raw_input or "").lower()
    hits = list(retrieval_hits or [])
    contains_paper_signal = any(
        str(getattr(item, "source_type", "") or getattr(item, "get", lambda *_: "")("source_type")).startswith("paper")
        if not isinstance(item, dict)
        else str(item.get("source_type") or "").startswith("paper")
        for item in hits
    )
    selected: list[dict[str, Any]] = []
    for item in DEFAULT_RESEARCH_PERSPECTIVES:
        perspective_id = item["perspective_id"]
        if perspective_id in {"advisor", "implementation_owner"}:
            selected.append(item)
            continue
        if perspective_id == "reviewer" and ("评测" in text or "review" in text or "baseline" in text or contains_paper_signal):
            selected.append(item)
            continue
        if perspective_id == "reproducer" and ("复现" in text or "reproduce" in text or "实验" in text):
            selected.append(item)
            continue
        if perspective_id == "baseline_challenger" and ("baseline" in text or "对照" in text or "比较" in text):
            selected.append(item)
            continue
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in selected:
        if item["perspective_id"] in seen:
            continue
        seen.add(item["perspective_id"])
        deduped.append(item)
    return deduped[:max_items]


def infer_default_mind_map_topic(store: ScholarFlowStore) -> str:
    stages = store.list_project_stages()
    if stages:
        return f"{stages[0].project_name} Research"
    return "ScholarFlow Research"


def build_topic_mind_map(store: ScholarFlowStore, *, topic: str, max_items: int = 5) -> str:
    normalized_topic = str(topic or "").strip() or "Research Topic"
    papers = _match_records(
        (
            {
                "label": paper.title,
                "detail": f"{paper.paper_id} | {paper.status.value} | {paper.year or 'unknown'}",
                "text": " ".join([paper.title, paper.summary or "", paper.relevance or "", paper.source or ""]),
            }
            for paper in store.list_papers()
        ),
        normalized_topic,
        max_items=max_items,
    )
    experiments = _match_records(
        (
            {
                "label": experiment.title,
                "detail": f"{experiment.experiment_id} | {experiment.status.value}",
                "text": " ".join([experiment.title, experiment.goal, experiment.result_summary or ""]),
            }
            for experiment in store.list_experiments()
        ),
        normalized_topic,
        max_items=max_items,
    )
    tasks = _match_records(
        (
            {
                "label": task.title,
                "detail": f"{task.task_id} | {task.status.value}",
                "text": " ".join([task.title, task.description or "", " ".join(task.related_papers)]),
            }
            for task in store.list_tasks()
        ),
        normalized_topic,
        max_items=max_items,
    )
    stages = _match_records(
        (
            {
                "label": stage.title,
                "detail": f"{stage.stage_id} | {stage.status.value}",
                "text": " ".join([stage.title, stage.goal, stage.progress_note or ""]),
            }
            for stage in store.list_project_stages()
        ),
        normalized_topic,
        max_items=max_items,
    )

    lines = [
        f"# {normalized_topic} Mind Map",
        "",
        "## Perspectives",
    ]
    for item in DEFAULT_RESEARCH_PERSPECTIVES:
        lines.append(f"- {item['title']}: {' / '.join(item['questions'])}")
    lines.extend(["", "## Project Stages"])
    lines.extend(_format_section(stages, empty_text="暂无相关阶段。"))
    lines.extend(["", "## Papers"])
    lines.extend(_format_section(papers, empty_text="暂无相关文献。"))
    lines.extend(["", "## Experiments"])
    lines.extend(_format_section(experiments, empty_text="暂无相关实验。"))
    lines.extend(["", "## Tasks"])
    lines.extend(_format_section(tasks, empty_text="暂无相关任务。"))
    lines.extend(
        [
            "",
            "## Open Questions",
            f"- 从 `Advisor` 视角再追问：围绕「{normalized_topic}」当前最缺的证据是什么？",
            f"- 从 `Reviewer` 视角再追问：围绕「{normalized_topic}」当前最弱的对照组是什么？",
            "",
        ]
    )
    return "\n".join(lines)


def write_topic_mind_map(
    store: ScholarFlowStore,
    *,
    vault_root: Path,
    topic: str,
    target_path: str | None = None,
) -> dict[str, Any]:
    body = build_topic_mind_map(store, topic=topic)
    if target_path:
        path = Path(target_path)
        if path.is_absolute() is False:
            path = Path(vault_root) / path
    else:
        path = Path(vault_root) / "wiki" / "concepts" / f"{topic} Mind Map.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return {"status": "written", "target_path": str(path), "topic": topic}


def _match_records(records: Iterable[dict[str, str]], topic: str, *, max_items: int) -> list[dict[str, str]]:
    topic_tokens = [token.lower() for token in topic.split() if token.strip()]
    ranked: list[tuple[int, dict[str, str]]] = []
    for record in records:
        text = str(record.get("text") or "").lower()
        score = sum(1 for token in topic_tokens if token in text)
        ranked.append((score, record))
    ranked.sort(key=lambda item: (item[0], item[1].get("label") or ""), reverse=True)
    matched = [record for score, record in ranked if score > 0]
    if matched:
        return matched[:max_items]
    return [record for _, record in ranked[:max_items]]


def _format_section(records: list[dict[str, str]], *, empty_text: str) -> list[str]:
    if not records:
        return [f"- {empty_text}"]
    return [f"- {item['label']} | {item['detail']}" for item in records]
