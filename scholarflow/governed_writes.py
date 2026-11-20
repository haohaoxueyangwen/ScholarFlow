"""Domain-neutral harness governance. Adapters supply facts and domain rules.

No model loop, domain switch, second database or operator HTTP authority.
The fact+receipt CAS is atomic within one entity file, not across projections.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from .mutations import MutationError, record_fingerprint
from .persistence import file_lock
from .proposal_state import load_proposal_state, save_proposal_state, reject_proposal
from .read_operations import ReadControl, ReadOperationError
from .storage import ScholarFlowStore


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def text(value, name, maximum=200):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise ReadOperationError("invalid_argument", f"Invalid {name}")
    return value


class GovernedWriteFacade:
    """Small adapter protocol; no central registry of business workflows."""
    def __init__(self, data_dir, domain, *, clock=None):
        self.root = Path(data_dir).resolve() if data_dir else None
        self.domain = domain
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def describe(self, grant):
        return {"protocol_version": 1, "capability": self.domain.capability, "configured": self.root is not None,
                "data_available": bool(self.root and self.root.is_dir()),
                "agent_operations": [self.domain.prefix + action for action in ("context", "propose", "status")],
                "operator_operations": [self.domain.prefix + "decide"], "confirmation": "operator CLI only; reviewed proposal hash required",
                "domain_commit_exposed_to_model": False, "principal": grant.principal}

    def _versions(self):
        result = []
        for resource in (self.domain.resource, "pi_mutations.json"):
            path = self.root / resource
            if path.exists():
                raw = path.read_bytes()
                payload = json.loads(raw)
                result.append({"resource": resource, "sha256": hashlib.sha256(raw).hexdigest(), "revision": payload.get("revision"),
                               "observation": "after operation; not a multi-file transaction"})
        return result

    def _proposal(self, store, grant, scope, proposal_id, state):
        proposal = self.domain.service(store).get_proposal(text(proposal_id, "proposal_id"))
        self.domain.validate_proposal(store, grant, scope, proposal)
        binding = state.get("harness_bindings", {}).get(proposal_id)
        if not binding or binding["principal"] != grant.principal:
            raise ReadOperationError("permission_denied", "Proposal is not owned by this harness principal")
        if binding["proposal_hash"] != digest(proposal):
            raise ReadOperationError("version_conflict", "Proposal contents changed; approval is invalid")
        if self.domain.namespaced and (binding.get("capability") != self.domain.capability or binding.get("scope") != scope):
            raise ReadOperationError("scope_conflict", "Proposal binding does not match the capability/target scope")
        return proposal, binding

    def _view(self, store, scope, proposal, binding, state):
        pid = proposal["proposal_id"]
        receipt = self._durable_receipt(store, proposal)
        decision = state.get("harness_decisions", {}).get(pid)
        stale = record_fingerprint(self.domain.current(store, scope)) != proposal["base_fingerprint"]
        expired = self.clock() >= datetime.fromisoformat(proposal["expires_at"])
        phase = "committed" if receipt else "rejected" if pid in state["rejections"] else "expired" if expired else "stale" if stale else "approved_pending" if decision else "pending"
        view = {"phase": phase, "proposal": proposal, "proposal_hash": binding["proposal_hash"], "proposal_task_id": binding["task_id"],
                "domain_receipt": receipt, "projection_pending": bool(receipt and pid not in state["commits"]), "decision": decision,
                "expires_at_has_passed": expired, "expiry_blocks_commit": bool(expired and not receipt), "edit_history_coverage": "not_provided"}
        observe = getattr(self.domain, "proposal_observation", None)
        if observe:
            view.update(observe(store, scope, proposal))
        return view

    def _durable_receipt(self, store, proposal):
        # External projections provide step receipts, not entity CAS receipts.
        lookup = getattr(self.domain, "durable_receipt", None)
        return lookup(store, proposal) if lookup else store.domain_proposal_receipt(proposal)

    def execute(self, request, grant, control=None, *, operator=False):
        identity = request if isinstance(request, dict) else {}
        response = {"protocol_version": 1, **{key: identity.get(key) for key in ("request_id", "task_id", "operation", "scope")},
                    "ok": False, "status": "error", "source_versions": []}
        receipt = {"request_id": response["request_id"], "task_id": response["task_id"], "principal": grant.principal,
                   "execution": "not_started", "domain_write": "not_attempted", "control_state_may_change": False}
        response["receipt"] = receipt
        proposal, store = None, None
        try:
            allowed = {"protocol_version", "request_id", "task_id", "operation", "scope", "params", "idempotency_key", "timeout_ms"}
            if not isinstance(request, dict) or set(request) - allowed or type(request.get("protocol_version")) is not int or request["protocol_version"] != 1:
                raise ReadOperationError("invalid_argument", "Invalid v1 request; approval/grant are not model request fields")
            for key in ("request_id", "task_id", "operation"):
                text(request.get(key), key)
            op, params, scope = request["operation"], request.get("params"), request.get("scope")
            actions = {self.domain.prefix + action: action for action in ("context", "propose", "status", "decide")}
            if op not in actions:
                raise ReadOperationError("unsupported", "Unsupported capability-write operation")
            action = actions[op]
            if action == "decide" and not operator:
                raise ReadOperationError("permission_denied", "Only the trusted operator CLI may approve/reject")
            self.domain.validate_scope(scope)
            if not isinstance(params, dict):
                raise ReadOperationError("invalid_argument", "Object params required")
            timeout = request.get("timeout_ms", 15000)
            if type(timeout) is not int or not 1 <= timeout <= 30000:
                raise ReadOperationError("invalid_argument", "timeout_ms must be 1..30000")
            control = control or ReadControl(timeout)
            control.check()
            if self.root is None:
                raise ReadOperationError("not_configured", "Local data directory required")
            if not self.root.is_dir():
                raise ReadOperationError("dependency_unavailable", "An existing data directory is required")
            # Exact host authority before locks or persisted proposal reads.
            self.domain.authorize_scope(grant, scope)
            with file_lock(self.root / ("harness-" + self.domain.capability), timeout_seconds=min(timeout / 1000, 10)):
                control.check()
                store = ScholarFlowStore(self.root)
                service = self.domain.service(store)
                self.domain.validate_current(store, scope)
                state = load_proposal_state(self.root)
                receipt.update(execution="started", control_state_may_change=action in {"propose", "decide"})
                if action == "context":
                    if set(params) != {"mode"} or not isinstance(params["mode"], str) or params["mode"] not in {"create", "update"}:
                        raise ReadOperationError("invalid_argument", "Context requires explicit create/update mode")
                    self.domain.authorize(grant, scope, params["mode"])
                    current = self.domain.current(store, scope)
                    data = {"phase": "context", "record": current, "expected_fingerprint": record_fingerprint(current), "domain_receipt": None,
                            "existing": current is not None, "mode": params["mode"], "edit_history_coverage": "current_record_only"}
                    read_context = getattr(self.domain, "read_context", None)
                    data.update(read_context(store, grant, scope, params["mode"]) if read_context
                                else self.domain.context(grant, scope, params["mode"]))
                elif action == "propose":
                    changes = self.domain.prepare_changes(grant, scope, params)
                    key = text(request.get("idempotency_key"), "idempotency_key", 160)
                    namespace = {"principal": grant.principal, "key": key}
                    if self.domain.namespaced:
                        namespace.update(capability=self.domain.capability, operation=op)
                    key_id = digest(namespace)
                    payload_hash = digest({"operation": op, "scope": scope, "params": params})
                    previous = state.get("harness_requests", {}).get(key_id)
                    if previous:
                        if previous["payload_hash"] != payload_hash:
                            raise ReadOperationError("idempotency_conflict", "Idempotency key is bound to a different payload")
                        proposal, binding = self._proposal(store, grant, scope, previous["proposal_id"], state)
                    else:
                        expected = text(params["expected_fingerprint"], "expected_fingerprint", 80)
                        if expected != record_fingerprint(self.domain.current(store, scope)):
                            raise ReadOperationError("version_conflict", "Target changed; read the current record before proposing")
                        proposal = self.domain.propose(service, grant, scope, params["mode"], changes, request["request_id"])
                        if proposal["base_fingerprint"] != expected:
                            raise ReadOperationError("version_conflict", "Target changed during proposal preparation; no approved commit")
                        existing = state.get("harness_bindings", {}).get(proposal["proposal_id"])
                        if existing and existing["principal"] != grant.principal:
                            raise ReadOperationError("permission_denied", "Identical proposal belongs to another principal")
                        if existing and self.domain.namespaced and (existing.get("capability") != self.domain.capability or existing.get("scope") != scope):
                            raise ReadOperationError("scope_conflict", "Identical proposal belongs to another capability/scope")
                        binding = existing or {"principal": grant.principal, "task_id": request["task_id"], "proposal_hash": digest(proposal)}
                        if self.domain.namespaced:
                            binding.update(capability=self.domain.capability, scope=dict(scope))
                        state = load_proposal_state(self.root)
                        state.setdefault("harness_bindings", {})[proposal["proposal_id"]] = binding
                        state.setdefault("harness_requests", {})[key_id] = {"payload_hash": payload_hash, "proposal_id": proposal["proposal_id"]}
                        save_proposal_state(self.root, state)
                    data = self._view(store, scope, proposal, binding, load_proposal_state(self.root))
                    data["idempotent_replay"] = previous is not None
                else:
                    expected_keys = {"proposal_id"} if action == "status" else {"proposal_id", "decision", "reviewed_hash"}
                    if set(params) != expected_keys:
                        raise ReadOperationError("invalid_argument", "Unsupported status/decision params")
                    proposal, binding = self._proposal(store, grant, scope, params["proposal_id"], state)
                    if action == "decide":
                        if params["decision"] not in {"approve", "reject"} or params["reviewed_hash"] != binding["proposal_hash"]:
                            raise ReadOperationError("confirmation_required", "Operator must review this exact proposal hash")
                        pid = proposal["proposal_id"]
                        current = self._view(store, scope, proposal, binding, state)
                        existing = state.get("harness_decisions", {}).get(pid)
                        if existing and existing["decision"] != params["decision"]:
                            raise ReadOperationError("decision_conflict", "A terminal operator decision already exists")
                        control.check()
                        if params["decision"] == "reject":
                            if current["phase"] == "committed":
                                raise ReadOperationError("decision_conflict", "A committed receipt cannot be rejected or rolled back")
                            reject_proposal(self.root, pid, rejected_by="interactive_user", rejected_at=self.clock().isoformat(),
                                            reason="Operator rejected reviewed proposal", request_id=request["request_id"])
                        else:
                            if current["phase"] == "rejected":
                                raise ReadOperationError("proposal_rejected", "User rejected this proposal")
                            if current["phase"] == "expired":
                                raise ReadOperationError("expired_proposal", "Proposal approval has expired")
                            if current["phase"] == "stale":
                                raise ReadOperationError("version_conflict", "Target changed after proposal creation")
                        decision = existing or {"decision": params["decision"], "principal": grant.principal, "channel": "operator_cli",
                            "proposal_hash": binding["proposal_hash"], "target": proposal["target"], "base_fingerprint": proposal["base_fingerprint"],
                            "expires_at": proposal["expires_at"], "decided_at": self.clock().isoformat(), "request_id": request["request_id"]}
                        state = load_proposal_state(self.root)
                        state.setdefault("harness_decisions", {})[pid] = decision
                        save_proposal_state(self.root, state)
                        if params["decision"] == "approve":
                            control.check()
                            self.domain.apply(service, pid, confirmation={"proposal_id": pid, "confirmed": True, "confirmed_by": "interactive_user",
                                "confirmed_at": decision["decided_at"], "pi_tool_call_id": request["request_id"]}, request_id=request["request_id"])
                    data = self._view(store, scope, proposal, binding, load_proposal_state(self.root))
                receipt["execution"] = "completed"
                receipt["domain_write"] = "committed" if data["domain_receipt"] else "not_committed"
                receipt["late_completion"] = control.cancelled.is_set() or (time.monotonic() - control.started) * 1000 >= control.timeout_ms
                response.update(ok=True, status=data["phase"], data=data)
        except TimeoutError:
            response.update(status="timeout", error={"code": "timeout", "message": "Persistence lock deadline elapsed; inspect durable receipt", "retryable": True})
        except (MutationError, ReadOperationError) as error:
            response["error"] = {"code": error.code, "message": str(error), "retryable": error.code in {"timeout", "dependency_unavailable"}}
            if error.code in {"cancelled", "timeout"}:
                response["status"] = error.code
        except Exception:
            response["error"] = {"code": "internal_error", "message": "Capability-write operation failed; inspect durable receipt before retrying", "retryable": False}
        if receipt["execution"] == "started":
            receipt["execution"] = "completed"
        if proposal is not None and store is not None:
            try:
                durable = self._durable_receipt(store, proposal)
                receipt["domain_write"] = "committed" if durable else "not_committed"
                if durable and not response["ok"]:
                    receipt["durable_domain_receipt"] = durable
            except Exception:
                receipt["domain_write"] = "unknown"
            observe_execution = getattr(self.domain, "execution_observation", None)
            if observe_execution:
                try:
                    receipt.update(observe_execution(store, proposal))
                except Exception:
                    receipt["external_file_write"] = "unknown"
        if store is not None:
            try:
                response["source_versions"] = self._versions()
            except Exception:
                response["source_versions"] = []
                receipt["version_observation"] = "unavailable"
        return response
