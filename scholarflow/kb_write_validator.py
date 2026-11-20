"""Validation rules for KB constitution checks."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .contracts import KBConstitutionCheck, KBWriteRequest
from .kb_target_resolver import expected_section_root, resolve_target_section

SPEC_SIGNALS = ("字段", "schema", "接口", "contract", "契约", "状态机", "输入", "输出", "data model")
SYNTHESIS_SIGNALS = ("路线图", "优先级", "决策", "方案", "规划", "为什么", "trade-off", "依赖")
ENTITY_SIGNALS = ("是什么", "角色定位", "组成部分", "一句话定义", "项目定位", "related:")
CONCEPT_SIGNALS = ("原理", "方法论", "定义", "适用条件", "区别", "联系")
REFERENCE_SIGNALS = ("配置", "排错", "操作手册", "文件索引", "命令", "版本")
LOG_SIGNALS = ("## 202", "今天", "本周", "工作日志", "操作日志", "- **更新**", "- **创建**")


def build_constitution_checks(vault_root: Path, request: KBWriteRequest, target_path: Path, *, current_date: str | None = None) -> list[KBConstitutionCheck]:
    checks: list[KBConstitutionCheck] = []
    checks.append(_classification_check(request))
    checks.append(_target_check(vault_root, request, target_path))
    checks.append(_duty_check(request))
    checks.append(_record_completeness_check(request, current_date=current_date))
    checks.append(_backlink_check(request))
    return checks


def required_side_effects(request: KBWriteRequest, *, current_date: str | None = None) -> list[str]:
    effects = ["wiki/log.md"]
    scope = str(request.source_context.get("scope", "")).lower()
    if scope in {"project", "scholarflow"}:
        effects.append("10_Projects/example_vault.md")
    if request.source_context.get("journal_required"):
        effective_date = current_date or datetime.now(timezone.utc).date().isoformat()
        effects.append(f"30_Journal/{effective_date} work log.md")
    return effects


def derive_missing_links(request: KBWriteRequest) -> list[str]:
    missing: list[str] = []
    if not request.related_links:
        missing.append("related_links")
    if not request.source_paths:
        missing.append("source_paths")
    return missing


def _classification_check(request: KBWriteRequest) -> KBConstitutionCheck:
    section = resolve_target_section(request.content_kind)
    passed = section != "wiki"
    detail = "内容类型已映射到知识库分层。" if passed else f"未知 content_kind: {request.content_kind}"
    return KBConstitutionCheck("page_classification", passed, "error", detail)


def _target_check(vault_root: Path, request: KBWriteRequest, target_path: Path) -> KBConstitutionCheck:
    expected_root = expected_section_root(vault_root, request.content_kind).resolve()
    try:
        target_resolved = target_path.resolve()
        passed = target_resolved.suffix == ".md" and expected_root in [target_resolved.parent.resolve(), *target_resolved.parents]
    except FileNotFoundError:
        target_resolved = target_path
        passed = target_path.suffix == ".md" and str(target_path).startswith(str(expected_root))
    detail = f"目标页将写入 `{target_path}`。" if passed else f"目标页不在 `{expected_root}` 允许范围内。"
    return KBConstitutionCheck("target_resolution", passed, "error", detail)


def _duty_check(request: KBWriteRequest) -> KBConstitutionCheck:
    if request.content_kind == "source":
        return KBConstitutionCheck("page_duty",bool(request.source_paths),"error","来源页可以归纳原文的方法与概念，必须保留明确来源。")
    body = request.body
    declared = request.content_kind
    scores = {
        "entity": _count_hits(body, ENTITY_SIGNALS),
        "synthesis": _count_hits(body, SYNTHESIS_SIGNALS),
        "concept": _count_hits(body, CONCEPT_SIGNALS),
        "spec": _count_hits(body, SPEC_SIGNALS),
        "reference": _count_hits(body, REFERENCE_SIGNALS),
    }
    log_hits = _count_hits(body, LOG_SIGNALS)
    strongest_kind = max(scores, key=scores.get)
    strongest_score = scores[strongest_kind]
    declared_score = scores.get(declared, 0)

    if declared == "entity" and (_count_hits(body, SPEC_SIGNALS) >= 2 or log_hits >= 1):
        return KBConstitutionCheck("page_duty", False, "error", "实体页混入规格或日志语义，建议改写到 specs/syntheses。")
    if declared == "spec" and log_hits >= 1:
        return KBConstitutionCheck("page_duty", False, "error", "规格页混入日志式记录，建议拆分。")
    if declared == "concept" and (_count_hits(body, SYNTHESIS_SIGNALS) >= 2 or "当前阶段" in body):
        return KBConstitutionCheck("page_duty", False, "error", "概念页混入项目阶段决策语义，建议改写到 syntheses。")
    if strongest_kind != declared and strongest_score >= max(2, declared_score + 1):
        return KBConstitutionCheck(
            "page_duty",
            False,
            "error",
            f"内容更像 `{strongest_kind}` 而不是 `{declared}`，目标页职责可能错误。",
        )
    return KBConstitutionCheck("page_duty", True, "error", "页面职责与声明类型基本一致。")


def _record_completeness_check(request: KBWriteRequest, *, current_date: str | None = None) -> KBConstitutionCheck:
    required = required_side_effects(request, current_date=current_date)
    missing = [item for item in required if item not in request.side_effects_acknowledged]
    passed = not missing
    detail = "已确认必要侧写动作。" if passed else f"仍缺少侧写确认: {', '.join(missing)}"
    return KBConstitutionCheck("record_completeness", passed, "warn", detail)


def _backlink_check(request: KBWriteRequest) -> KBConstitutionCheck:
    missing = derive_missing_links(request)
    passed = not missing
    detail = "已提供 related/source 追踪信息。" if passed else f"缺少追踪字段: {', '.join(missing)}"
    return KBConstitutionCheck("backlink_checkpoint", passed, "warn", detail)


def _count_hits(text: str, signals: Iterable[str]) -> int:
    lowered = text.lower()
    return sum(1 for signal in signals if signal.lower() in lowered)
