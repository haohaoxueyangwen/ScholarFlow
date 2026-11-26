"""Audit production Workbench dependencies with time-bounded exceptions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scholarflow.dependency_gate import (  # noqa: E402
    DependencyGateError,
    evaluate_dependency_audit,
)

WORKBENCH = ROOT / "apps" / "research-workbench"
DEFAULT_EXCEPTIONS = WORKBENCH / "quality" / "dependency-exceptions.json"


def _run_audit() -> dict[str, Any]:
    npm = shutil.which("npm")
    if not npm:
        raise DependencyGateError("npm is required for the Workbench dependency gate")
    completed = subprocess.run(
        [npm, "audit", "--omit=dev", "--json"],
        cwd=WORKBENCH,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DependencyGateError("npm audit did not return valid JSON") from exc
    if completed.returncode not in {0, 1}:
        raise DependencyGateError(
            f"npm audit failed with exit code {completed.returncode}"
        )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-file", type=Path)
    parser.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTIONS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        audit = (
            json.loads(args.audit_file.read_text(encoding="utf-8-sig"))
            if args.audit_file
            else _run_audit()
        )
        exceptions = json.loads(args.exceptions.read_text(encoding="utf-8"))
        report = evaluate_dependency_audit(audit, exceptions)
    except (OSError, json.JSONDecodeError, DependencyGateError) as exc:
        report = {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
