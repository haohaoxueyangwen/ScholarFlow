"""Shared deterministic contracts for storage and governed KB writes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class KBConstitutionCheck:
    name: str
    passed: bool
    severity: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class KBWriteRequest:
    title: str
    content_kind: str
    body: str
    source_context: Dict[str, Any] = field(default_factory=dict)
    target_path: Optional[str] = None
    related_links: List[str] = field(default_factory=list)
    source_paths: List[str] = field(default_factory=list)
    side_effects_acknowledged: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "content_kind": self.content_kind,
            "body": self.body,
            "source_context": dict(self.source_context),
            "target_path": self.target_path,
            "related_links": list(self.related_links),
            "source_paths": list(self.source_paths),
            "side_effects_acknowledged": list(self.side_effects_acknowledged),
        }


@dataclass(frozen=True)
class KBWriteDecision:
    source_context: Dict[str, Any]
    content_kind: str
    target_section: str
    target_path: str
    decision: str
    allowed: bool
    reason: str
    missing_links: List[str] = field(default_factory=list)
    required_side_effects: List[str] = field(default_factory=list)
    constitution_checks: List[KBConstitutionCheck] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_context": dict(self.source_context),
            "content_kind": self.content_kind,
            "target_section": self.target_section,
            "target_path": self.target_path,
            "decision": self.decision,
            "allowed": self.allowed,
            "reason": self.reason,
            "missing_links": list(self.missing_links),
            "required_side_effects": list(self.required_side_effects),
            "constitution_checks": [item.to_dict() for item in self.constitution_checks],
        }


@dataclass(frozen=True)
class StorageEnvelope:
    object_type: str
    operation: str
    object_id: str
    record: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "object_type": self.object_type,
            "operation": self.operation,
            "object_id": self.object_id,
            "record": dict(self.record),
        }
