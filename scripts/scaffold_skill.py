"""Create a project-local ScholarFlow Skill and its first evaluation fixture.

This helper intentionally supports Skills that compose existing ``sf_*``
tools. A Skill that needs a new tool, domain object, or write path must follow
the full extension contract documented in ``pi-native-extension-guide.md``.
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Sequence


NAME_PATTERN = re.compile(r"^scholarflow-[a-z0-9]+(?:-[a-z0-9]+)*$")
TOOL_PATTERN = re.compile(r'^sf_[a-z0-9_]+$')
REGISTERED_TOOL_PATTERN = re.compile(r'name:\s*"(sf_[a-z0-9_]+)"')
SUPPORTED_LANES = (
    "paper",
    "intake",
    "literature",
    "experiment",
    "planning",
    "review",
    "automation",
)
RELEASE_LANES = {
    "paper": "entry",
    "intake": "entry",
    "experiment": "entry",
    "literature": "output",
    "planning": "output",
    "review": "output",
    "automation": "control",
}
APPLY_TOOLS = (
    "sf_artifact_apply_publish",
    "sf_experiment_apply_log",
    "sf_inbox_ack",
    "sf_kb_apply_write",
    "sf_paper_apply_intake",
    "sf_project_apply_update",
    "sf_relation_apply_upsert",
    "sf_task_apply_create",
    "sf_task_apply_update",
)


def scaffold_skill(
    *,
    package_root: Path,
    name: str,
    description: str,
    lane: str,
    prompt: str,
    required_tools: Sequence[str],
    governed_write: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate and create one Skill plus one strict starter fixture."""

    root = Path(package_root).resolve()
    normalized_name = str(name).strip()
    normalized_description = " ".join(str(description).split())
    normalized_prompt = " ".join(str(prompt).split())
    normalized_lane = str(lane).strip().lower()
    tools = list(dict.fromkeys(str(item).strip() for item in required_tools if str(item).strip()))

    if not NAME_PATTERN.fullmatch(normalized_name):
        raise ValueError("Skill name must match scholarflow-<lowercase-hyphen-name>.")
    if not normalized_description:
        raise ValueError("Skill description is required and must include its trigger context.")
    if not normalized_prompt:
        raise ValueError("A realistic evaluation prompt is required.")
    if normalized_lane not in SUPPORTED_LANES:
        raise ValueError(f"Unsupported lane '{normalized_lane}'. Choose from: {', '.join(SUPPORTED_LANES)}.")
    if not tools or any(not TOOL_PATTERN.fullmatch(item) for item in tools):
        raise ValueError("At least one valid sf_* required tool is required.")
    apply_tools = sorted(set(tools).intersection(APPLY_TOOLS))
    if apply_tools:
        raise ValueError(
            "The scaffold creates proposal/read workflows only. Add apply tools manually after confirmation-boundary review: "
            + ", ".join(apply_tools)
        )

    extension_path = root / "extensions" / "index.ts"
    fixture_path = root / "quality" / "skill-fixtures.json"
    lock_path = root / "quality" / "skill-lock.json"
    package_path = root / "package.json"
    if not extension_path.exists() or not fixture_path.exists():
        raise ValueError("package_root must contain extensions/index.ts and quality/skill-fixtures.json.")
    available_tools = set(REGISTERED_TOOL_PATTERN.findall(extension_path.read_text(encoding="utf-8")))
    unknown_tools = sorted(set(tools) - available_tools)
    if unknown_tools:
        raise ValueError("Implement and register unknown tools before scaffolding the Skill: " + ", ".join(unknown_tools))

    skill_dir = root / "skills" / normalized_name
    skill_path = skill_dir / "SKILL.md"
    fixture_source = fixture_path.read_text(encoding="utf-8")
    fixture_document = json.loads(fixture_source)
    fixtures = list(fixture_document.get("fixtures") or [])
    fixture_id = normalized_name.removeprefix("scholarflow-")
    if skill_dir.exists():
        raise ValueError(f"Skill already exists: {skill_dir}")
    if any(str(item.get("id")) == fixture_id for item in fixtures):
        raise ValueError(f"Fixture id already exists: {fixture_id}")
    if any(str(item.get("skill")) == normalized_name for item in fixtures):
        raise ValueError(f"A fixture already targets Skill: {normalized_name}")

    skill_source = _skill_source(
        name=normalized_name,
        description=normalized_description,
        lane=normalized_lane,
        required_tools=tools,
        governed_write=governed_write,
    )
    fixture = _fixture(
        fixture_id=fixture_id,
        skill=normalized_name,
        prompt=normalized_prompt,
        lane=normalized_lane,
        required_tools=tools,
        governed_write=governed_write,
    )
    fixture_document["fixtures"] = [*fixtures, fixture]
    package_version = "workspace"
    if package_path.exists():
        package_version = str(
            json.loads(package_path.read_text(encoding="utf-8")).get("version")
            or "workspace"
        )
    lock_source = lock_path.read_text(encoding="utf-8") if lock_path.exists() else None
    lock_document = (
        json.loads(lock_source)
        if lock_source is not None
        else {
            "schema_version": 1,
            "spec_url": "https://agentskills.io/specification",
            "package_version": package_version,
            "skills": [],
        }
    )
    lock_entries = list(lock_document.get("skills") or [])
    if any(str(item.get("name")) == normalized_name for item in lock_entries):
        raise ValueError(f"A reviewed source lock already targets Skill: {normalized_name}")
    lock_entries.append(
        {
            "name": normalized_name,
            "source_type": "first_party",
            "source": f"workspace://packages/scholarflow-pi/skills/{normalized_name}",
            "source_revision": f"@scholarflow/pi@{package_version}",
            "content_sha256": hashlib.sha256(skill_source.encode("utf-8")).hexdigest(),
            "update_policy": "repository_versioned_and_release_gated",
            "reviewed_at": date.today().isoformat(),
        }
    )
    lock_document["skills"] = sorted(lock_entries, key=lambda item: str(item.get("name") or ""))
    result = {
        "status": "dry_run" if dry_run else "created",
        "skill": normalized_name,
        "lane": normalized_lane,
        "skill_path": str(skill_path),
        "fixture_path": str(fixture_path),
        "lock_path": str(lock_path),
        "fixture_id": fixture_id,
        "required_tools": tools,
        "next_commands": [
            "python scripts/quality.py",
            f"node packages/scholarflow-pi/scripts/evaluate-skills.mjs --live --skill={normalized_name}",
        ],
    }
    if dry_run:
        result["skill_preview"] = skill_source
        result["fixture_preview"] = fixture
        return result

    skill_dir.mkdir(parents=True, exist_ok=False)
    try:
        _atomic_write(skill_path, skill_source)
        _atomic_write(fixture_path, json.dumps(fixture_document, indent=2, ensure_ascii=False) + "\n")
        _atomic_write(lock_path, json.dumps(lock_document, indent=2, ensure_ascii=False) + "\n")
    except Exception:
        if skill_path.exists():
            skill_path.unlink()
        _atomic_write(fixture_path, fixture_source)
        if lock_source is None:
            if lock_path.exists():
                lock_path.unlink()
        else:
            _atomic_write(lock_path, lock_source)
        if skill_dir.exists() and not any(skill_dir.iterdir()):
            skill_dir.rmdir()
        raise
    return result


