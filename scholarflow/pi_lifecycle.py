"""Controlled Pi runtime discovery, qualification, update, and audit.

The project lock is authoritative.  Network discovery never mutates the
runtime, and an upgrade is applied only after a candidate Pi CLI passes the
deterministic ScholarFlow quality gate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


PI_PACKAGE = "@earendil-works/pi-coding-agent"
POLICY_PATH = Path("packages/scholarflow-pi/compatibility/runtime-policy.json")
PI_PACKAGE_PATH = Path("packages/scholarflow-pi/package.json")
PI_PACKAGE_LOCK_PATH = Path("packages/scholarflow-pi/package-lock.json")
ECOSYSTEM_MANIFEST_PATH = Path("packages/scholarflow-pi/quality/ecosystem-manifest.json")
SETTINGS_PATH = Path(".pi/settings.json")
MANAGED_NPM_PATH = Path(".pi/npm")
CACHE_PATH = Path(".tmp/pi-lifecycle/update-cache.json")
LAST_REPORT_PATH = Path(".tmp/pi-lifecycle/last-upgrade.json")
SEMVER_RE = re.compile(r"^[v=\s]*(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
NPM_SPEC_RE = re.compile(r"^npm:(.+)@([^@]+)$")
SCOPE_RANK = {"none": 0, "patch": 1, "minor": 2, "major": 3}
SEVERITY_RANK = {"info": 0, "low": 1, "moderate": 2, "high": 3, "critical": 4}


class PiLifecycleError(RuntimeError):
    """Raised when a lifecycle action cannot be completed safely."""


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> "Version":
        match = SEMVER_RE.match(str(value).strip())
        if not match:
            raise ValueError(f"Unsupported semantic version: {value!r}")
        return cls(*(int(part) for part in match.groups()))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def version_scope(current: str, target: str) -> str:
    before = Version.parse(current)
    after = Version.parse(target)
    if before == after:
        return "none"
    if before.major != after.major:
        return "major"
    if before.minor != after.minor:
        return "minor"
    return "patch"


def peer_range_for(version: str) -> str:
    parsed = Version.parse(version)
    if parsed.major == 0:
        return f">={parsed.major}.{parsed.minor}.0 <{parsed.major}.{parsed.minor + 1}.0"
    return f">={parsed.major}.0.0 <{parsed.major + 1}.0.0"


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n", encoding="utf-8")
    os.replace(temporary, path)


def _package_version(path: Path) -> str | None:
    try:
        return str(_read_json(path)["version"])
    except (FileNotFoundError, KeyError, json.JSONDecodeError, OSError):
        return None


def _command_path(name: str, environment_name: str) -> str:
    configured = os.environ.get(environment_name, "").strip()
    return configured or shutil.which(name) or name


def node_command() -> str:
    configured = os.environ.get("SCHOLARFLOW_NODE", "").strip()
    if configured:
        return configured
    candidates: list[Path] = []
    current = shutil.which("node")
    if current:
        current_path = Path(current)
        candidates.append(current_path)
        candidates.extend(current_path.parent.parent.glob("node-v*/node.exe"))
    program_files = os.environ.get("ProgramFiles", "").strip()
    if program_files:
        candidates.append(Path(program_files) / "nodejs" / "node.exe")
    compatible: list[tuple[Version, Path]] = []
    for candidate in dict.fromkeys(path.resolve() for path in candidates if path.exists()):
        try:
            completed = subprocess.run(
                [str(candidate), "--version"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10, check=False,
            )
            if completed.returncode == 0:
                compatible.append((Version.parse(completed.stdout.strip()), candidate))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    if compatible:
        return str(max(compatible, key=lambda item: item[0])[1])
    return current or "node"


def npm_command() -> str:
    configured = os.environ.get("SCHOLARFLOW_NPM", "").strip()
    if configured:
        return configured
    paired = Path(node_command()).with_name("npm.cmd" if os.name == "nt" else "npm")
    return str(paired) if paired.exists() else (shutil.which("npm") or "npm")


def pi_command(root: Path) -> list[str]:
    configured_cli = os.environ.get("SCHOLARFLOW_PI_CLI", "").strip()
    if configured_cli:
        return [node_command(), configured_cli]
    configured_command = os.environ.get("SCHOLARFLOW_PI_COMMAND", "").strip()
    if configured_command:
        return [configured_command]
    project_cli = root / "packages/scholarflow-pi/node_modules" / PI_PACKAGE / "dist/cli.js"
    if project_cli.exists():
        return [node_command(), str(project_cli)]
    return [_command_path("pi", "SCHOLARFLOW_PI_COMMAND")]


def global_pi_command() -> list[str]:
    configured_command = os.environ.get("SCHOLARFLOW_GLOBAL_PI_COMMAND", "").strip()
    if configured_command:
        return [configured_command]
    prefix = global_pi_prefix()
    if prefix:
        cli = prefix / "node_modules" / PI_PACKAGE / "dist" / "cli.js"
        if cli.exists():
            return [node_command(), str(cli)]
    return [shutil.which("pi") or "pi"]


def global_pi_prefix() -> Path | None:
    configured = os.environ.get("SCHOLARFLOW_GLOBAL_PI_PREFIX", "").strip()
    candidates: list[Path] = [Path(configured)] if configured else []
    resolved = shutil.which("pi")
    if resolved:
        candidates.append(Path(resolved).resolve().parent)
    app_data = os.environ.get("APPDATA", "").strip()
    if app_data:
        candidates.append(Path(app_data) / "npm")
    for candidate in dict.fromkeys(path.resolve() for path in candidates):
        package_path = candidate / "node_modules" / PI_PACKAGE / "package.json"
        if package_path.exists():
            return candidate
    return None


def install_global_pi(root: Path, version: str) -> dict[str, Any]:
    prefix = global_pi_prefix()
    if prefix is None:
        return {"status": "fail", "error": "Could not locate the user-level Pi npm prefix."}
    environment = dict(os.environ)
    environment["npm_config_prefix"] = str(prefix)
    completed = _run(
        [
            npm_command(), "install", "--global", "--ignore-scripts", "--no-audit", "--no-fund",
            f"{PI_PACKAGE}@{version}",
        ],
        cwd=prefix,
        env=environment,
        timeout=600,
        check=False,
    )
    package_version = _package_version(prefix / "node_modules" / PI_PACKAGE / "package.json")
    cli_version = _version_output(global_pi_command(), root) if completed.returncode == 0 else None
    actual = package_version or cli_version
    return {
        "status": "pass" if completed.returncode == 0 and actual == version else "fail",
        "prefix": str(prefix),
        "target_version": version,
        "actual_version": actual,
        "package_version": package_version,
        "cli_version": cli_version,
        "exit_code": completed.returncode,
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
    }


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    effective_env = dict(os.environ if env is None else env)
    selected_node = Path(node_command())
    if selected_node.exists():
        effective_env["PATH"] = f"{selected_node.parent}{os.pathsep}{effective_env.get('PATH', '')}"
    # Nested Python gates inherit a pipe rather than an interactive UTF-8
    # terminal on Windows. Force a stable encoding so rendered build output
    # (for example, a check mark) cannot crash lifecycle qualification.
    effective_env.setdefault("PYTHONIOENCODING", "utf-8")
    effective_env.setdefault("PYTHONUTF8", "1")
    completed = subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        env=effective_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode:
        message = (completed.stderr or completed.stdout or "command failed").strip()
        raise PiLifecycleError(f"{' '.join(command)}: {message}")
    return completed


def _version_output(command: Sequence[str], root: Path) -> str | None:
    try:
        output = _run([*command, "--version"], cwd=root, timeout=20).stdout.strip()
        return str(Version.parse(output.splitlines()[-1]))
    except (OSError, IndexError, ValueError, PiLifecycleError, subprocess.TimeoutExpired):
        return None


def _extension_specs(settings: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for source in settings.get("packages") or []:
        match = NPM_SPEC_RE.match(str(source))
        if match:
            result[match.group(1)] = match.group(2)
    return result


def inspect_runtime(root: Path | None = None) -> dict[str, Any]:
    root = Path(root or repository_root()).resolve()
    policy = _read_json(root / POLICY_PATH)
    package = _read_json(root / PI_PACKAGE_PATH)
    ecosystem = _read_json(root / ECOSYSTEM_MANIFEST_PATH)
    settings = _read_json(root / SETTINGS_PATH)
    tested = str(policy["pi"]["tested_version"])
    minimum_node = str(policy["node"]["minimum"])
    expected_extensions = {
        str(item["package"]): str(item["version"])
        for item in policy.get("extensions") or []
    }
    configured_extensions = _extension_specs(settings)
    reviewed_extensions = {
        str(item["package"]): str(item["version"])
        for item in ecosystem.get("packages") or []
    }
    node_version = _version_output([node_command()], root)
    active_pi_version = _version_output(pi_command(root), root)
    global_pi_version = _version_output(global_pi_command(), root)
    project_pi_version = _package_version(
        root / "packages/scholarflow-pi/node_modules" / PI_PACKAGE / "package.json"
    )
    managed_pi_version = _package_version(root / MANAGED_NPM_PATH / "node_modules" / PI_PACKAGE / "package.json")
    managed_extensions = {
        name: _package_version(root / MANAGED_NPM_PATH / "node_modules" / Path(*name.split("/")) / "package.json")
        for name in expected_extensions
    }

    issues: list[dict[str, Any]] = []
    if node_version is None:
        issues.append({"severity": "error", "code": "node_missing", "expected": minimum_node})
    elif Version.parse(node_version) < Version.parse(minimum_node):
        issues.append({"severity": "error", "code": "node_too_old", "actual": node_version, "expected": minimum_node})
    if str((package.get("devDependencies") or {}).get(PI_PACKAGE) or "") != tested:
        issues.append({"severity": "error", "code": "project_pi_lock_drift", "actual": (package.get("devDependencies") or {}).get(PI_PACKAGE), "expected": tested})
    expected_peer = peer_range_for(tested)
    if str((package.get("peerDependencies") or {}).get(PI_PACKAGE) or "") != expected_peer:
        issues.append({"severity": "error", "code": "pi_peer_range_drift", "actual": (package.get("peerDependencies") or {}).get(PI_PACKAGE), "expected": expected_peer})
    if project_pi_version and project_pi_version != tested:
        issues.append({"severity": "error", "code": "project_pi_install_drift", "actual": project_pi_version, "expected": tested})
    if active_pi_version and active_pi_version != tested:
        issues.append({"severity": "warn", "code": "active_pi_drift", "actual": active_pi_version, "expected": tested})
    if global_pi_version and global_pi_version != tested:
        issues.append({"severity": "warn", "code": "global_pi_drift", "actual": global_pi_version, "expected": tested})
    if managed_pi_version and managed_pi_version != tested:
        issues.append({"severity": "error", "code": "managed_pi_drift", "actual": managed_pi_version, "expected": tested})
    for name, expected in expected_extensions.items():
        configured = configured_extensions.get(name)
        installed = managed_extensions.get(name)
        if configured != expected:
            issues.append({"severity": "error", "code": "extension_pin_drift", "package": name, "actual": configured, "expected": expected})
        reviewed = reviewed_extensions.get(name)
        if reviewed != expected:
            issues.append({"severity": "error", "code": "extension_review_manifest_drift", "package": name, "actual": reviewed, "expected": expected})
        if installed and installed != expected:
            issues.append({"severity": "error", "code": "extension_install_drift", "package": name, "actual": installed, "expected": expected})

    status = "error" if any(item["severity"] == "error" for item in issues) else "warn" if issues else "ok"
    return {
        "schema_version": 1,
        "status": status,
        "node": {"command": node_command(), "version": node_version, "minimum": minimum_node},
        "pi": {
            "package": PI_PACKAGE,
            "tested_version": tested,
            "supported_range": policy["pi"]["supported_range"],
            "active_version": active_pi_version,
            "global_version": global_pi_version,
            "project_version": project_pi_version,
            "managed_peer_version": managed_pi_version,
        },
        "extensions": [
            {
                "package": name,
                "pinned_version": expected,
                "configured_version": configured_extensions.get(name),
                "installed_version": managed_extensions.get(name),
            }
            for name, expected in expected_extensions.items()
        ],
        "issues": issues,
    }


def _query_one_latest(root: Path, package: str) -> tuple[str, str]:
    completed = _run([npm_command(), "view", package, "version", "--json"], cwd=root, timeout=45)
    raw = completed.stdout.strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw.strip('"')
    if isinstance(value, list):
        value = value[-1]
    return package, str(Version.parse(str(value)))


def _cached_latest(root: Path, ttl_hours: float) -> dict[str, Any] | None:
    cache_path = root / CACHE_PATH
    try:
        payload = _read_json(cache_path)
        checked = datetime.fromisoformat(str(payload["checked_at"]).replace("Z", "+00:00"))
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError, OSError):
        return None
    if datetime.now(timezone.utc) - checked > timedelta(hours=ttl_hours):
        return None
    return payload


def query_latest(
    root: Path | None = None,
    *,
    refresh: bool = False,
    cache_ttl_hours: float = 24,
) -> dict[str, Any]:
    root = Path(root or repository_root()).resolve()
    if not refresh:
        cached = _cached_latest(root, cache_ttl_hours)
        if cached is not None:
            return {**cached, "source": "cache"}
    policy = _read_json(root / POLICY_PATH)
    packages = [str(policy["pi"]["package"]), *[str(item["package"]) for item in policy.get("extensions") or []]]
    versions: dict[str, str] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(5, len(packages))) as executor:
        futures = {executor.submit(_query_one_latest, root, package): package for package in packages}
        for future in as_completed(futures):
            package = futures[future]
            try:
                name, version = future.result()
                versions[name] = version
            except Exception as error:  # Network/tool errors belong in the report.
                errors[package] = str(error)
    payload = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "source": "registry",
        "versions": dict(sorted(versions.items())),
        "errors": dict(sorted(errors.items())),
    }
    _write_json(root / CACHE_PATH, payload)
    return payload


def build_update_plan(
    root: Path | None = None,
    *,
    refresh: bool = False,
    cache_ttl_hours: float = 24,
    target_pi: str | None = None,
    allow_minor: bool = False,
    allow_major: bool = False,
) -> dict[str, Any]:
    root = Path(root or repository_root()).resolve()
    policy = _read_json(root / POLICY_PATH)
    latest = query_latest(root, refresh=refresh, cache_ttl_hours=cache_ttl_hours)
    known_blockers = {
        (str(item.get("package") or ""), str(version)): item
        for item in (policy.get("security") or {}).get("known_upstream_blockers") or []
        for version in item.get("versions") or []
    }
    components = [
        {
            "kind": "pi",
            "package": str(policy["pi"]["package"]),
            "current": str(policy["pi"]["tested_version"]),
            "automatic_scope": str(policy["pi"].get("automatic_scope") or "patch"),
        },
        *[
            {
                "kind": "extension",
                "package": str(item["package"]),
                "current": str(item["version"]),
                "automatic_scope": str(item.get("automatic_scope") or "patch"),
            }
            for item in policy.get("extensions") or []
        ],
    ]
    planned: list[dict[str, Any]] = []
    for component in components:
        package = component["package"]
        target = target_pi if component["kind"] == "pi" and target_pi not in (None, "latest") else latest["versions"].get(package)
        if not target:
            planned.append({**component, "target": None, "scope": None, "action": "unavailable", "allowed": False})
            continue
        current_version = Version.parse(component["current"])
        target_version = Version.parse(str(target))
        scope = version_scope(component["current"], str(target))
        security_blocker = known_blockers.get((package, str(target)))
        allowed_rank = SCOPE_RANK[component["automatic_scope"]]
        if allow_minor:
            allowed_rank = max(allowed_rank, SCOPE_RANK["minor"])
        if allow_major:
            allowed_rank = SCOPE_RANK["major"]
        if security_blocker:
            action = "security_blocked"
            allowed = False
        elif target_version < current_version:
            action = "downgrade_blocked"
            allowed = False
        elif scope == "none":
            action = "current"
            allowed = True
        elif SCOPE_RANK[scope] <= allowed_rank:
            action = "update"
            allowed = True
        else:
            action = "review_required"
            allowed = False
        planned_item = {
            **component,
            "target": str(target),
            "scope": scope,
            "action": action,
            "allowed": allowed,
        }
        if security_blocker:
            planned_item["security_blocker"] = {
                "advisory": security_blocker.get("advisory"),
                "dependency": security_blocker.get("dependency"),
                "reason": security_blocker.get("reason"),
            }
        planned.append(planned_item)
    updates = [item for item in planned if item["action"] == "update"]
    blocked = [
        item for item in planned
        if item["action"] in {"review_required", "downgrade_blocked", "security_blocked"}
    ]
    unavailable = [item for item in planned if item["action"] == "unavailable"]
    if updates and blocked:
        status = "partial_update"
    elif blocked:
        status = "blocked"
    elif updates:
        status = "update_available"
    elif unavailable:
        status = "unavailable"
    else:
        status = "current"
    return {
        "schema_version": 1,
        "status": status,
        "checked_at": latest["checked_at"],
        "source": latest["source"],
        "components": planned,
        "update_count": len(updates),
        "blocked_count": len(blocked),
        "errors": latest.get("errors") or {},
    }


def _tail(value: str, limit: int = 5000) -> str:
    return value[-limit:] if len(value) > limit else value


def qualify_candidate(
    root: Path,
    target_version: str,
    *,
    extension_versions: dict[str, str] | None = None,
) -> dict[str, Any]:
    target = str(Version.parse(target_version))
    policy = _read_json(root / POLICY_PATH)
    package = _read_json(root / PI_PACKAGE_PATH)
    typebox_version = str((package.get("devDependencies") or {}).get("typebox") or "1.3.6")
    candidate_extensions = dict(extension_versions or {})
    dependencies = {PI_PACKAGE: target, "typebox": typebox_version, **candidate_extensions}
    manifest = {
        "name": "scholarflow-pi-canary",
        "private": True,
        "dependencies": dict(sorted(dependencies.items())),
        "overrides": dict(sorted((policy.get("security") or {}).get("overrides", {}).items())),
    }
    lifecycle_root = root / ".tmp" / "pi-lifecycle"
    canary_root: Path | None = None
    reused = False
    for existing in sorted(
        lifecycle_root.glob(f"canary-{target}-*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ):
        try:
            if _read_json(existing / "package.json") != manifest:
                continue
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        existing_cli = existing / "node_modules" / PI_PACKAGE / "dist" / "cli.js"
        if not existing_cli.exists() or not (existing / "package-lock.json").exists():
            continue
        integrity = _run(
            [npm_command(), "ls", "--all", "--json"],
            cwd=existing,
            timeout=180,
            check=False,
        )
        if integrity.returncode == 0:
            canary_root = existing
            reused = True
            break
    if canary_root is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        canary_root = lifecycle_root / f"canary-{target}-{stamp}"
        canary_root.mkdir(parents=True, exist_ok=False)
        _write_json(canary_root / "package.json", manifest)
        install = _run(
            [
                npm_command(), "install", "--ignore-scripts", "--no-audit", "--no-fund",
            ],
            cwd=canary_root,
            timeout=900,
            check=False,
        )
    else:
        install = subprocess.CompletedProcess(
            args=[npm_command(), "install"],
            returncode=0,
            stdout="Reused an integrity-checked candidate installation.\n",
            stderr="",
        )
    cli_path = canary_root / "node_modules" / PI_PACKAGE / "dist" / "cli.js"
    result: dict[str, Any] = {
        "target_version": target,
        "canary_root": str(canary_root),
        "install_exit_code": install.returncode,
        "install_stdout_tail": _tail(install.stdout),
        "install_stderr_tail": _tail(install.stderr),
        "reused": reused,
        "status": "fail",
    }
    if install.returncode or not cli_path.exists():
        return result
    candidate_audit = _audit_npm_runtime(
        root,
        canary_root,
        block_level=str((policy.get("security") or {}).get("audit_block_level") or "high"),
    )
    result["audit_before_native_rebuild"] = candidate_audit
    if candidate_audit["status"] != "pass":
        result["phase"] = "dependency_audit"
        return result
    native_rebuild = _rebuild_reviewed_native_dependencies(canary_root, policy)
    result["native_rebuild"] = native_rebuild
    if native_rebuild["status"] != "pass":
        result["phase"] = "native_rebuild"
        return result
    candidate_audit = _audit_npm_runtime(
        root,
        canary_root,
        block_level=str((policy.get("security") or {}).get("audit_block_level") or "high"),
    )
    result["audit"] = candidate_audit
    if candidate_audit["status"] != "pass":
        result["phase"] = "post_rebuild_dependency_audit"
        return result
    evaluation_project = canary_root / "project"
    settings_packages = [str((root / "packages/scholarflow-pi").resolve())]
    settings_packages.extend(
        str((canary_root / "node_modules" / Path(*name.split("/"))).resolve())
        for name in sorted(candidate_extensions)
    )
    _write_json(evaluation_project / ".pi" / "settings.json", {"packages": settings_packages})
    environment = dict(os.environ)
    environment.update({
        "SCHOLARFLOW_PI_CLI": str(cli_path),
        "SCHOLARFLOW_NODE": node_command(),
        "SCHOLARFLOW_DISABLE_UPDATE_CHECK": "1",
        "SCHOLARFLOW_EVAL_CWD": str(evaluation_project),
        "SCHOLARFLOW_CANARY_QUALIFICATION": "1",
    })
    checks: list[dict[str, Any]] = []
    for command_text in policy.get("qualification", {}).get("commands") or ["python scripts/quality.py"]:
        command = [sys.executable, "scripts/quality.py"] if command_text == "python scripts/quality.py" else command_text.split()
        completed = _run(command, cwd=root, env=environment, timeout=900, check=False)
        checks.append({
            "command": command_text,
            "exit_code": completed.returncode,
            "stdout_tail": _tail(completed.stdout),
            "stderr_tail": _tail(completed.stderr),
        })
        if completed.returncode:
            break
    result["checks"] = checks
    result["status"] = "pass" if checks and all(item["exit_code"] == 0 for item in checks) else "fail"
    return result


def _update_settings_versions(settings: dict[str, Any], versions: dict[str, str]) -> dict[str, Any]:
    updated = dict(settings)
    packages: list[Any] = []
    for source in settings.get("packages") or []:
        match = NPM_SPEC_RE.match(str(source))
        if match and match.group(1) in versions:
            packages.append(f"npm:{match.group(1)}@{versions[match.group(1)]}")
        else:
            packages.append(source)
    updated["packages"] = packages
    return updated


def sync_managed_runtime(root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    managed_root = root / MANAGED_NPM_PATH
    dependencies = {str(policy["pi"]["package"]): str(policy["pi"]["tested_version"])}
    dependencies.update({str(item["package"]): str(item["version"]) for item in policy.get("extensions") or []})
    manifest = {
        "name": "pi-extensions",
        "private": True,
        "dependencies": dict(sorted(dependencies.items())),
        "overrides": dict(sorted((policy.get("security") or {}).get("overrides", {}).items())),
    }
    block_level = str((policy.get("security") or {}).get("audit_block_level") or "high")
    if (managed_root / "package-lock.json").exists():
        try:
            manifest_matches = _read_json(managed_root / "package.json") == manifest
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            manifest_matches = False
        if manifest_matches:
            integrity = _run(
                [npm_command(), "ls", "--all", "--json"],
                cwd=managed_root,
                timeout=180,
                check=False,
            )
            audit_before = _audit_npm_runtime(
                root, managed_root, block_level=block_level,
            )
            if integrity.returncode == 0 and audit_before["status"] == "pass":
                native_rebuild = _rebuild_reviewed_native_dependencies(
                    managed_root, policy,
                )
                audit_after = _audit_npm_runtime(
                    root, managed_root, block_level=block_level,
                )
                if native_rebuild["status"] == "pass" and audit_after["status"] == "pass":
                    return {
                        "status": "pass",
                        "exit_code": 0,
                        "stdout_tail": "Reused an integrity-checked managed runtime.\n",
                        "stderr_tail": "",
                        "reused": True,
                        "audit_before_native_rebuild": audit_before,
                        "native_rebuild": native_rebuild,
                        "audit": audit_after,
                        "activated": True,
                    }
    managed_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = managed_root.parent / f"{managed_root.name}-staging-{os.getpid()}"
    backup_root = managed_root.parent / f"{managed_root.name}-backup-{os.getpid()}"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    if backup_root.exists():
        shutil.rmtree(backup_root)
    _write_json(staging_root / "package.json", manifest)
    completed = _run(
        [npm_command(), "install", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=staging_root,
        timeout=600,
        check=False,
    )
    result: dict[str, Any] = {
        "status": "pass" if completed.returncode == 0 else "fail",
        "exit_code": completed.returncode,
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
    }
    if completed.returncode:
        shutil.rmtree(staging_root, ignore_errors=True)
        return result
    result["audit_before_native_rebuild"] = _audit_npm_runtime(
        root, staging_root, block_level=block_level,
    )
    if result["audit_before_native_rebuild"]["status"] != "pass":
        result["status"] = "fail"
        shutil.rmtree(staging_root, ignore_errors=True)
        return result
    result["native_rebuild"] = _rebuild_reviewed_native_dependencies(
        staging_root, policy,
    )
    if result["native_rebuild"]["status"] != "pass":
        result["status"] = "fail"
        shutil.rmtree(staging_root, ignore_errors=True)
        return result
    result["audit"] = _audit_npm_runtime(
        root, staging_root, block_level=block_level,
    )
    if result["audit"]["status"] != "pass":
        result["status"] = "fail"
        shutil.rmtree(staging_root, ignore_errors=True)
        return result
    try:
        if managed_root.exists():
            managed_root.rename(backup_root)
        staging_root.rename(managed_root)
        shutil.rmtree(backup_root, ignore_errors=True)
        result["activated"] = True
    except OSError as error:
        result["status"] = "fail"
        result["activation_error"] = str(error)
        if not managed_root.exists() and backup_root.exists():
            backup_root.rename(managed_root)
        shutil.rmtree(staging_root, ignore_errors=True)
    return result


def _rebuild_reviewed_native_dependencies(
    runtime_path: Path,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Run lifecycle scripts only for audited, explicitly reviewed packages."""
    allowlist = [
        str(item).strip()
        for item in (policy.get("qualification") or {}).get("native_rebuild_allowlist", [])
        if str(item).strip()
    ]
    checks: list[dict[str, Any]] = []
    for package in allowlist:
        completed = _run(
            [npm_command(), "rebuild", package, "--foreground-scripts"],
            cwd=runtime_path,
            timeout=600,
            check=False,
        )
        checks.append({
            "package": package,
            "exit_code": completed.returncode,
            "stdout_tail": _tail(completed.stdout),
            "stderr_tail": _tail(completed.stderr),
        })
        if completed.returncode:
            break
    return {
        "status": "pass" if all(item["exit_code"] == 0 for item in checks) else "fail",
        "allowlist": allowlist,
        "checks": checks,
    }


