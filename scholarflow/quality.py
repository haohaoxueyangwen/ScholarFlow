"""Read-only quality audits for the Pi-native ScholarFlow runtime."""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .event_bus import list_events
from .gateway import list_gateway_inbox
from .job_store import AutomationJobStore
from .knowledge_engine import LIGHTRAG_PINNED_VERSION
from .memory_audit import audit_memory_content
from .storage import ScholarFlowStore


STALE_EXPERIMENT_HOURS = {
    "running": 24,
    "planned": 24 * 7,
}

WORKFLOW_PENDING_HOURS = {
    "daily_review": 36,
    "weekly_review": 24 * 9,
    "experiment_watch": 2,
}


def detect_stale_experiments(
    store: ScholarFlowStore,
    *,
    now: datetime | None = None,
    thresholds: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Find experiments whose active state has not been refreshed in time."""

    effective_now = _coerce_now(now)
    limits = dict(STALE_EXPERIMENT_HOURS)
    limits.update(thresholds or {})
    items: list[dict[str, Any]] = []
    observed = 0
    for experiment in store.list_experiments():
        status = experiment.status.value
        threshold = limits.get(status)
        if threshold is None:
            continue
        observed += 1
        updated_at = _parse_timestamp(experiment.updated_at)
        if updated_at is None:
            items.append(
                {
                    "experiment_id": experiment.experiment_id,
                    "status": status,
                    "reason": "invalid_updated_at",
                    "updated_at": experiment.updated_at,
                    "threshold_hours": threshold,
                }
            )
            continue
        age_hours = max(0.0, (effective_now - updated_at).total_seconds() / 3600)
        if age_hours < threshold:
            continue
        items.append(
            {
                "experiment_id": experiment.experiment_id,
                "title": experiment.title,
                "status": status,
                "reason": f"{status}_without_refresh",
                "updated_at": updated_at.isoformat(),
                "age_hours": round(age_hours, 2),
                "threshold_hours": threshold,
                "has_runtime_reference": bool(experiment.log_path or experiment.checkpoint_path),
            }
        )
    return {
        "status": "warn" if items else "ok",
        "observed_count": observed,
        "stale_count": len(items),
        "items": items,
    }


def workflow_health_summary(
    store_root: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Summarize scheduled Pi workflow backlog and recent job outcomes."""

    effective_now = _coerce_now(now)
    inbox = list_gateway_inbox(store_root, status="", limit=100)
    pending = [item for item in inbox["items"] if item.get("status") == "pending" and item.get("workflow_type")]
    pending_by_type = Counter(str(item.get("workflow_type")) for item in pending)
    overdue: list[dict[str, Any]] = []
    for item in pending:
        workflow_type = str(item.get("workflow_type"))
        received_at = _parse_timestamp(item.get("received_at"))
        threshold = WORKFLOW_PENDING_HOURS.get(workflow_type, 24)
        if received_at is None:
            overdue.append({"message_id": item.get("message_id"), "workflow_type": workflow_type, "reason": "invalid_received_at"})
            continue
        age_hours = max(0.0, (effective_now - received_at).total_seconds() / 3600)
        if age_hours >= threshold:
            overdue.append(
                {
                    "message_id": item.get("message_id"),
                    "workflow_type": workflow_type,
                    "age_hours": round(age_hours, 2),
                    "threshold_hours": threshold,
                }
            )

    jobs = AutomationJobStore(store_root).list_jobs()
    failed_jobs = [job for job in jobs if job.get("last_status") == "failed"]
    throttled_jobs = [job for job in jobs if job.get("status") == "throttled"]
    events = list_events(store_root, limit=200).get("items") or []
    succeeded_by_type: Counter[str] = Counter()
    failed_by_type: Counter[str] = Counter()
    for event in events:
        payload = event.get("payload") or {}
        if event.get("event_type") == "automation.job.succeeded":
            succeeded_by_type[str(payload.get("job_type") or "unknown")] += 1
        elif event.get("event_type") == "automation.job.failed":
            failed_by_type[str(payload.get("job_type") or "unknown")] += 1

    status = "healthy"
    if overdue or failed_jobs or throttled_jobs:
        status = "degraded"
    elif pending:
        status = "pending"
    return {
        "status": status,
        "pending_count": len(pending),
        "pending_by_type": dict(sorted(pending_by_type.items())),
        "overdue_count": len(overdue),
        "overdue": overdue,
        "job_count": len(jobs),
        "failed_job_ids": [str(job.get("job_id")) for job in failed_jobs],
        "throttled_job_ids": [str(job.get("job_id")) for job in throttled_jobs],
        "recent_succeeded_by_type": dict(sorted(succeeded_by_type.items())),
        "recent_failed_by_type": dict(sorted(failed_by_type.items())),
    }


def audit_pi_resources(
    repository_root: Path,
    *,
    agent_dir: Path | None = None,
) -> dict[str, Any]:
    """Audit memory ownership, project Skill metadata, and quality fixtures."""

    root = Path(repository_root)
    settings_path = root / ".pi" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    packages = [str(item) for item in settings.get("packages") or []]
    memory_packages = [item for item in packages if "memory" in item.lower()]
    skills_root = root / "packages" / "scholarflow-pi" / "skills"
    skill_dirs = sorted(path for path in skills_root.iterdir() if path.is_dir()) if skills_root.exists() else []
    skill_names: list[str] = []
    malformed: list[str] = []
    for skill_dir in skill_dirs:
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            malformed.append(skill_dir.name)
            continue
        name = _frontmatter_value(skill_file.read_text(encoding="utf-8"), "name")
        description = _frontmatter_value(skill_file.read_text(encoding="utf-8"), "description")
        if name != skill_dir.name or not description:
            malformed.append(skill_dir.name)
        skill_names.append(name or skill_dir.name)
    duplicates = sorted(name for name, count in Counter(skill_names).items() if count > 1)

    fixture_path = root / "packages" / "scholarflow-pi" / "quality" / "skill-fixtures.json"
    fixture_count = 0
    fixture_scenario_count = 0
    fixture_skills: set[str] = set()
    duplicate_fixture_ids: list[str] = []
    if fixture_path.exists():
        fixture_payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        fixture_items = [item for item in fixture_payload.get("fixtures") or [] if isinstance(item, dict)]
        fixture_scenario_count = len(fixture_items)
        fixture_skills = {str(item.get("skill")) for item in fixture_items}
        fixture_ids = [str(item.get("id") or "") for item in fixture_items]
        duplicate_fixture_ids = sorted(
            fixture_id for fixture_id, count in Counter(fixture_ids).items() if not fixture_id or count > 1
        )
        fixture_count = len(fixture_skills)
    missing_fixtures = sorted(set(skill_names) - fixture_skills)
    orphan_fixture_skills = sorted(fixture_skills - set(skill_names))
    ecosystem = audit_ecosystem_manifest(root)

    effective_agent_dir = Path(
        agent_dir
        or os.environ.get("PI_CODING_AGENT_DIR")
        or (Path.home() / ".pi" / "agent")
    )
    hermes_config_path = effective_agent_dir / "hermes-memory-config.json"
    try:
        hermes_config = json.loads(hermes_config_path.read_text(encoding="utf-8")) if hermes_config_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        # Hermes may atomically rewrite this user-level file while an audit is
        # running. Quality status must remain read-only and fail soft.
        hermes_config = {}
    memory_mode = str(hermes_config.get("memoryMode") or "policy-only")
    policy_style = str(hermes_config.get("memoryPolicyStyle") or "full")
    session_variant = str((hermes_config.get("sessionSearch") or {}).get("variant") or "legacy")
    append_policy = root / ".pi" / "APPEND_SYSTEM.md"
    append_text = append_policy.read_text(encoding="utf-8").lower() if append_policy.exists() else ""
    ownership_policy_ok = "research truth" in append_text and "memory" in append_text
    lifecycle_policy_ok = all(
        marker in append_text
        for marker in ("candidate", "validated", "consolidated", "superseded/expired")
    )

    issues: list[dict[str, Any]] = []
    if len(memory_packages) != 1 or not memory_packages[0].startswith("npm:pi-hermes-memory@"):
        issues.append({"severity": "error", "code": "memory_owner_count", "detail": memory_packages})
    if malformed:
        issues.append({"severity": "error", "code": "malformed_skills", "detail": malformed})
    if duplicates:
        issues.append({"severity": "error", "code": "duplicate_skills", "detail": duplicates})
    if missing_fixtures:
        issues.append({"severity": "warn", "code": "missing_skill_fixtures", "detail": missing_fixtures})
    if orphan_fixture_skills:
        issues.append({"severity": "error", "code": "orphan_skill_fixtures", "detail": orphan_fixture_skills})
    if duplicate_fixture_ids:
        issues.append({"severity": "error", "code": "duplicate_fixture_ids", "detail": duplicate_fixture_ids})
    if not ownership_policy_ok:
        issues.append({"severity": "error", "code": "memory_ownership_policy_missing", "detail": ".pi/APPEND_SYSTEM.md"})
    if not lifecycle_policy_ok:
        issues.append({"severity": "error", "code": "memory_lifecycle_policy_missing", "detail": ".pi/APPEND_SYSTEM.md"})
    if ecosystem["status"] == "error":
        issues.append({"severity": "error", "code": "ecosystem_manifest", "detail": ecosystem["issues"]})
    elif ecosystem["status"] == "warn":
        issues.append({"severity": "warn", "code": "ecosystem_manifest", "detail": ecosystem["issues"]})
    recommendations: list[str] = []
    if session_variant != "anchors":
        recommendations.append("Consider sessionSearch.variant=anchors for source-grounded session recall.")
    memory_content = audit_memory_content(
        effective_agent_dir,
        config=hermes_config,
        project_root=root,
    )
    if memory_content["status"] == "error":
        issues.append({"severity": "error", "code": "memory_content_audit", "detail": "sensitive memory content requires review"})
    elif memory_content["status"] == "warn":
        issues.append({"severity": "warn", "code": "memory_content_audit", "detail": f"{memory_content['issue_count']} memory content issue(s)"})
    recommendations.extend(memory_content.get("recommendations") or [])
    status = "ok"
    if any(item["severity"] == "error" for item in issues):
        status = "error"
    elif issues:
        status = "warn"
    return {
        "status": status,
        "memory": {
            "owner_packages": memory_packages,
            "memory_mode": memory_mode,
            "policy_style": policy_style,
            "session_search_variant": session_variant,
            "explicit_config": hermes_config_path.exists(),
            "research_truth_policy": ownership_policy_ok,
            "lifecycle_policy": lifecycle_policy_ok,
            "content_audit": memory_content,
        },
        "skills": {
            "count": len(skill_names),
            "fixture_count": fixture_count,
            "fixture_scenario_count": fixture_scenario_count,
            "malformed": malformed,
            "duplicates": duplicates,
            "missing_fixtures": missing_fixtures,
            "orphan_fixture_skills": orphan_fixture_skills,
            "duplicate_fixture_ids": duplicate_fixture_ids,
        },
        "ecosystem": ecosystem,
        "issues": issues,
        "recommendations": recommendations,
    }


def audit_ecosystem_manifest(repository_root: Path) -> dict[str, Any]:
    """Verify every external Pi package has a reviewed, pinned manifest entry."""

    root = Path(repository_root)
    manifest_path = root / "packages" / "scholarflow-pi" / "quality" / "ecosystem-manifest.json"
    settings_path = root / ".pi" / "settings.json"
    if not manifest_path.exists() or not settings_path.exists():
        return {"status": "error", "package_count": 0, "issues": ["ecosystem manifest or Pi settings is missing"]}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "error", "package_count": 0, "issues": [f"could not read ecosystem metadata: {type(exc).__name__}"]}
    packages = [item for item in manifest.get("packages") or [] if isinstance(item, dict)]
    required = set(manifest.get("policy", {}).get("required_review_fields") or [])
    issues: list[str] = []
    names = [str(item.get("package") or "") for item in packages]
    if len(names) != len(set(names)) or any(not name for name in names):
        issues.append("manifest package names must be unique and non-empty")
    specs = {f"npm:{item.get('package')}@{item.get('version')}" for item in packages}
    configured_specs = {str(item) for item in settings.get("packages") or [] if str(item).startswith("npm:")}
    if specs != configured_specs:
        issues.append("manifest package pins do not match .pi/settings.json")
    for item in packages:
        missing = sorted(field for field in required if not item.get(field))
        if missing:
            issues.append(f"{item.get('package') or 'unknown'} missing review fields: {','.join(missing)}")
        if str(item.get("license") or "").upper() not in {"MIT", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "APACHE-2.0"}:
            issues.append(f"{item.get('package') or 'unknown'} has an unapproved license marker")
    memory_owners = [item for item in packages if "memory" in str(item.get("capability") or "").lower()]
    if len(memory_owners) != 1:
        issues.append("ecosystem manifest must identify exactly one generic memory owner")
    return {
        "status": "error" if issues else "ok",
        "package_count": len(packages),
        "packages": [
            {
                "package": item.get("package"),
                "version": item.get("version"),
                "capability": item.get("capability"),
                "permission_class": item.get("permission_class"),
                "domain_write_owner": item.get("domain_write_owner"),
            }
            for item in packages
        ],
        "issues": issues,
    }


