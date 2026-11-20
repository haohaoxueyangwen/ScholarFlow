"""Exact human-note/source rules; shared governance owns approval lifecycle."""
from dataclasses import dataclass
from datetime import datetime, timezone
import re

from scholarflow.governed_writes import GovernedWriteFacade, text
from scholarflow.read_operations import ReadOperationError
from scholarflow.source_notes import (MAX_SOURCES, SourceNoteMutationService, read_sources,
    source_locks, source_version_state, validate_source_snapshot)

FIELDS = {"title", "content", "status"}


def validate_note_changes(changes):
    """Normalize normal Markdown boundary whitespace before preview/hash."""
    if not isinstance(changes, dict) or not changes or set(changes) - FIELDS:
        raise ReadOperationError("invalid_argument", "Only title/content/status may be changed")
    normalized = {}
    for field, value in changes.items():
        if field == "content":
            if not isinstance(value,str) or len(value) > 12000:
                raise ReadOperationError("invalid_argument", "Invalid content")
            normalized[field] = text(value.strip(),field,12000)
        else:
            normalized[field] = text(value,field,1000)
    return normalized


def now():
    return datetime.now(timezone.utc)


def identifier(value, kind):
    value = text(value, kind + "_id")
    prefix = {"note": "artifact-", "stage": "stage-", "paper": "paper-"}[kind]
    if not value.startswith(prefix) or len(value) == len(prefix):
        raise ReadOperationError("invalid_argument", "Canonical " + kind + " ID required")
    return value


@dataclass(frozen=True)
class NoteWriteGrant:
    principal: str
    create_notes: tuple
    update_notes: tuple
    source_papers: tuple

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"principal", "create_notes", "update_notes", "source_papers"}:
            raise ReadOperationError("invalid_argument", "Invalid trusted note-write grant")
        principal = text(value["principal"], "principal", 160)
        mappings = []
        for key in ("create_notes", "update_notes"):
            mapping = value[key]
            if not isinstance(mapping, dict) or len(mapping) > 100:
                raise ReadOperationError("invalid_argument", "Invalid note action grant")
            mappings.append(tuple((identifier(note, "note"), identifier(stage, "stage")) for note, stage in mapping.items()))
        create, update = map(dict, mappings)
        if any(create[note] != update[note] for note in create.keys() & update.keys()):
            raise ReadOperationError("invalid_argument", "One note cannot be granted to different projects")
        sources = value["source_papers"]
        if not isinstance(sources, dict) or len(sources) > 100:
            raise ReadOperationError("invalid_argument", "Invalid source grant")
        normalized = []
        for stage, papers in sources.items():
            if (not isinstance(papers, list) or not 1 <= len(papers) <= MAX_SOURCES
                    or not all(isinstance(p, str) for p in papers) or len(set(papers)) != len(papers)):
                raise ReadOperationError("invalid_argument", "Distinct bounded source IDs required")
            normalized.append((identifier(stage, "stage"), tuple(identifier(p, "paper") for p in papers)))
        return cls(principal, *mappings, tuple(normalized))

    def authorize(self, scope, mode):
        mapping = dict(self.create_notes if mode == "create" else self.update_notes)
        if mapping.get(scope["artifact_id"]) != scope["stage_id"]:
            raise ReadOperationError("permission_denied", "Exact note/project/action is not granted")

    def authorize_sources(self, scope, papers):
        allowed = dict(self.source_papers).get(scope["stage_id"], ())
        if not papers or not set(papers) <= set(allowed):
            raise ReadOperationError("permission_denied", "Exact note project sources are not granted")


