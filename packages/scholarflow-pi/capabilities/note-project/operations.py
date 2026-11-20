"""Projection adapter for shared operator governance; paths are host-owned."""
from dataclasses import dataclass
import re

from scholarflow.governed_writes import GovernedWriteFacade, text
from scholarflow.mutations import record_fingerprint
from scholarflow.note_projection import NoteProjectionService, file_hash, render_note, safe_target
from scholarflow.vault_projection import VAULT_POLICIES, VaultProjectionService, validate_target
from scholarflow.governed_writes import digest
from scholarflow.read_operations import ReadOperationError
from scholarflow.source_notes import source_version_state, validate_source_snapshot


def projection_path(target):
    return validate_target(target) if target.get("policy") in VAULT_POLICIES else safe_target(target)


class ProjectionServices(NoteProjectionService):
    def __init__(self,store,*,clock=None,grant_guard=None):
        super().__init__(store,clock=clock)
        self.vault=VaultProjectionService(store,clock=clock,grant_guard=grant_guard)

    def get_proposal(self,pid):
        from scholarflow.proposal_state import load_proposal_state
        p=load_proposal_state(self.root)["proposals"].get(pid)
        return self.vault.get_proposal(pid) if p and p.get("operation")=="note.obsidian.project" else super().get_proposal(pid)

    def propose(self,target_id,target,**kwargs):
        return self.vault.propose(target_id,target,**kwargs) if target["policy"] in VAULT_POLICIES else super().propose(target_id,target,**kwargs)

    def apply(self,pid,**kwargs):
        p=self.get_proposal(pid)
        return self.vault.apply(pid,**kwargs) if p["operation"]=="note.obsidian.project" else super().apply(pid,**kwargs)


@dataclass(frozen=True)
class ProjectionGrant:
    principal: str
    targets: dict

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"principal", "targets"} or not isinstance(value["targets"], dict) or not 1 <= len(value["targets"]) <= 100:
            raise ReadOperationError("invalid_argument", "Exact projection targets required")
        principal = text(value["principal"], "principal", 160)
        targets = {}
        for key, target in value["targets"].items():
            text(key, "target_id")
            if not isinstance(target, dict):
                raise ReadOperationError("invalid_argument", "Object projection target required")
            for field, prefix in (("artifact_id", "artifact-"), ("stage_id", "stage-")):
                if not text(target.get(field), field).startswith(prefix):
                    raise ReadOperationError("invalid_argument", "Canonical source ID required")
            projection_path(target)
            targets[key] = dict(target)
        # Distinct grants cannot concurrently overwrite the same export path.
        paths = [str(projection_path(t)).casefold() for t in targets.values()]
        if len(paths) != len(set(paths)):
            raise ReadOperationError("invalid_argument", "Duplicate projection paths")
        return cls(principal, targets)