def _audit_npm_runtime(
    root: Path,
    runtime_path: Path,
    *,
    block_level: str,
) -> dict[str, Any]:
    threshold = block_level
    completed = _run(
        [npm_command(), "audit", "--omit=dev", "--json"],
        cwd=runtime_path,
        timeout=180,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "block_level": threshold, "error": _tail(completed.stderr or completed.stdout)}
    counts = dict((payload.get("metadata") or {}).get("vulnerabilities") or {})
    blocking_count = sum(int(counts.get(level, 0)) for level, rank in SEVERITY_RANK.items() if rank >= SEVERITY_RANK[threshold])
    advisories = []
    for name, detail in sorted((payload.get("vulnerabilities") or {}).items()):
        severity = str(detail.get("severity") or "info")
        advisories.append({
            "package": name,
            "severity": severity,
            "direct": bool(detail.get("isDirect")),
            "via": [item if isinstance(item, str) else item.get("title") for item in detail.get("via") or []],
            "fix_available": bool(detail.get("fixAvailable")),
        })
    return {
        "status": "fail" if blocking_count else "pass",
        "block_level": threshold,
        "blocking_count": blocking_count,
        "counts": counts,
        "advisories": advisories,
    }


def audit_managed_runtime(root: Path | None = None, *, block_level: str | None = None) -> dict[str, Any]:
    root = Path(root or repository_root()).resolve()
    policy = _read_json(root / POLICY_PATH)
    threshold = block_level or str((policy.get("security") or {}).get("audit_block_level") or "high")
    return _audit_npm_runtime(
        root,
        root / MANAGED_NPM_PATH,
        block_level=threshold,
    )