class NoteDomain:
    capability = "note-write"
    prefix = "note.write."
    resource = "research_artifacts.json"
    namespaced = True

    def validate_scope(self, scope):
        if not isinstance(scope, dict) or set(scope) != {"kind", "stage_id", "artifact_id"} or scope["kind"] != "note":
            raise ReadOperationError("scope_conflict", "Exact note/project scope required")
        identifier(scope["stage_id"], "stage")
        identifier(scope["artifact_id"], "note")

    def authorize_scope(self, grant, scope):
        if not any(dict(items).get(scope["artifact_id"]) == scope["stage_id"] for items in (grant.create_notes, grant.update_notes)):
            raise ReadOperationError("permission_denied", "Exact note/project is not granted")

    def authorize(self, grant, scope, mode):
        grant.authorize(scope, mode)

    def validate_current(self, store, scope):
        if store.get_project_stage(scope["stage_id"]) is None:
            raise ReadOperationError("not_found", "Granted project does not exist")
        self.current(store, scope)

    def current(self, store, scope):
        record = store.get_research_artifact(scope["artifact_id"])
        if record and (record.project_stage_id != scope["stage_id"] or record.kind.value != "synthesis"):
            raise ReadOperationError("scope_conflict", "Not a synthesis note in this project; no implicit reassignment")
        return record.to_dict() if record else None

    def read_context(self, store, grant, scope, mode):
        current = self.current(store, scope)
        if current and current.get("source_snapshot"):
            grant.authorize_sources(scope, [s["paper_id"] for s in current["source_snapshot"]["sources"]])
        papers = dict(grant.source_papers).get(scope["stage_id"], ())
        grant.authorize_sources(scope, papers)
        with source_locks(store):
            snapshot = read_sources(store, scope["stage_id"], papers)
        return {"project_stage_id": scope["stage_id"], "artifact_id": scope["artifact_id"], "sources": snapshot["sources"],
            "project_fingerprint": snapshot["stage_fingerprint"], "source_kind": "stored_summary_not_pdf_full_text",
            "current_note_source_state": source_version_state(store, current["source_snapshot"]) if current and current.get("source_snapshot") else None}

    def service(self, store):
        return SourceNoteMutationService(store)

    def prepare_changes(self, grant, scope, params):
        if (set(params) != {"mode", "artifact_id", "changes", "source_versions", "expected_fingerprint"}
                or params["artifact_id"] != scope["artifact_id"] or not isinstance(params["mode"], str)
                or params["mode"] not in {"create", "update"}):
            raise ReadOperationError("invalid_argument", "Explicit note/mode/changes/source versions/base required")
        self.authorize(grant, scope, params["mode"])
        changes = validate_note_changes(params["changes"])
        if "status" in changes and changes["status"] not in {"draft", "reviewed", "final", "archived"}:
            raise ReadOperationError("invalid_argument", "Invalid note status")
        versions = params["source_versions"]
        if not isinstance(versions, dict) or not 1 <= len(versions) <= MAX_SOURCES:
            raise ReadOperationError("invalid_argument", "At least one bounded source version is required")
        for paper, version in versions.items():
            identifier(paper, "paper")
            if not isinstance(version, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", version):
                raise ReadOperationError("invalid_argument", "Exact source fingerprint required")
        grant.authorize_sources(scope, versions)
        return {"changes": dict(changes), "source_versions": dict(versions)}

    def propose(self, service, grant, scope, mode, changes, request_id):
        return service.propose_note(scope["artifact_id"], stage_id=scope["stage_id"], mode=mode, request_id=request_id, **changes)

    def validate_proposal(self, store, grant, scope, proposal):
        if (proposal.get("operation") != "research_artifact.publish" or proposal.get("target") != {"artifact_id": scope["artifact_id"]}
                or proposal.get("mode") not in {"create", "update"}):
            raise ReadOperationError("scope_conflict", "Proposal does not match this note")
        self.authorize(grant, scope, proposal["mode"])
        self.validate_current(store, scope)
        changes = proposal["normalized_changes"]
        snapshot = changes.get("source_snapshot")
        validate_source_snapshot(snapshot)
        create = proposal["mode"] == "create"
        if (set(changes) - (FIELDS | {"source_snapshot", "related_papers", "evidence_refs"} | ({"kind", "project_stage_id"} if create else set()))
                or snapshot["stage_id"] != scope["stage_id"]
                or create and (changes.get("kind") != "synthesis" or changes.get("project_stage_id") != scope["stage_id"])):
            raise ReadOperationError("scope_conflict", "Proposal exceeds note/project fields")
        papers = [s["paper_id"] for s in snapshot["sources"]]
        grant.authorize_sources(scope, papers)
        if changes.get("related_papers") != papers or changes.get("evidence_refs") != ["paper:" + p for p in papers]:
            raise ReadOperationError("scope_conflict", "Proposal source references do not match its frozen snapshot")

    def proposal_observation(self, store, scope, proposal):
        return {"source_version_state": source_version_state(store, proposal["normalized_changes"]["source_snapshot"])}

    def apply(self, service, proposal_id, **kwargs):
        return service.apply(proposal_id, **kwargs)


class NoteWriteFacade(GovernedWriteFacade):
    def __init__(self, data_dir):
        super().__init__(data_dir, NoteDomain(), clock=lambda: now())