def _skill_source(
    *,
    name: str,
    description: str,
    lane: str,
    required_tools: Sequence[str],
    governed_write: bool,
) -> str:
    title = name.removeprefix("scholarflow-").replace("-", " ").title()
    tool_lines = "\n".join(f"   - `{tool}`" for tool in required_tools)
    governance = (
        "Prepare proposals only. Never apply a mutation without a separate explicit confirmation step."
        if governed_write
        else "Keep this workflow read-only. Hand any requested mutation to a separate governed proposal/confirmation flow."
    )
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"1. Call `sf_lane_enter` with `lane: \"{lane}\"` when a bounded tool surface is needed.\n"
        "2. Ground the request with the minimum required ScholarFlow tools:\n"
        f"{tool_lines}\n"
        "3. Distinguish stored evidence from interpretation and state any missing evidence.\n"
        "4. Stop after the smallest complete result; do not widen into unrelated tools.\n\n"
        "## Completion\n\n"
        "Return a result linked to exact ScholarFlow ids, or ask one bounded clarification question when grounding is insufficient.\n\n"
        "## Governance\n\n"
        f"{governance}\n"
    )


def _fixture(
    *,
    fixture_id: str,
    skill: str,
    prompt: str,
    lane: str,
    required_tools: Sequence[str],
    governed_write: bool,
) -> dict[str, Any]:
    return {
        "id": fixture_id,
        "skill": skill,
        "prompt": prompt,
        "required_tools": list(required_tools),
        "live_expected_any": list(required_tools),
        "live_required_all": list(required_tools),
        "max_tool_calls": max(4, len(required_tools) + 2),
        "forbidden_tools": list(APPLY_TOOLS),
        "governed_write": bool(governed_write),
        "release_gate": {
            "lane": RELEASE_LANES[lane],
            "min_pass_rate": 1,
            "recommended_repeat": 3,
            "critical_checks": [
                "forbidden_tools",
                "no_tool_errors",
                "required_all",
                "max_tool_calls",
            ],
        },
    }


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            temporary_path = Path(handle.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scaffold a ScholarFlow Pi Skill using existing sf_* tools.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--lane", required=True, choices=SUPPORTED_LANES)
    parser.add_argument("--prompt", required=True, help="First realistic live-evaluation prompt.")
    parser.add_argument("--required-tool", action="append", required=True, dest="required_tools")
    parser.add_argument("--governed-write", action="store_true")
    parser.add_argument("--package-root", default=str(Path("packages") / "scholarflow-pi"))
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = scaffold_skill(
            package_root=Path(args.package_root),
            name=args.name,
            description=args.description,
            lane=args.lane,
            prompt=args.prompt,
            required_tools=args.required_tools,
            governed_write=args.governed_write,
            dry_run=args.dry_run,
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
