"""Exact project/task rules; common proposal governance owns the lifecycle."""
from dataclasses import dataclass
from datetime import datetime, timezone
from scholarflow.governed_writes import GovernedWriteFacade, text
from scholarflow.mutations import TaskMutationService
from scholarflow.read_operations import ReadOperationError

FIELDS = {"title", "description", "status", "priority", "due_hint"}


def now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TaskWriteGrant:
    principal: str
    create_tasks: tuple[tuple[str, str], ...]
    update_tasks: tuple[tuple[str, str], ...]

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"principal", "create_tasks", "update_tasks"}:
            raise ReadOperationError("invalid_argument", "Invalid trusted task-write grant")
        principal = text(value["principal"], "principal", 160)
        maps = []
        for key in ("create_tasks", "update_tasks"):
            mapping = value[key]
            if not isinstance(mapping, dict) or len(mapping) > 100:
                raise ReadOperationError("invalid_argument", f"Invalid {key}")
            maps.append(tuple((text(task, "task_id"), text(stage, "stage_id")) for task, stage in mapping.items()))
        create, update = map(dict, maps)
        if any(create[task] != update[task] for task in create.keys() & update.keys()):
            raise ReadOperationError("invalid_argument", "One task cannot be granted to different projects")
        return cls(principal, *maps)

    def authorize(self, scope, mode):
        mapping = dict(self.create_tasks if mode == "create" else self.update_tasks)
        if mapping.get(scope["task_id"]) != scope["stage_id"]:
            raise ReadOperationError("permission_denied", "Exact task/project/action is not authorized by the current host grant")


class TaskDomain:
    capability = "task-write"
    prefix = "task.write."
    resource = "tasks.json"
    namespaced = True

    def validate_scope(self, scope):
        if not isinstance(scope, dict) or set(scope) != {"kind", "stage_id", "task_id"} or scope["kind"] != "task":
            raise ReadOperationError("scope_conflict", "Exact task and project stage scope required")
        text(scope.get("stage_id"), "stage_id")
        text(scope.get("task_id"), "task_id")

    def authorize_scope(self, grant, scope):
        if not any(dict(items).get(scope["task_id"]) == scope["stage_id"] for items in (grant.create_tasks, grant.update_tasks)):
            raise ReadOperationError("permission_denied", "Exact task/project scope is not granted")

    def authorize(self, grant, scope, mode):
        grant.authorize(scope, mode)

    def validate_current(self, store, scope):
        if store.get_project_stage(scope["stage_id"]) is None:
            raise ReadOperationError("not_found", "Granted project stage does not exist")
        record = store.get_task(scope["task_id"])
        if record and record.project_stage_id != scope["stage_id"]:
            raise ReadOperationError("scope_conflict", "Task no longer belongs to the requested project; no implicit reassignment")

    def current(self, store, scope):
        record = store.get_task(scope["task_id"])
        if record and record.project_stage_id != scope["stage_id"]:
            raise ReadOperationError("scope_conflict", "Task belongs to a different project; no record exposed")
        return record.to_dict() if record else None

    def context(self, grant, scope, mode):
        return {"project_stage_id": scope["stage_id"], "domain_task_id": scope["task_id"]}

    def service(self, store):
        return TaskMutationService(store)

    def prepare_changes(self, grant, scope, params):
        if (set(params) != {"mode", "task_id", "changes", "expected_fingerprint"} or params["task_id"] != scope["task_id"]
                or not isinstance(params["mode"], str) or params["mode"] not in {"create", "update"}):
            raise ReadOperationError("invalid_argument", "Explicit mode/task/changes/base fingerprint required")
        self.authorize(grant, scope, params["mode"])
        changes = params["changes"]
        if not isinstance(changes, dict) or not changes or set(changes) - FIELDS:
            raise ReadOperationError("invalid_argument", "Only title/description/status/priority/due_hint can be changed")
        for field, value in changes.items():
            if value is None and field in {"description", "priority", "due_hint"}:
                continue
            text(value, field, 8000 if field == "description" else 1000)
        return {**changes, "project_stage_id": scope["stage_id"]} if params["mode"] == "create" else dict(changes)

    def propose(self, service, grant, scope, mode, changes, request_id):
        return (service.propose_create if mode == "create" else service.propose_update)(scope["task_id"], changes, request_id=request_id)

    def validate_proposal(self, store, grant, scope, proposal):
        if proposal.get("operation") not in {"task.create", "task.update"} or proposal.get("target") != {"task_id": scope["task_id"]}:
            raise ReadOperationError("scope_conflict", "Proposal does not match the requested task")
        create = proposal["operation"] == "task.create"
        self.authorize(grant, scope, "create" if create else "update")
        self.validate_current(store, scope)
        changes = proposal["normalized_changes"]
        allowed = FIELDS | ({"project_stage_id"} if create else set())
        if set(changes) - allowed or create and changes.get("project_stage_id") != scope["stage_id"]:
            raise ReadOperationError("scope_conflict", "Proposal exceeds the permitted task fields/project")

    def apply(self, service, proposal_id, **kwargs):
        return service.apply_update(proposal_id, **kwargs)


class TaskWriteFacade(GovernedWriteFacade):
    def __init__(self, data_dir):
        super().__init__(data_dir, TaskDomain(), clock=lambda: now())
