"""Time-bounded dependency-audit exception evaluation."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping


class DependencyGateError(ValueError):
    """Raised when an audit or exception manifest is malformed."""


def evaluate_dependency_audit(
    audit: Mapping[str, Any],
    exception_manifest: Mapping[str, Any],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Evaluate production dependency findings against explicit exceptions."""

    effective_today = today or date.today()
    vulnerabilities = audit.get("vulnerabilities")
    if not isinstance(vulnerabilities, Mapping):
        raise DependencyGateError("npm audit payload is missing vulnerabilities")
    raw_exceptions = exception_manifest.get("exceptions")
    if not isinstance(raw_exceptions, list):
        raise DependencyGateError("dependency exception manifest must contain an exceptions array")

    exceptions: dict[str, Mapping[str, Any]] = {}
    expired: list[str] = []
    malformed: list[str] = []
    for raw in raw_exceptions:
        if not isinstance(raw, Mapping):
            malformed.append("<non-object>")
            continue
        package = str(raw.get("package") or "").strip()
        expires_on = str(raw.get("expires_on") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        unreachable = raw.get("unreachable_features")
        if (
            not package
            or not expires_on
            or not reason
            or not isinstance(unreachable, list)
            or not unreachable
        ):
            malformed.append(package or "<missing-package>")
            continue
        try:
            expiry = date.fromisoformat(expires_on)
        except ValueError:
            malformed.append(package)
            continue
        exceptions[package] = raw
        if expiry < effective_today:
            expired.append(package)

    high_or_critical: dict[str, str] = {}
    critical: list[str] = []
    for package, raw in vulnerabilities.items():
        if not isinstance(raw, Mapping):
            continue
        severity = str(raw.get("severity") or "").casefold()
        if severity in {"high", "critical"}:
            high_or_critical[str(package)] = severity
        if severity == "critical":
            critical.append(str(package))

    excepted = sorted(
        package
        for package in high_or_critical
        if package in exceptions and package not in expired
    )
    unapproved = sorted(set(high_or_critical) - set(excepted))
    stale = sorted(set(exceptions) - set(high_or_critical))
    passed = not critical and not expired and not malformed and not unapproved
    return {
        "status": "pass" if passed else "fail",
        "scope": "production",
        "date": effective_today.isoformat(),
        "high_or_critical": high_or_critical,
        "excepted": excepted,
        "unapproved": unapproved,
        "critical": sorted(critical),
        "expired_exceptions": sorted(expired),
        "malformed_exceptions": sorted(malformed),
        "stale_exceptions": stale,
    }
