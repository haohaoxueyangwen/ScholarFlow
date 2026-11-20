"""Project rules over shared harness governance; legacy grants/keys remain valid."""
from dataclasses import dataclass
from datetime import datetime, timezone
from scholarflow.governed_writes import GovernedWriteFacade, digest, text
from scholarflow.read_operations import ReadOperationError
from scholarflow.research_mutations import ResearchMutationService

OPS = {"project.write.context", "project.write.propose", "project.write.status", "project.write.decide"}
UPDATE_FIELDS = {"progress_note", "status", "next_actions", "risks"}


def now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class WriteGrant:
    principal: str
    create_stage_ids: frozenset[str]
    update_stage_ids: frozenset[str]
    paper_ids: frozenset[str]
    goal_requests: tuple[tuple[str, str, str], ...] = ()

    @classmethod
    def from_dict(cls, value):
        required = {"principal", "create_stage_ids", "update_stage_ids", "paper_ids"}
        if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"goal_requests"}:
            raise ReadOperationError("invalid_argument", "Invalid trusted project-write grant")
        principal = text(value["principal"], "principal", 160)
        entries = []
        for key in ("create_stage_ids", "update_stage_ids", "paper_ids"):
            items = value[key]
            if not isinstance(items, list) or len(items) > 100:
                raise ReadOperationError("invalid_argument", f"Invalid {key}")
            entries.append(frozenset(text(item, key) for item in items))
        goals = value.get("goal_requests", {})
        if not isinstance(goals, dict) or len(goals) > 100:
            raise ReadOperationError("invalid_argument", "Invalid trusted goal requests")
        origins = []
        for stage, item in goals.items():
            if stage not in entries[0] or not isinstance(item, dict) or set(item) != {"request_id", "goal"}:
                raise ReadOperationError("invalid_argument", "Goal request must match an authorized create target")
            origins.append((stage, text(item["request_id"], "goal request_id"), text(item["goal"], "trusted goal", 4000)))
        return cls(principal, *entries, tuple(origins))

    def goal_request(self, stage):
        return next(({"request_id": request_id, "goal": goal} for target, request_id, goal in self.goal_requests if target == stage), None)

    def authorize(self, stage, mode):
        if stage not in (self.create_stage_ids if mode == "create" else self.update_stage_ids):
            raise ReadOperationError("permission_denied", "Target/action not authorized by the current host grant")


class ProjectDomain:
    capability = "project-write"
    prefix = "project.write."
    resource = "project_stages.json"
    namespaced = False  # Preserve pre-H006b project idempotency keys/bindings.

    def validate_scope(self, scope):
        if not isinstance(scope, dict) or set(scope) != {"kind", "stage_id"} or scope["kind"] != "project_stage":
            raise ReadOperationError("scope_conflict", "Exact project_stage scope required")
        text(scope.get("stage_id"), "stage_id")

    def authorize_scope(self, grant, scope):
        if scope["stage_id"] not in grant.create_stage_ids | grant.update_stage_ids:
            raise ReadOperationError("permission_denied", "Project scope is not granted")

    def authorize(self, grant, scope, mode):
        grant.authorize(scope["stage_id"], mode)

    def validate_current(self, store, scope):
        pass

    def current(self, store, scope):
        record = store.get_project_stage(scope["stage_id"])
        return record.to_dict() if record else None

    def service(self, store):
        return ResearchMutationService(store)

    def context(self, grant, scope, mode):
        if mode != "create":
            return {}
        goal = grant.goal_request(scope["stage_id"])
        return {"creation_basis": "user_goal", "authorized_goal": goal["goal"]} if goal else {"creation_basis": "paper_evidence"}

    def prepare_changes(self, grant, scope, params):
        if (set(params) != {"mode", "stage_id", "changes", "expected_fingerprint"} or params["stage_id"] != scope["stage_id"]
                or not isinstance(params["mode"], str) or params["mode"] not in {"create", "update"}):
            raise ReadOperationError("invalid_argument", "Explicit mode/target/changes/base fingerprint required")
        self.authorize(grant, scope, params["mode"])
        changes = params["changes"]
        if not isinstance(changes, dict) or params["mode"] == "update" and set(changes) - UPDATE_FIELDS:
            raise ReadOperationError("invalid_argument", "Only bounded progress fields can be updated")
        for ref in changes.get("evidence_refs", []):
            if not isinstance(ref, str) or not ref.startswith("paper:") or ref[6:] not in grant.paper_ids:
                raise ReadOperationError("permission_denied", "Paper linkage is not authorized")
        return changes

    def propose(self, service, grant, scope, mode, changes, request_id):
        stage = scope["stage_id"]
        goal = grant.goal_request(stage) if mode == "create" else None
        if goal:
            return service.propose_project_from_goal(stage, changes, trusted_goal=goal["goal"],
                goal_request_id=goal["request_id"], request_id=request_id)
        return (service.propose_project_create if mode == "create" else service.propose_project_update)(stage, changes, request_id=request_id)

    def validate_proposal(self, store, grant, scope, proposal):
        if proposal.get("operation") not in {"project_stage.create", "project_stage.update"} or proposal.get("target") != {"stage_id": scope["stage_id"]}:
            raise ReadOperationError("scope_conflict", "Proposal does not match the requested project/action")
        self.authorize(grant, scope, proposal["mode"])
        origin = proposal["normalized_changes"].get("creation_origin")
        if origin is not None:
            goal = grant.goal_request(scope["stage_id"])
            if (not goal or origin != {"kind": "user_goal", "request_id": goal["request_id"]}
                    or proposal["normalized_changes"].get("goal") != goal["goal"]):
                raise ReadOperationError("permission_denied", "The current host goal authorization changed or was revoked")
        for ref in proposal["normalized_changes"].get("evidence_refs", []):
            if not isinstance(ref, str) or not ref.startswith("paper:") or ref[6:] not in grant.paper_ids:
                raise ReadOperationError("permission_denied", "Paper linkage exceeds the current grant")

    def apply(self, service, proposal_id, **kwargs):
        return service.apply(proposal_id, **kwargs)


class ProjectWriteFacade(GovernedWriteFacade):
    def __init__(self, data_dir):
        super().__init__(data_dir, ProjectDomain(), clock=lambda: now())
