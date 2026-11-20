"""Knowledge-base constitution layer for controlled wiki writes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .contracts import KBWriteDecision, KBWriteRequest
from .kb_write_policy import evaluate_kb_write, required_side_effects


def plan_kb_write(
    vault_root: Path,
    request: KBWriteRequest,
    *,
    auto_side_effects: bool = False,
) -> KBWriteDecision:
    effective_request = _with_auto_acknowledged_side_effects(request) if auto_side_effects else request
    return evaluate_kb_write(vault_root, effective_request)


def write_kb_note(
    vault_root: Path,
    request: KBWriteRequest,
    *,
    auto_side_effects: bool = False,
) -> KBWriteDecision:
    effective_request = _with_auto_acknowledged_side_effects(request) if auto_side_effects else request
    decision = plan_kb_write(vault_root, effective_request)
    if not decision.allowed:
        return decision

    target_path = Path(decision.target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(_render_note(effective_request), encoding="utf-8")
    if auto_side_effects:
        _apply_side_effects(vault_root, effective_request, target_path)
    return decision


def write_log_only_entry(
    vault_root: Path,
    *,
    title: str,
    reason: str,
    object_ref: str,
    source_paths: Iterable[str] = (),
) -> dict[str, Any]:
    log_path = vault_root / "wiki" / "log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    current_date = _today()
    source_suffix = ""
    source_list = [item for item in source_paths if item]
    if source_list:
        source_suffix = f" 来源: {', '.join(source_list)}。"
    entry = f"- **记录** `{object_ref}` - `{title}` — {reason}{source_suffix}"
    if not log_path.exists():
        log_path.write_text(f"# 操作日志\n\n## {current_date}\n{entry}\n", encoding="utf-8")
    else:
        content = log_path.read_text(encoding="utf-8")
        heading = f"## {current_date}"
        if heading in content:
            updated = content.replace(heading, f"{heading}\n{entry}", 1)
        else:
            suffix = "" if content.endswith("\n") else "\n"
            updated = content + f"{suffix}\n{heading}\n{entry}\n"
        log_path.write_text(updated, encoding="utf-8")
    return {
        "decision": "log_only",
        "allowed": True,
        "target_path": str(log_path),
        "title": title,
        "object_ref": object_ref,
        "reason": reason,
        "status": "logged_only",
    }


def append_merge_entry(
    vault_root: Path,
    *,
    target_relative_path: str,
    merge_block: str,
    source_paths: Iterable[str] = (),
) -> dict[str, Any]:
    relative = target_relative_path.strip().lstrip("/").replace("\\", "/")
    target_path = (vault_root / relative).resolve()
    vault_root_resolved = vault_root.resolve()
    if vault_root_resolved not in target_path.parents and target_path != vault_root_resolved:
        raise ValueError("merge target must be under vault root")
    if target_path.suffix.lower() != ".md":
        raise ValueError("merge target must be a markdown file")
    if not target_path.exists():
        raise ValueError("merge target does not exist")

    header = merge_block.strip().splitlines()[0] if merge_block.strip() else ""
    existing = target_path.read_text(encoding="utf-8")
    if header and header in existing:
        return {
            "decision": "merge",
            "allowed": True,
            "target_path": str(target_path),
            "status": "skipped_duplicate",
            "reason": "merge block already present",
        }

    suffix = "" if existing.endswith("\n") else "\n"
    updated = existing + f"{suffix}\n{merge_block.rstrip()}\n"
    target_path.write_text(updated, encoding="utf-8")
    _append_log_entry(vault_root / "wiki" / "log.md", _merge_log_request(source_paths), target_path)
    return {
        "decision": "merge",
        "allowed": True,
        "target_path": str(target_path),
        "status": "merged",
        "reason": "merge block appended",
    }


def _merge_log_request(source_paths: Iterable[str]) -> KBWriteRequest:
    return KBWriteRequest(
        title="KB Merge",
        content_kind="reference",
        body="KB merge entry",
        source_context={"scope": "kb_merge", "journal_required": False},
        target_path=None,
        related_links=[],
        source_paths=list(source_paths),
        side_effects_acknowledged=["wiki/log.md"],
    )


def _render_note(request: KBWriteRequest) -> str:
    frontmatter = [
        "---",
        f'title: "{request.title}"',
        f"type: {request.content_kind}",
        "status: draft",
    ]
    if request.related_links:
        frontmatter.append("related:")
        frontmatter.extend(f'  - "{link}"' for link in request.related_links)
    if request.source_paths:
        frontmatter.append("sources:")
        frontmatter.extend(f'  - "{path}"' for path in request.source_paths)
    frontmatter.append("---")

    body = request.body.strip()
    if not body.startswith("# "):
        body = f"# {request.title}\n\n{body}"
    return "\n".join(frontmatter) + "\n\n" + body.rstrip() + "\n"


def _with_auto_acknowledged_side_effects(request: KBWriteRequest) -> KBWriteRequest:
    required = required_side_effects(request)
    merged = list(request.side_effects_acknowledged)
    for item in required:
        if item not in merged:
            merged.append(item)
    return KBWriteRequest(
        title=request.title,
        content_kind=request.content_kind,
        body=request.body,
        source_context=dict(request.source_context),
        target_path=request.target_path,
        related_links=list(request.related_links),
        source_paths=list(request.source_paths),
        side_effects_acknowledged=merged,
    )


def _apply_side_effects(vault_root: Path, request: KBWriteRequest, target_path: Path) -> None:
    for effect in required_side_effects(request):
        if effect == "wiki/log.md":
            _append_log_entry(vault_root / effect, request, target_path)
        elif effect == "10_Projects/example_vault.md":
            _ensure_project_root_link(vault_root / effect, target_path, request.title)
        elif effect.startswith("30_Journal/"):
            _append_journal_stub(vault_root / effect, request, target_path)


def _append_log_entry(log_path: Path, request: KBWriteRequest, target_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    current_date = _today()
    entry = f"- **更新** `{_display_path(log_path.parent.parent, target_path)}` — 通过 KB Constitution Layer 写入 `{request.title}`。"
    if not log_path.exists():
        log_path.write_text(f"# 操作日志\n\n## {current_date}\n{entry}\n", encoding="utf-8")
        return

    content = log_path.read_text(encoding="utf-8")
    heading = f"## {current_date}"
    if heading in content:
        updated = content.replace(heading, f"{heading}\n{entry}", 1)
    else:
        suffix = "" if content.endswith("\n") else "\n"
        updated = content + f"{suffix}\n{heading}\n{entry}\n"
    log_path.write_text(updated, encoding="utf-8")


def _ensure_project_root_link(project_root_path: Path, target_path: Path, title: str) -> None:
    project_root_path.parent.mkdir(parents=True, exist_ok=True)
    relative_target = _display_path(project_root_path.parent.parent, target_path).replace("\\", "/")
    link = f"[[{relative_target}]]"
    bullet = f"- {link} - {title}"

    if not project_root_path.exists():
        project_root_path.write_text(f"# example_vault\n\n## 当前活跃知识项目\n\n{bullet}\n", encoding="utf-8")
        return

    content = project_root_path.read_text(encoding="utf-8")
    if link in content:
        return
    heading = "## 当前活跃知识项目"
    if heading in content:
        updated = content.replace(heading, f"{heading}\n\n{bullet}", 1)
    else:
        suffix = "" if content.endswith("\n") else "\n"
        updated = content + f"{suffix}\n## 当前活跃知识项目\n\n{bullet}\n"
    project_root_path.write_text(updated, encoding="utf-8")


def _append_journal_stub(journal_path: Path, request: KBWriteRequest, target_path: Path) -> None:
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"- KB sync: `{request.title}` -> `{_display_path(journal_path.parent.parent, target_path)}`"
    if journal_path.exists():
        content = journal_path.read_text(encoding="utf-8")
        if line in content:
            return
        journal_path.write_text(content.rstrip() + "\n" + line + "\n", encoding="utf-8")
        return
    journal_path.write_text(f"# {_today()} work log\n\n{line}\n", encoding="utf-8")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _display_path(root: Path, target_path: Path) -> str:
    try:
        return str(target_path.relative_to(root))
    except ValueError:
        return str(target_path)