class ProjectionDomain:
    capability, prefix, resource, namespaced = "note-project", "note.project.", "research_artifacts.json", True

    def __init__(self,*,clock=None,grant_guard=None):
        self.clock,self.grant_guard=clock,grant_guard

    def validate_scope(self, scope):
        if not isinstance(scope, dict) or set(scope) != {"kind", "stage_id", "artifact_id", "target_id"} or scope["kind"] != "note_projection":
            raise ReadOperationError("scope_conflict", "Exact project/note/export scope required")
        for key in ("stage_id", "artifact_id", "target_id"):
            text(scope[key], key)

    def authorize_scope(self, grant, scope):
        target = grant.targets.get(scope["target_id"])
        if not target or any(target[k] != scope[k] for k in ("stage_id", "artifact_id")):
            raise ReadOperationError("permission_denied", "Exact source and export target not granted")
        projection_path(target)

    def authorize(self, grant, scope, mode):
        if mode != "update":
            raise ReadOperationError("permission_denied", "Projection requires an existing note; use update mode")
        self.authorize_scope(grant, scope)

    def validate_current(self, store, scope):
        self.current(store, scope)

    def current(self, store, scope):
        note = store.get_research_artifact(scope["artifact_id"])
        if not note or note.project_stage_id != scope["stage_id"] or note.kind.value != "synthesis" or not note.source_snapshot:
            raise ReadOperationError("scope_conflict", "Existing source note in the exact project required")
        validate_source_snapshot(note.source_snapshot)
        return note.to_dict()

    def read_context(self, store, grant, scope, mode):
        target = grant.targets[scope["target_id"]]
        path = projection_path(target)
        note = self.current(store, scope)
        if target["policy"] in VAULT_POLICIES:
            service=self.service(store).vault
            plan=service.plan(note,target)
            return {"target_id":scope["target_id"],"project_stage_id":scope["stage_id"],"artifact_id":scope["artifact_id"],
                "target_path":str(path),"expected_target_hash":"sha256:"+digest(plan),"rendered_preview":plan["files"][0]["after_text"],
                "projection_plan":plan,"side_effects":[s["path"] for s in plan["files"]],"implicit_files":[],
                "source_version_state":source_version_state(store,note["source_snapshot"]),"policy":target["policy"],"vault_sync":False,"ai_index_updated":False}
        return {"target_id": scope["target_id"], "project_stage_id": scope["stage_id"], "artifact_id": scope["artifact_id"],
            "target_path": str(path), "expected_target_hash": file_hash(path), "rendered_preview": render_note(note),
            "source_version_state": source_version_state(store, note["source_snapshot"]), "policy": target["policy"],
            "side_effects": [str(path)], "implicit_files": [], "ai_index_updated": False, "vault_sync": False}

    def service(self, store):
        return ProjectionServices(store,clock=self.clock,grant_guard=self.grant_guard)

    def prepare_changes(self, grant, scope, params):
        if set(params) != {"mode", "expected_fingerprint", "expected_target_hash"} or params["mode"] != "update":
            raise ReadOperationError("invalid_argument", "Exact source and target versions required; no model paths or text")
        if not isinstance(params["expected_target_hash"], str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", params["expected_target_hash"]):
            raise ReadOperationError("invalid_argument", "Exact target fingerprint required")
        return {"expected_target_hash": params["expected_target_hash"]}

    def propose(self, service, grant, scope, mode, changes, request_id):
        return service.propose(scope["target_id"], grant.targets[scope["target_id"]], request_id=request_id, **changes)

    def validate_proposal(self, store, grant, scope, proposal):
        target = {"target_id": scope["target_id"], **grant.targets[scope["target_id"]]}
        vault=target["policy"] in VAULT_POLICIES
        operation="note.obsidian.project" if vault else "note.markdown.project"
        if proposal.get("operation") != operation or proposal.get("target") != target or proposal.get("mode") != "update":
            raise ReadOperationError("scope_conflict", "Projection target or trusted grant changed")
        projection_path(grant.targets[scope["target_id"]])
        change = proposal["normalized_changes"]
        import hashlib
        if vault:
            files=change.get("files",[])
            if set(s["relative_path"] for s in files)!=set(target["files"]):
                raise ReadOperationError("version_conflict","Frozen side effects changed")
            for step in files:
                if step["after_hash"]!="sha256:"+hashlib.sha256(step["after_text"].encode("utf-8")).hexdigest():
                    raise ReadOperationError("version_conflict","Frozen vault bytes changed")
            return
        if set(change) != {"rendered_text", "rendered_hash", "target_before_hash"} or change["rendered_hash"] != "sha256:" + hashlib.sha256(change["rendered_text"].encode("utf-8")).hexdigest():
            raise ReadOperationError("version_conflict", "Frozen export bytes changed")

    def durable_receipt(self, store, proposal):
        return self.service(store).receipt(proposal)

    def execution_observation(self, store, proposal):
        from scholarflow.proposal_state import load_proposal_state
        step = load_proposal_state(store.root_dir).get("projection_steps", {}).get(proposal["proposal_id"])
        if proposal["operation"]=="note.obsidian.project":
            return {"external_file_write":"completed_historical" if step and step.get("phase")=="completed" else "partial_inspect_steps" if step else "not_attempted",
                "external_receipt_atomic_with_file":False,"projection_progress":step}
        return {"external_file_write": "completed_historical" if step and step["phase"] == "completed"
            else "may_have_completed_inspect_step" if step else "not_attempted",
            "external_receipt_atomic_with_file": False}

    def proposal_observation(self, store, scope, proposal):
        if proposal["operation"]=="note.obsidian.project":
            observe=self.service(store).vault.observation(proposal)
            observe["source_note_matches_current"]=record_fingerprint(self.current(store,scope))==proposal["base_fingerprint"]
            if observe["partial_execution"]: observe["phase"]="partial"
            return observe
        target = {k: v for k, v in proposal["target"].items() if k != "target_id"}
        path = safe_target(target)
        from scholarflow.proposal_state import load_proposal_state
        step = load_proposal_state(store.root_dir).get("projection_steps", {}).get(proposal["proposal_id"])
        return {"projection_step": step, "projection_target_matches_current": file_hash(path) == proposal["normalized_changes"]["rendered_hash"],
            "source_note_matches_current": record_fingerprint(self.current(store, scope)) == proposal["base_fingerprint"],
            "projection_target_before_matches_current": file_hash(path) == proposal["normalized_changes"]["target_before_hash"],
            "vault_sync": False, "ai_index_updated": False, "projection_receipt_kind": "single_file_step_receipt_not_entity_CAS"}

    def apply(self, service, proposal_id, **kwargs):
        return service.apply(proposal_id, **kwargs)


class ProjectionFacade(GovernedWriteFacade):
    def __init__(self, data_dir, *, clock=None, grant_guard=None):
        super().__init__(data_dir, ProjectionDomain(clock=clock,grant_guard=grant_guard), clock=clock)
