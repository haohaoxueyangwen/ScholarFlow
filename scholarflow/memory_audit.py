"""Provider-neutral, read-only quality checks for Pi durable memory files."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


SENSITIVE_PATTERN = re.compile(
    r"(?:"
    r"(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd|secret)"
    r"[\"']?\s*[:=]\s*[\"']?(?!<redacted>)[A-Za-z0-9._/+\-=]{8,}"
    r"|bearer\s+[A-Za-z0-9._-]{10,}"
    r"|sk-[A-Za-z0-9]{10,}"
    r"|AKIA[0-9A-Z]{12,}"
    r"|-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE KEY-----"
    r")",
    re.IGNORECASE,
)
DATE_PATTERN = re.compile(
    r"(?:last[_ -]?verified|verified|updated|created|date)\s*[:=]\s*(20\d{2})[-/](\d{1,2})[-/](\d{1,2})",
    re.IGNORECASE,
)
SCOPE_PATTERN = re.compile(r"(?:^|\n)\s*(?:[-*]\s*)?scope\s*[:=]\s*(global|project)\b", re.IGNORECASE)
KEY_VALUE_PATTERN = re.compile(r"^\s*(?:[-*]\s*)?([A-Za-z][A-Za-z0-9_. /-]{1,80})\s*[:=]\s*(\S.*)$")
RESEARCH_REF_PATTERN = re.compile(r"\b(?:paper-|exp-|task-|stage-|artifact-|relation-)[A-Za-z0-9_-]+\b|\b(?:citation|experiment result|paper claim)\b", re.IGNORECASE)


def audit_memory_content(
    agent_dir: Path,
    *,
    config: Mapping[str, Any] | None = None,
    project_root: Path | None = None,
    now: datetime | None = None,
    stale_days: int = 180,
    max_files: int = 500,
) -> dict[str, Any]:
    """Audit Markdown memory without returning memory text or secret values."""

    effective_agent = Path(agent_dir).expanduser().resolve()
    files = discover_memory_files(effective_agent, config=config, max_files=max_files)
    effective_now = _coerce_now(now)
    project_path = Path(project_root).expanduser().resolve() if project_root else None
    entries: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    for path, scope in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            issues.append(_issue("unreadable", "warn", [path], detail="memory file could not be read"))
            continue
        relative = _display_path(path, effective_agent)
        if SENSITIVE_PATTERN.search(text):
            issues.append(_issue("sensitive_content", "error", [path], detail="secret-like content detected; value omitted"))
        if RESEARCH_REF_PATTERN.search(text):
            issues.append(_issue("research_truth_in_generic_memory", "warn", [path], detail="domain facts should be verified in ScholarFlow"))
        declared_scope = _declared_scope(text)
        if declared_scope and declared_scope != scope:
            issues.append(_issue("mis_scoped", "warn", [path], detail=f"declared {declared_scope}, file scope {scope}"))
        if scope == "project" and project_path and _contains_foreign_absolute_path(text, project_path):
            issues.append(_issue("mis_scoped", "warn", [path], detail="project memory references a path outside the active project"))
        stale_date = _last_explicit_date(text)
        if stale_date and (effective_now - stale_date).days >= max(1, int(stale_days)):
            issues.append(_issue("stale", "warn", [path], detail=f"last verification is older than {stale_days} days"))
        for entry in _entries(text):
            normalized = _normalize_entry(entry)
            if not normalized:
                continue
            key_value = _key_value(entry)
            entries.append({
                "fingerprint": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                "key": key_value[0] if key_value else None,
                "value": key_value[1] if key_value else None,
                "scope": scope,
                "file": relative,
            })

    duplicate_groups = _groups(entries, "fingerprint")
    for group in duplicate_groups:
        if len({item["file"] for item in group}) > 1:
            issues.append(_issue("duplicate", "warn", [effective_agent / item["file"] for item in group], detail="same normalized entry appears in multiple memory files"))

    conflicts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        if entry["key"] and entry["value"]:
            conflicts[_normalize_entry(entry["key"])].append(entry)
    for key, group in conflicts.items():
        values = {_normalize_entry(str(item["value"])) for item in group}
        if len(values) > 1:
            issues.append(_issue("conflict", "warn", [effective_agent / item["file"] for item in group], detail=f"key '{key}' has conflicting values; values omitted"))

    files_with_issue = {file for item in issues for file in item.get("files", [])}
    severity_order = {"error": 2, "warn": 1, "info": 0}
    status = "ok"
    if any(item["severity"] == "error" for item in issues):
        status = "error"
    elif issues:
        status = "warn"
    return {
        "status": status,
        "scanned": bool(files),
        "memory_root": str(effective_agent),
        "file_count": len(files),
        "entry_count": len(entries),
        "affected_file_count": len(files_with_issue),
        "issue_count": len(issues),
        "issues": sorted(issues, key=lambda item: (-severity_order.get(item["severity"], 0), item["category"])),
        "recommendations": _recommendations(issues),
        "policy": {
            "read_only": True,
            "stale_days": max(1, int(stale_days)),
            "project_root_configured": project_path is not None,
        },
    }


def discover_memory_files(
    agent_dir: Path,
    *,
    config: Mapping[str, Any] | None = None,
    max_files: int = 500,
) -> list[tuple[Path, str]]:
    """Return global/project Markdown memory files without scanning sessions.db."""

    root = Path(agent_dir).expanduser().resolve()
    payload = dict(config or {})
    configured_memory = str(payload.get("memoryDir") or "").strip()
    global_root = Path(configured_memory).expanduser() if configured_memory else root / "pi-hermes-memory"
    if not global_root.is_absolute():
        global_root = root / global_root
    configured_projects = str(payload.get("projectsMemoryDir") or "projects-memory").strip()
    project_root = Path(configured_projects).expanduser()
    if not project_root.is_absolute():
        project_root = root / project_root
    candidates: list[tuple[Path, str]] = []
    for base, scope in ((global_root, "global"), (project_root, "project"), (root / "memory", "global")):
        if not base.exists() or not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if path.is_file():
                candidates.append((path.resolve(), scope))
    unique: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for path, scope in candidates:
        if path in seen:
            continue
        seen.add(path)
        unique.append((path, scope))
        if len(unique) >= max(1, int(max_files)):
            break
    return unique


def _entries(text: str) -> list[str]:
    blocks = re.split(r"\n\s*\n|(?=^#{1,6}\s+)" , text, flags=re.MULTILINE)
    return [block.strip()[:4_000] for block in blocks if block.strip()]


def _normalize_entry(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def _key_value(value: str) -> tuple[str, str] | None:
    for line in value.splitlines():
        match = KEY_VALUE_PATTERN.match(line)
        if match and len(match.group(2)) <= 500:
            return _normalize_entry(match.group(1)), match.group(2).strip()
    return None


def _declared_scope(text: str) -> str | None:
    match = SCOPE_PATTERN.search(text)
    return match.group(1).lower() if match else None


def _last_explicit_date(text: str) -> datetime | None:
    dates: list[datetime] = []
    for match in DATE_PATTERN.finditer(text):
        try:
            dates.append(datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc))
        except ValueError:
            continue
    return max(dates) if dates else None


def _contains_foreign_absolute_path(text: str, project_root: Path) -> bool:
    for match in re.findall(r"(?:[A-Za-z]:[\\/][^\s)\]]+|/(?:home|Users|work|workspace)/[^\s)\]]+)", text):
        candidate = Path(match.rstrip(".,;" )).expanduser().resolve()
        if project_root not in candidate.parents and candidate != project_root:
            return True
    return False


def _groups(entries: Sequence[dict[str, Any]], key: str) -> list[list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        grouped[str(entry.get(key) or "")].append(entry)
    return [group for group in grouped.values() if len(group) > 1]


def _issue(category: str, severity: str, paths: Sequence[Path], *, detail: str) -> dict[str, Any]:
    return {
        "category": category,
        "severity": severity,
        "files": sorted({str(Path(path)) for path in paths}),
        "detail": detail,
    }


def _recommendations(issues: Sequence[dict[str, Any]]) -> list[str]:
    categories = {str(item.get("category")) for item in issues}
    recommendations: list[str] = []
    if "sensitive_content" in categories:
        recommendations.append("Remove or rotate secret-like values; do not keep credentials in Pi memory.")
    if "duplicate" in categories:
        recommendations.append("Merge duplicate entries and keep one scoped, source-labeled memory.")
    if "conflict" in categories:
        recommendations.append("Review conflicting entries manually and record the chosen policy with a verification date.")
    if "stale" in categories:
        recommendations.append("Re-verify stale entries or archive them; do not silently refresh them from model output.")
    if "mis_scoped" in categories or "research_truth_in_generic_memory" in categories:
        recommendations.append("Move research facts to ScholarFlow papers, experiments, artifacts, or evidence relations.")
    return recommendations


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _coerce_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
