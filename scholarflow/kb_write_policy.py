"""Decision policy for KB constitution evaluation."""

from __future__ import annotations

from pathlib import Path

from .contracts import KBWriteDecision, KBWriteRequest
from .kb_target_resolver import resolve_target_path, resolve_target_section
from .kb_write_validator import build_constitution_checks, derive_missing_links, required_side_effects


def evaluate_kb_write(vault_root: Path, request: KBWriteRequest, *, current_date: str | None = None) -> KBWriteDecision:
    target_path = resolve_target_path(vault_root, request)
    checks = build_constitution_checks(vault_root, request, target_path, current_date=current_date)
    failed_errors = [check for check in checks if not check.passed and check.severity == "error"]
    failed_warns = [check for check in checks if not check.passed and check.severity == "warn"]
    missing_links = derive_missing_links(request)
    required_effects = required_side_effects(request, current_date=current_date)

    if any(check.name in {"page_classification", "target_resolution"} for check in failed_errors):
        decision = "needs_retarget"
        allowed = False
        reason = "知识条目的类型或目标目录不符合宪法分层。"
    elif any(check.name == "page_duty" for check in failed_errors):
        decision = "needs_split"
        allowed = False
        reason = "内容与目标页面职责不匹配，需拆分或改投其它分层。"
    elif failed_warns:
        decision = "needs_human_confirm"
        allowed = False
        reason = "内容本身可写，但仍缺少侧写确认或追踪链接。"
    else:
        decision = "allow_write"
        allowed = True
        reason = "知识写入通过宪法检查，可以执行。"

    return KBWriteDecision(
        source_context=dict(request.source_context),
        content_kind=request.content_kind,
        target_section=resolve_target_section(request.content_kind),
        target_path=str(target_path),
        decision=decision,
        allowed=allowed,
        reason=reason,
        missing_links=missing_links,
        required_side_effects=required_effects,
        constitution_checks=checks,
    )
