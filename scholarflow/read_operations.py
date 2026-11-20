"""Small deterministic read boundary. It never runs a model or dispatches writes."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable

from .domain_queries import DomainQueryError
from .persistence import file_lock
from .storage import JsonEntityStore, ScholarFlowStore


class ReadOperationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Field:
    kind: type
    required: bool = False
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class Operation:
    name: str
    capability: str
    scope_kind: str
    fields: dict[str, Field]
    handler: Callable[[ScholarFlowStore, dict], dict]
    stage_field: str | None = None
    resources: tuple[str, ...] = ()
    context_handler: Callable | None = None

    def validate(self, params: Any) -> dict:
        if not isinstance(params, dict) or set(params) - self.fields.keys():
            raise ReadOperationError("invalid_argument", "Unsupported or non-object params")
        normalized = dict(params)
        for name, rule in self.fields.items():
            if name not in normalized:
                if rule.required:
                    raise ReadOperationError("invalid_argument", f"Required parameter: {name}")
                continue
            value = normalized[name]
            if type(value) is not rule.kind:
                raise ReadOperationError("invalid_argument", f"Invalid type: {name}")
            if rule.kind is str:
                if len(value) > 4000 or (rule.required and not value.strip()):
                    raise ReadOperationError("invalid_argument", f"Invalid text: {name}")
                normalized[name] = value.strip()
            if rule.kind is int and ((rule.minimum is not None and value < rule.minimum) or (rule.maximum is not None and value > rule.maximum)):
                raise ReadOperationError("invalid_argument", f"Out of range: {name}")
        return normalized


@dataclass(frozen=True)
class ReadGrant:
    """Host/adapter context, never taken from the model request body."""
    principal: str
    capabilities: frozenset[str]
    project_catalog: bool = False
    paper_catalog: bool = False
    stage_ids: frozenset[str] = frozenset()

    @classmethod
    def from_dict(cls, value: dict) -> "ReadGrant":
        if not isinstance(value, dict) or set(value) - {"principal", "capabilities", "project_catalog", "paper_catalog", "stage_ids"}:
            raise ReadOperationError("invalid_argument", "Invalid trusted grant configuration")
        principal = value.get("principal")
        capabilities, stages = value.get("capabilities", []), value.get("stage_ids", [])
        if not isinstance(principal, str) or not principal.strip() or len(principal) > 160:
            raise ReadOperationError("invalid_argument", "Grant principal required")
        for entries in (capabilities, stages):
            if not isinstance(entries, list) or len(entries) > 100 or any(not isinstance(x, str) or not x.strip() or len(x) > 200 for x in entries):
                raise ReadOperationError("invalid_argument", "Invalid grant resource list")
        if any(type(value.get(key, False)) is not bool for key in ("project_catalog", "paper_catalog")):
            raise ReadOperationError("invalid_argument", "Grant permissions must be booleans")
        return cls(principal, frozenset(capabilities), value.get("project_catalog", False), value.get("paper_catalog", False), frozenset(stages))


@dataclass
class ReadControl:
    timeout_ms: int = 15000
    cancelled: threading.Event = field(default_factory=threading.Event)
    started: float = field(default_factory=time.monotonic)

    def check(self):
        if self.cancelled.is_set():
            raise ReadOperationError("cancelled", "Read cancelled; synchronous I/O may finish before cancellation is observed")
        if (time.monotonic() - self.started) * 1000 >= self.timeout_ms:
            raise ReadOperationError("timeout", "Read deadline elapsed; a completed read result is not returned")


class ReadSnapshot:
    """Request-local snapshots of existing entity files, not another persistent store."""
    def __init__(self, data_dir: Path, control: ReadControl, resources: tuple[str, ...]):
        if not data_dir.is_dir():
            raise ReadOperationError("dependency_unavailable", "An existing local data directory is required")
        self.store = ScholarFlowStore(data_dir)
        self.versions: dict[str, dict] = {}
        self.entities = {}
        for entity in vars(self.store).values():
            if isinstance(entity, JsonEntityStore):
                name = entity.file_path.name
                self.entities[name] = entity
                entity._load = self._reader(entity, control, resources)

    def _reader(self, entity, control, resources):
        cached = None
        def load():
            nonlocal cached
            control.check()
            if entity.file_path.name not in resources:
                raise ReadOperationError("permission_denied", "Read exceeds the operation's declared source resources")
            if cached is None:
                try:
                    raw = entity.file_path.read_bytes()
                except FileNotFoundError:
                    raw = None
                cached = json.loads(raw.decode("utf-8")) if raw is not None else {"version": 2, "revision": 0, "object_type": entity.model_cls.object_type, "items": {}}
                if not isinstance(cached, dict) or not isinstance(cached.get("items"), dict):
                    raise ReadOperationError("internal_error", "Invalid local entity storage envelope")
                self.versions[entity.file_path.name] = {"resource": entity.file_path.name, "present": raw is not None,
                    "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
                    "revision": cached.get("revision") if raw is not None else None,
                    "schema_version": cached.get("version") if raw is not None else None}
            return deepcopy(cached)
        return load


class ReadFacade:
    def __init__(self, operations: list[Operation], data_dir: str | Path | None, audit_path: str | Path | None = None):
        self.operations = {operation.name: operation for operation in operations}
        if len(self.operations) != len(operations):
            raise ValueError("Duplicate read operation name")
        self.data_dir = Path(data_dir).resolve() if data_dir else None
        self.audit_path = Path(audit_path).resolve() if audit_path else None
        if self.audit_path and self.data_dir and self.audit_path.is_relative_to(self.data_dir):
            raise ValueError("Read audit must be outside the domain data directory")

    def describe(self, grant: ReadGrant) -> dict:
        return {"protocol_version": 1, "read_only": True, "capabilities": sorted(grant.capabilities),
                "operations": [{"name": op.name, "scope_kind": op.scope_kind, "capability": op.capability} for op in self.operations.values() if op.capability in grant.capabilities],
                "configured": self.data_dir is not None, "data_available": bool(self.data_dir and self.data_dir.is_dir()),
                "audit_enabled": self.audit_path is not None,
                "consistency": "per-file request snapshot; not an atomic multi-collection transaction",
                "cancellation": "cooperative checks before/after synchronous reads; transport kill may leave receipt unknown"}

    def execute(self, request: Any, grant: ReadGrant, control: ReadControl | None = None) -> dict:
        identity = request if isinstance(request, dict) else {}
        response = {"protocol_version": 1, "request_id": identity.get("request_id"), "task_id": identity.get("task_id"),
                    "operation": identity.get("operation"), "scope": identity.get("scope"), "ok": False, "status": "error", "source_versions": []}
        receipt = {"request_id": response["request_id"], "task_id": response["task_id"], "operation": response["operation"],
                   "principal": grant.principal, "execution": "not_started", "read_only": True,
                   "execution_scope": "domain_handler",
                   "audit": {"persisted": False}, "consistency": "per-file snapshot"}
        response["receipt"] = receipt
        snapshot = None
        try:
            allowed = {"protocol_version", "request_id", "task_id", "operation", "scope", "params", "expected_versions", "timeout_ms"}
            if not isinstance(request, dict) or set(request) - allowed or type(request.get("protocol_version")) is not int or request["protocol_version"] != 1:
                raise ReadOperationError("invalid_argument", "Invalid v1 request; authorization is not a request field")
            for key in ("request_id", "task_id", "operation"):
                if not isinstance(request.get(key), str) or not request[key].strip() or len(request[key]) > 200:
                    raise ReadOperationError("invalid_argument", f"Required request identity: {key}")
            timeout = request.get("timeout_ms", 15000)
            if type(timeout) is not int or not 1 <= timeout <= 30000:
                raise ReadOperationError("invalid_argument", "timeout_ms must be 1..30000")
            control = control or ReadControl(timeout)
            control.check()
            operation = self.operations.get(request["operation"])
            if operation is None:
                raise ReadOperationError("unsupported", "Unsupported read operation")
            if operation.capability not in grant.capabilities:
                raise ReadOperationError("permission_denied", "Capability is disabled in the trusted grant")
            params = operation.validate(request.get("params"))
            scope = request.get("scope")
            if not isinstance(scope, dict) or set(scope) - {"kind", "stage_id"} or scope.get("kind") != operation.scope_kind:
                raise ReadOperationError("scope_conflict", "Scope does not match the requested operation")
            if operation.stage_field:
                stage = params[operation.stage_field]
                if scope.get("stage_id") != stage:
                    raise ReadOperationError("scope_conflict", "Scope and target stage differ")
                if stage not in grant.stage_ids:
                    raise ReadOperationError("permission_denied", "Target stage is not authorized by the host")
            elif "stage_id" in scope:
                raise ReadOperationError("scope_conflict", "Catalog reads do not imply project membership")
            if operation.scope_kind == "project_catalog" and not grant.project_catalog or operation.scope_kind == "paper_catalog" and not grant.paper_catalog:
                raise ReadOperationError("permission_denied", "Catalog read permission is not granted")
            if self.data_dir is None:
                raise ReadOperationError("not_configured", "Local data directory is not configured")
            snapshot = ReadSnapshot(self.data_dir, control, operation.resources)
            expected = request.get("expected_versions", {})
            if not isinstance(expected, dict):
                raise ReadOperationError("invalid_argument", "expected_versions must be an object")
            for resource, digest in expected.items():
                if resource not in operation.resources:
                    raise ReadOperationError("permission_denied", "Version precondition exceeds the operation's declared resources")
                if resource not in snapshot.entities or not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    raise ReadOperationError("invalid_argument", "Expected version must name a known entity file and SHA256")
                snapshot.entities[resource]._load()
                if snapshot.versions[resource]["sha256"] != digest:
                    raise ReadOperationError("version_conflict", "Source version changed; refresh evidence before retrying")
            control.check()
            receipt["execution"] = "started"
            try:
                data = (operation.context_handler(snapshot, params, control, grant)
                        if operation.context_handler else operation.handler(snapshot.store, params))
            finally:
                receipt["execution"] = "completed"
            control.check()
            response.update(ok=True, status="ok", data=data)
        except (ReadOperationError, DomainQueryError) as error:
            code = error.code
            response.update(status=code if code in {"cancelled", "timeout"} else "error",
                            error={"code": code, "message": str(error), "retryable": code in {"dependency_unavailable", "timeout"}})
        except PermissionError:
            response["error"] = {"code": "permission_denied", "message": "Local storage access denied", "retryable": False}
        except (json.JSONDecodeError, UnicodeError):
            response["error"] = {"code": "internal_error", "message": "Local storage is not valid UTF-8 JSON", "retryable": False}
        except Exception:
            response["error"] = {"code": "internal_error", "message": "Read operation failed; no business write was performed", "retryable": False}
        if snapshot:
            response["source_versions"] = list(snapshot.versions.values())
        receipt["outcome"] = response["status"]
        if self.audit_path:
            try:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                entry = {"protocol_version": 1, **receipt, "audit": {"persisted": True}, "error_code": response.get("error", {}).get("code"),
                         "source_versions": response["source_versions"]}
                with file_lock(self.audit_path):
                    with self.audit_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                receipt["audit"] = {"persisted": True}
            except Exception:
                response.pop("data", None)
                response.update(ok=False, status="error", error={"code": "audit_failed", "message": "Read receipt could not be persisted; see receipt.execution", "retryable": False})
                receipt["outcome"] = "error"
        return response