def audit_knowledge_engine_manifest(repository_root: Path) -> dict[str, Any]:
    """Verify the LightRAG pin, adapter API profile, and deployment files agree."""

    root = Path(repository_root)
    manifest_path = (
        root / "packages" / "scholarflow-pi" / "quality" / "knowledge-engine-manifest.json"
    )
    dockerfile_path = root / "deploy" / "lightrag" / "Dockerfile"
    compose_path = root / "deploy" / "lightrag" / "compose.yaml"
    issues: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "error",
            "issues": [f"could not read knowledge-engine manifest: {type(exc).__name__}"],
        }
    if str(manifest.get("version") or "") != LIGHTRAG_PINNED_VERSION:
        issues.append("manifest version does not match the adapter pin")
    if not manifest.get("wheel_sha256") or len(str(manifest["wheel_sha256"])) != 64:
        issues.append("manifest wheel SHA-256 is missing or malformed")
    api_profile = manifest.get("api_profile") or {}
    required_endpoints = {
        "health",
        "query",
        "text_ingest",
        "track_status",
        "delete_document",
        "pipeline_status",
    }
    if set(api_profile) != required_endpoints:
        issues.append("knowledge-engine API profile is incomplete")
    security = manifest.get("security") or {}
    if security.get("bind") != "127.0.0.1" or not security.get("api_key_required"):
        issues.append("knowledge-engine local binding or API-key policy is unsafe")
    if security.get("remote_http_allowed") is not False:
        issues.append("remote HTTP must remain disabled")
    for path in (dockerfile_path, compose_path):
        if not path.exists():
            issues.append(f"deployment file is missing: {path.relative_to(root)}")
            continue
        source = path.read_text(encoding="utf-8")
        if LIGHTRAG_PINNED_VERSION not in source:
            issues.append(f"deployment pin is missing from {path.relative_to(root)}")
    acceptance = manifest.get("acceptance") or {}
    real_service = str(acceptance.get("real_service") or "unknown")
    gates = acceptance.get("gates") or {}
    if real_service == "passed" and (
        not gates or any(value != "passed" for value in gates.values())
    ):
        issues.append("real-service acceptance is passed without complete passed gates")
    status = "error" if issues else ("ok" if real_service == "passed" else "pending")
    return {
        "status": status,
        "engine": manifest.get("engine"),
        "version": manifest.get("version"),
        "contract_tests": acceptance.get("contract_tests"),
        "real_service": real_service,
        "accepted_at": acceptance.get("accepted_at"),
        "gates": gates,
        "issues": issues,
    }


def quality_status(
    store: ScholarFlowStore,
    *,
    repository_root: Path,
    now: datetime | None = None,
    agent_dir: Path | None = None,
) -> dict[str, Any]:
    stale = detect_stale_experiments(store, now=now)
    workflows = workflow_health_summary(store.root_dir, now=now)
    resources = audit_pi_resources(repository_root, agent_dir=agent_dir)
    knowledge_engine = audit_knowledge_engine_manifest(repository_root)
    status = "ok"
    if (
        resources["status"] == "error"
        or workflows["status"] == "degraded"
        or knowledge_engine["status"] == "error"
    ):
        status = "error"
    elif (
        stale["status"] == "warn"
        or workflows["status"] == "pending"
        or resources["status"] == "warn"
        or knowledge_engine["status"] == "pending"
    ):
        status = "warn"
    return {
        "status": status,
        "stale_experiments": stale,
        "workflows": workflows,
        "pi_resources": resources,
        "knowledge_engine": knowledge_engine,
    }


def _frontmatter_value(source: str, key: str) -> str | None:
    if not source.startswith("---\n"):
        return None
    for line in source.split("---", 2)[1].splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _coerce_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