def _snapshot(paths: Iterable[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def _restore(snapshot: dict[Path, bytes | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            if path.exists():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def apply_update(
    root: Path | None = None,
    *,
    target_pi: str | None = None,
    refresh: bool = True,
    allow_minor: bool = False,
    allow_major: bool = False,
    core_only: bool = False,
    update_global: bool = False,
) -> dict[str, Any]:
    root = Path(root or repository_root()).resolve()
    before = inspect_runtime(root)
    plan = build_update_plan(
        root,
        refresh=refresh,
        target_pi=target_pi,
        allow_minor=allow_minor,
        allow_major=allow_major,
    )
    eligible_updates = [
        item for item in plan["components"]
        if item["action"] == "update"
        and item["allowed"]
        and (item["kind"] == "pi" or not core_only)
    ]
    deferred = [
        item for item in plan["components"]
        if not item["allowed"] and (item["kind"] == "pi" or not core_only)
    ]
    if not eligible_updates and deferred:
        return {"schema_version": 1, "status": "blocked", "phase": "plan", "plan": plan, "before": before}
    if not eligible_updates:
        return {"schema_version": 1, "status": "current", "phase": "plan", "plan": plan, "before": before}
    pi_item = next(item for item in plan["components"] if item["kind"] == "pi")
    target = str(
        pi_item["target"]
        if pi_item["action"] == "update" and pi_item["allowed"]
        else pi_item["current"]
    )
    candidate_extensions = {
        item["package"]: str(
            item["target"]
            if item["action"] == "update" and item["allowed"] and not core_only
            else item["current"]
        )
        for item in plan["components"]
        if item["kind"] == "extension"
    }
    canary = qualify_candidate(root, target, extension_versions=candidate_extensions)
    report: dict[str, Any] = {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "fail",
        "phase": "canary",
        "before": before,
        "plan": plan,
        "deferred_components": deferred,
        "canary": canary,
    }
    if canary["status"] != "pass":
        _write_json(root / LAST_REPORT_PATH, report)
        return report

    policy_path = root / POLICY_PATH
    package_path = root / PI_PACKAGE_PATH
    lock_path = root / PI_PACKAGE_LOCK_PATH
    ecosystem_path = root / ECOSYSTEM_MANIFEST_PATH
    settings_path = root / SETTINGS_PATH
    snapshot = _snapshot([policy_path, package_path, lock_path, ecosystem_path, settings_path])
    old_global = before["pi"].get("global_version")
    global_changed = False
    try:
        policy = _read_json(policy_path)
        package = _read_json(package_path)
        ecosystem = _read_json(ecosystem_path)
        settings = _read_json(settings_path)
        policy["pi"]["tested_version"] = target
        policy["pi"]["supported_range"] = peer_range_for(target)
        package.setdefault("devDependencies", {})[PI_PACKAGE] = target
        package.setdefault("peerDependencies", {})[PI_PACKAGE] = peer_range_for(target)
        extension_updates = {
            item["package"]: item["target"]
            for item in plan["components"]
            if item["kind"] == "extension" and item["action"] == "update" and item["allowed"] and not core_only
        }
        for item in policy.get("extensions") or []:
            if item["package"] in extension_updates:
                item["version"] = extension_updates[item["package"]]
        for item in ecosystem.get("packages") or []:
            if item["package"] in extension_updates:
                item["version"] = extension_updates[item["package"]]
        settings = _update_settings_versions(settings, extension_updates)
        _write_json(policy_path, policy)
        _write_json(package_path, package)
        _write_json(ecosystem_path, ecosystem)
        _write_json(settings_path, settings)

        package_install = _run(
            [npm_command(), "install", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=root / "packages/scholarflow-pi",
            timeout=600,
            check=False,
        )
        if package_install.returncode:
            raise PiLifecycleError(package_install.stderr or package_install.stdout)
        managed = sync_managed_runtime(root, policy)
        report["managed_runtime"] = managed
        if managed["status"] != "pass":
            raise PiLifecycleError(managed["stderr_tail"] or managed["stdout_tail"])

        if update_global and old_global != target:
            global_update = _run([*global_pi_command(), "update", "--self", "--approve"], cwd=root, timeout=600, check=False)
            report["global_update"] = {
                "exit_code": global_update.returncode,
                "stdout_tail": _tail(global_update.stdout),
                "stderr_tail": _tail(global_update.stderr),
            }
            if global_update.returncode:
                fallback = install_global_pi(root, target)
                report["global_update_fallback"] = fallback
                if fallback["status"] != "pass":
                    raise PiLifecycleError(fallback.get("error") or fallback.get("stderr_tail") or fallback.get("stdout_tail") or "Global Pi fallback failed.")
            global_changed = True
            prefix = global_pi_prefix()
            actual_global = _package_version(prefix / "node_modules" / PI_PACKAGE / "package.json") if prefix else None
            if actual_global != target:
                raise PiLifecycleError(f"Global Pi resolved to {actual_global}, expected {target}.")
            model_refresh = _run([*global_pi_command(), "update", "--models", "--approve"], cwd=root, timeout=180, check=False)
            report["model_refresh"] = {"exit_code": model_refresh.returncode, "stderr_tail": _tail(model_refresh.stderr)}

        environment = dict(os.environ)
        environment["SCHOLARFLOW_DISABLE_UPDATE_CHECK"] = "1"
        postflight = _run([sys.executable, "scripts/quality.py"], cwd=root, env=environment, timeout=900, check=False)
        report["postflight"] = {
            "exit_code": postflight.returncode,
            "stdout_tail": _tail(postflight.stdout),
            "stderr_tail": _tail(postflight.stderr),
        }
        if postflight.returncode:
            raise PiLifecycleError(postflight.stderr or postflight.stdout)
        report["audit"] = audit_managed_runtime(root)
        if report["audit"]["status"] != "pass":
            raise PiLifecycleError(
                f"Dependency audit blocked the update at {report['audit']['block_level']} severity."
            )
        report["after"] = inspect_runtime(root)
        report["status"] = "pass"
        report["phase"] = "complete"
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as error:
        report["error"] = str(error)
        report["phase"] = "rollback"
        _restore(snapshot)
        rollback_policy = _read_json(policy_path)
        report["rollback_managed_runtime"] = sync_managed_runtime(root, rollback_policy)
        _run(
            [npm_command(), "install", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=root / "packages/scholarflow-pi",
            timeout=600,
            check=False,
        )
        if global_changed and old_global:
            report["rollback_global"] = install_global_pi(root, str(old_global))
        report["status"] = "rolled_back"
    _write_json(root / LAST_REPORT_PATH, report)
    return report


def _print(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        # JSON is a machine interface and must remain printable even when the
        # Windows host console uses GBK while captured build tails contain
        # characters such as U+2713.
        print(json.dumps(payload, indent=2, ensure_ascii=True))
        return
    print(f"status: {payload.get('status', 'unknown')}")
    if "pi" in payload:
        pi = payload["pi"]
        print(f"Pi tested={pi.get('tested_version')} active={pi.get('active_version')} global={pi.get('global_version')}")
    for issue in payload.get("issues") or []:
        print(f"[{issue['severity']}] {issue['code']}: {issue}")
    for component in payload.get("components") or []:
        print(f"{component['package']}: {component['current']} -> {component.get('target')} ({component['action']})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ScholarFlow controlled Pi lifecycle manager.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", help="Inspect Node, Pi, extension, and lock consistency without network access.")
    status.add_argument("--json", action="store_true")
    status.add_argument("--strict", action="store_true", help="Return non-zero when an error-level drift is found.")
    check = subparsers.add_parser("check", help="Check the npm registry for Pi and extension updates without changing files.")
    check.add_argument("--json", action="store_true")
    check.add_argument("--refresh", action="store_true")
    check.add_argument("--cache-ttl-hours", type=float, default=24)
    check.add_argument("--fail-on-update", action="store_true")
    canary = subparsers.add_parser("canary", help="Install a candidate Pi in isolation and run deterministic qualification.")
    canary.add_argument("--target", default="latest")
    canary.add_argument("--json", action="store_true")
    canary.add_argument("--refresh", action="store_true")
    canary.add_argument("--allow-minor", action="store_true")
    canary.add_argument("--allow-major", action="store_true")
    upgrade = subparsers.add_parser("upgrade", help="Canary first, then update tracked locks and managed runtimes.")
    upgrade.add_argument("--target", default="latest")
    upgrade.add_argument("--apply", action="store_true", help="Apply after qualification; otherwise print the plan only.")
    upgrade.add_argument("--json", action="store_true")
    upgrade.add_argument("--refresh", action="store_true")
    upgrade.add_argument("--allow-minor", action="store_true")
    upgrade.add_argument("--allow-major", action="store_true")
    upgrade.add_argument("--core-only", action="store_true")
    upgrade.add_argument("--global", dest="update_global", action="store_true", help="Also update the user-level Pi CLI.")
    sync = subparsers.add_parser("sync", help="Rebuild ignored .pi/npm runtime from the reviewed project policy.")
    sync.add_argument("--json", action="store_true")
    audit = subparsers.add_parser("audit", help="Audit the managed Pi runtime dependency graph.")
    audit.add_argument("--json", action="store_true")
    audit.add_argument("--block-level", choices=tuple(SEVERITY_RANK), default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = repository_root()
    try:
        if args.command == "status":
            payload = inspect_runtime(root)
            _print(payload, as_json=args.json)
            return 1 if args.strict and payload["status"] == "error" else 0
        if args.command == "check":
            payload = build_update_plan(root, refresh=args.refresh, cache_ttl_hours=args.cache_ttl_hours)
            _print(payload, as_json=args.json)
            return 3 if args.fail_on_update and payload["update_count"] else 0
        if args.command == "canary":
            plan = build_update_plan(
                root,
                refresh=args.refresh,
                target_pi=args.target,
                allow_minor=args.allow_minor,
                allow_major=args.allow_major,
            )
            pi_item = next(item for item in plan["components"] if item["kind"] == "pi")
            if not pi_item["allowed"]:
                payload = {"status": "blocked", "plan": plan}
            else:
                extension_versions = {
                    item["package"]: str(item["target"] if item["allowed"] and item["target"] else item["current"])
                    for item in plan["components"]
                    if item["kind"] == "extension"
                }
                payload = qualify_candidate(
                    root,
                    str(pi_item["target"]),
                    extension_versions=extension_versions,
                )
            _print(payload, as_json=args.json)
            return 0 if payload["status"] == "pass" else 1
        if args.command == "upgrade":
            if not args.apply:
                payload = build_update_plan(
                    root,
                    refresh=args.refresh,
                    target_pi=args.target,
                    allow_minor=args.allow_minor,
                    allow_major=args.allow_major,
                )
            else:
                payload = apply_update(
                    root,
                    target_pi=args.target,
                    refresh=args.refresh,
                    allow_minor=args.allow_minor,
                    allow_major=args.allow_major,
                    core_only=args.core_only,
                    update_global=args.update_global,
                )
            _print(payload, as_json=args.json)
            return 0 if payload["status"] in {"pass", "current", "update_available", "partial_update"} else 1
        if args.command == "sync":
            payload = sync_managed_runtime(root, _read_json(root / POLICY_PATH))
            _print(payload, as_json=args.json)
            return 0 if payload["status"] == "pass" else 1
        if args.command == "audit":
            payload = audit_managed_runtime(root, block_level=args.block_level)
            _print(payload, as_json=args.json)
            return 0 if payload["status"] == "pass" else 1
    except (PiLifecycleError, OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        payload = {"status": "error", "error": str(error)}
        _print(payload, as_json=getattr(args, "json", False))
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
