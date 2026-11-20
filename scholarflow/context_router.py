"""Deterministic per-turn context routing for the Pi-native Workbench.

The router does not execute retrieval or replace Pi's context window.  It
classifies the current request and publishes an auditable working-set policy
for the existing owners: Pi history, Workbench checkpoints, ScholarFlow typed
truth, LightRAG evidence, and Hermes experiential memory.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable


CONTEXT_ROUTER_SCHEMA_VERSION = 1
CONTEXT_ROUTER_POLICY_VERSION = "pi-native-context-router-v1"
DEFAULT_CONTEXT_TARGET_TOKENS = 400_000

WORKLOADS = {
    "factual_query",
    "sequential_decision",
    "project_review",
    "cross_project_synthesis",
    "open_research",
}

SOURCE_AUTHORITY = {
    "recent_dialogue": "working_context_only",
    "checkpoint": "handoff_hint_verify_before_use",
    "typed_truth": "authoritative_current_state",
    "hermes": "preference_and_procedure_hint_only",
    "light_rag": "derived_evidence_discovery_only",
}


@dataclass(frozen=True)
class _Profile:
    working_set_basis_points: int
    checkpoint_items: int
    checkpoint_characters: int
    weights: dict[str, int]
    source_order: tuple[str, ...]
    directive: str


_PROFILES: dict[str, _Profile] = {
    "factual_query": _Profile(
        working_set_basis_points=300,
        checkpoint_items=1,
        checkpoint_characters=1_600,
        weights={
            "recent_dialogue": 10,
            "checkpoint": 5,
            "typed_truth": 70,
            "hermes": 0,
            "light_rag": 15,
        },
        source_order=("typed_truth", "light_rag", "checkpoint", "recent_dialogue"),
        directive=(
            "Read the smallest matching sf_* record first. Checkpoints are only "
            "handoff hints; use LightRAG only when source evidence is needed and "
            "do not use Hermes for current research facts."
        ),
    ),
    "sequential_decision": _Profile(
        working_set_basis_points=400,
        checkpoint_items=2,
        checkpoint_characters=3_000,
        weights={
            "recent_dialogue": 35,
            "checkpoint": 25,
            "typed_truth": 30,
            "hermes": 5,
            "light_rag": 5,
        },
        source_order=("recent_dialogue", "checkpoint", "typed_truth", "light_rag", "hermes"),
        directive=(
            "Continue from the active dialogue and relevant handoff, re-reading "
            "changed state through sf_* tools. Hermes may supply preferences or "
            "reusable procedures only, never the decision's research facts."
        ),
    ),
    "project_review": _Profile(
        working_set_basis_points=600,
        checkpoint_items=5,
        checkpoint_characters=6_000,
        weights={
            "recent_dialogue": 15,
            "checkpoint": 20,
            "typed_truth": 45,
            "hermes": 0,
            "light_rag": 20,
        },
        source_order=("typed_truth", "checkpoint", "light_rag", "recent_dialogue"),
        directive=(
            "Review project-scoped typed records and a bounded evidence bundle. "
            "Use checkpoints for continuity, surface missing evidence, and do not "
            "treat generic memory as project status."
        ),
    ),
    "cross_project_synthesis": _Profile(
        working_set_basis_points=800,
        checkpoint_items=10,
        checkpoint_characters=10_000,
        weights={
            "recent_dialogue": 10,
            "checkpoint": 20,
            "typed_truth": 45,
            "hermes": 0,
            "light_rag": 25,
        },
        source_order=("typed_truth", "checkpoint", "light_rag", "recent_dialogue"),
        directive=(
            "Compare bounded project summaries and typed records; never merge raw "
            "full chat histories. Preserve project provenance and verify every "
            "cross-project claim before ranking or prioritizing it."
        ),
    ),
    "open_research": _Profile(
        working_set_basis_points=800,
        checkpoint_items=1,
        checkpoint_characters=1_600,
        weights={
            "recent_dialogue": 10,
            "checkpoint": 5,
            "typed_truth": 10,
            "hermes": 5,
            "light_rag": 70,
        },
        source_order=("light_rag", "typed_truth", "recent_dialogue", "checkpoint", "hermes"),
        directive=(
            "Prioritize primary, citable evidence and provenance-checked retrieval. "
            "Use typed state only to scope the research; Hermes may recall search "
            "preferences or procedures but cannot support scholarly claims."
        ),
    ),
}


_SIGNALS: dict[str, tuple[tuple[str, str, int], ...]] = {
    "cross_project_synthesis": (
        ("cross_project_zh", r"跨项目|所有项目|全部项目|各个项目|多个项目|项目组合|科研总控|全局.{0,8}项目|汇总.{0,12}项目", 8),
        ("cross_project_en", r"\b(?:across|all|multiple|portfolio|overall)\b.{0,40}\b(?:projects?|theses)\b|\b(?:my|all|multiple|active)\s+(?:research\s+)?projects?\b|\bprojects?\b.{0,40}\b(?:across|portfolio|prioriti[sz]e|compare|attention)\b", 8),
    ),
    "open_research": (
        ("literature_zh", r"文献综述|研究现状|最新研究|研究前沿|研究空白|开放问题|检索.{0,48}(?:论文|文献)|搜索.{0,48}(?:论文|文献)|调研.{0,24}(?:架构|方法|技术|领域)|探索.{0,12}(?:是否|方法|机制)", 7),
        ("literature_en", r"\b(?:literature review|state of the art|latest research|research frontier|research gaps?|open research question|find (?:primary )?papers?|search (?:for )?papers?|research landscape|explore whether|investigate whether)\b", 7),
    ),
    "project_review": (
        ("review_zh", r"项目回顾|项目复盘|课题复盘|阶段复盘|周报|日报|进展.{0,12}阻塞|阻塞.{0,12}进展|总结.{0,12}(?:项目|课题)|回顾.{0,12}(?:项目|课题)", 6),
        ("review_en", r"\b(?:project review|progress review|weekly review|daily review|retrospective|status and blockers?|progress.{0,20}blockers?|summari[sz]e.{0,20}(?:project|thesis)|review.{0,20}(?:project|thesis))\b", 6),
    ),
    "sequential_decision": (
        ("sequence_zh", r"继续|下一步|接下来|然后呢|如何选择|怎么选|决定|决策|执行计划|开始执行|安排.{0,12}(?:步骤|任务)|优先级", 5),
        ("sequence_en", r"\b(?:continue|next steps?|what should|how should|decide|decision|choose|which .* next|proceed|carry on|continue implementing|priorit(?:y|ize)|plan the next)\b", 5),
    ),
    "factual_query": (
        ("fact_zh", r"是什么|有哪些|列出|查看|查询|检查|核对|确认|多少|当前状态|标题|作者|年份|编号|标识|\bdoi\b", 4),
        ("fact_en", r"\b(?:what is|what are|show|list|get|check|verify|confirm|how many|current status|stored tasks?|title|authors?|year|identifier|doi|marked)\b", 4),
    ),
}

_TIE_PRIORITY = {
    "cross_project_synthesis": 5,
    "open_research": 4,
    "project_review": 3,
    "sequential_decision": 2,
    "factual_query": 1,
}


def classify_context_request(
    message: str,
    *,
    scope: str = "global",
    context_refs: Iterable[str] = (),
) -> dict[str, Any]:
    """Classify one user turn and explain the deterministic signals used."""

    text = _user_request_only(message).casefold()
    scores = {workload: 0 for workload in WORKLOADS}
    matched: list[dict[str, Any]] = []
    for workload, rules in _SIGNALS.items():
        for signal, pattern, weight in rules:
            if re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL):
                scores[workload] += weight
                matched.append(
                    {"signal": signal, "workload": workload, "weight": weight}
                )

    refs = [str(item).strip() for item in context_refs if str(item).strip()]
    project_refs = {
        item.split(":", 1)[-1]
        for item in refs
        if item.startswith("stage:") or item.startswith("stage-")
    }
    if len(project_refs) > 1:
        scores["cross_project_synthesis"] += 8
        matched.append(
            {
                "signal": "multiple_project_references",
                "workload": "cross_project_synthesis",
                "weight": 8,
            }
        )
    if scope == "project" and scores["project_review"]:
        scores["project_review"] += 1
    if scope == "task" and scores["sequential_decision"]:
        scores["sequential_decision"] += 1

    if not any(scores.values()):
        workload = "sequential_decision"
        matched.append(
            {
                "signal": "continuity_default",
                "workload": workload,
                "weight": 1,
            }
        )
        scores[workload] = 1
    else:
        workload = max(
            WORKLOADS,
            key=lambda item: (scores[item], _TIE_PRIORITY[item]),
        )

    ranked = sorted(scores.values(), reverse=True)
    margin = ranked[0] - ranked[1]
    confidence = min(0.99, 0.62 + min(scores[workload], 10) * 0.025 + min(margin, 8) * 0.015)
    return {
        "workload": workload,
        "confidence": round(confidence, 3),
        "signals": [item for item in matched if item["workload"] == workload],
        "scores": dict(sorted(scores.items())),
    }


def route_context(
    message: str,
    *,
    scope: str = "global",
    context_refs: Iterable[str] = (),
    context_target_tokens: int = DEFAULT_CONTEXT_TARGET_TOKENS,
) -> dict[str, Any]:
    """Return a JSON-ready route without loading or writing any source."""

    classification = classify_context_request(
        message,
        scope=scope,
        context_refs=context_refs,
    )
    workload = classification["workload"]
    profile = _PROFILES[workload]
    target = max(1, int(context_target_tokens))
    working_set = max(1, target * profile.working_set_basis_points // 10_000)
    allocations = []
    for source_type, weight in profile.weights.items():
        allocations.append(
            {
                "source_type": source_type,
                "weight_percent": weight,
                "target_tokens": working_set * weight // 100,
                "authority": SOURCE_AUTHORITY[source_type],
                "enforcement": (
                    "pi_native_advisory"
                    if source_type == "recent_dialogue"
                    else "workbench_injection_cap"
                    if source_type == "checkpoint"
                    else "governed_tool_result_budget"
                ),
                "enabled": weight > 0,
            }
        )
    return {
        "schema_version": CONTEXT_ROUTER_SCHEMA_VERSION,
        "policy_version": CONTEXT_ROUTER_POLICY_VERSION,
        "workload": workload,
        "confidence": classification["confidence"],
        "signals": classification["signals"],
        "scope": scope,
        "working_set_target_tokens": working_set,
        "working_set_basis_points_of_compaction_target": profile.working_set_basis_points,
        "source_order": list(profile.source_order),
        "source_allocations": allocations,
        "checkpoint_policy": {
            "selection": "latest_per_relevant_session",
            "max_items": profile.checkpoint_items,
            "max_characters": profile.checkpoint_characters,
            "factual_claims_require_typed_verification": True,
        },
        "memory_policy": {
            "owner": "pi-hermes-memory",
            "read_mode": "on_demand",
            "write_mode": "explicit_preference_or_reusable_procedure_only",
            "allowed": [
                "user_preference",
                "correction",
                "reusable_procedure",
                "tool_quirk",
            ],
            "forbidden": [
                "project_status",
                "paper_claim",
                "experiment_result",
                "research_artifact",
            ],
            "lifecycle": [
                "candidate",
                "validated",
                "consolidated",
                "superseded_or_expired",
            ],
            "automatic_truth_promotion": False,
        },
        "directive": profile.directive,
    }


def format_context_route(route: dict[str, Any]) -> str:
    """Render the smallest model-facing policy block for one route."""

    checkpoint = dict(route.get("checkpoint_policy") or {})
    return (
        f"Dynamic context route: {route.get('workload')} "
        f"({route.get('policy_version')}).\n"
        f"Working-set target: {route.get('working_set_target_tokens')} tokens; "
        f"checkpoint cap: {checkpoint.get('max_items')} items / "
        f"{checkpoint.get('max_characters')} characters.\n"
        f"{route.get('directive')}\n"
        "Do not write generic memory during this turn unless the user explicitly "
        "asks to remember a preference or reusable procedure."
    )


def _user_request_only(message: str) -> str:
    marker = "\n\nUser request:\n"
    text = str(message or "")
    return text.split(marker, 1)[1] if marker in text else text
