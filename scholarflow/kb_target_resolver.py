"""Resolve knowledge-base target sections and paths."""

from __future__ import annotations

import re
from pathlib import Path

from .contracts import KBWriteRequest

KIND_TO_SECTION = {
    "entity": "wiki/entities",
    "synthesis": "wiki/syntheses",
    "concept": "wiki/concepts",
    "source": "wiki/sources",
    "spec": "wiki/specs",
    "reference": "wiki/reference",
}


def resolve_target_section(content_kind: str) -> str:
    return KIND_TO_SECTION.get(content_kind, "wiki")


def resolve_target_path(vault_root: Path, request: KBWriteRequest) -> Path:
    if request.target_path:
        target_path = Path(request.target_path)
        return target_path if target_path.is_absolute() else vault_root / target_path

    section = resolve_target_section(request.content_kind)
    safe_name = _sanitize_title(request.title)
    return vault_root / section / f"{safe_name}.md"


def expected_section_root(vault_root: Path, content_kind: str) -> Path:
    return vault_root / resolve_target_section(content_kind)


def _sanitize_title(title: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "-", title.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "untitled"
