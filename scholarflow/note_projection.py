"""Frozen single-file exports. No model approval, vault policy bypass or AI index."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

from .governed_writes import digest
from .mutations import MutationError, record_fingerprint
from .persistence import atomic_write_text, file_lock
from .proposal_state import load_proposal_state, save_proposal_state

POLICY = "local-markdown-v1"
MAX_EXPORT_BYTES = 160000


def file_hash(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else record_fingerprint(None)


def safe_target(target):
    if set(target) != {"stage_id", "artifact_id", "root", "subdir", "path", "policy"} or target["policy"] != POLICY:
        raise MutationError("permission_denied", "Exact local Markdown target policy required")
    root = Path(target["root"])
    if not root.is_absolute() or not root.is_dir() or root.absolute() != root.resolve():
        raise MutationError("permission_denied", "Existing non-aliased absolute export root required")
    for ancestor in (root, *root.parents):
        if (ancestor / ".obsidian").exists():
            raise MutationError("policy_rejected", "Use a constitution-governed vault adapter, not local export")
    import os
    vault = os.environ.get("SCHOLARFLOW_VAULT_DIR")
    if vault and (root == Path(vault).resolve() or Path(vault).resolve() in root.parents):
        raise MutationError("policy_rejected", "Configured vault is not a local export target")
    def parts(value):
        if not isinstance(value, str) or not value or "\\" in value or ":" in value:
            raise MutationError("permission_denied", "Canonical relative path required")
        items = value.split("/")
        for p in items:
            if (not p or p in {".", ".."} or p.lower() == ".obsidian" or p.endswith((".", " "))
                    or any(ord(c) < 32 or c in '<>"|?*' for c in p)
                    or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p)):
                raise MutationError("permission_denied", "Unsafe export path component")
        return items
    subdir, relative = parts(target["subdir"]), parts(target["path"])
    if relative[:len(subdir)] != subdir or len(relative) <= len(subdir) or not relative[-1].lower().endswith(".md"):
        raise MutationError("permission_denied", "Exact Markdown path under granted subdirectory required")
    path = root.joinpath(*relative)
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise MutationError("permission_denied", "Export aliases are not allowed")
    if path.exists() and not path.is_file():
        raise MutationError("permission_denied", "Export target must be a regular file")
    if root not in path.resolve().parents:
        raise MutationError("permission_denied", "Export escaped its trusted root")
    return path


def render_note(record):
    # JSON strings are valid YAML quoted scalars, including quotes/newlines.
    quote = lambda v: json.dumps(v, ensure_ascii=False)
    metadata = {"title": record["title"], "type": "scholarflow-note-projection", "policy": POLICY,
        "artifact_id": record["artifact_id"], "project_stage_id": record["project_stage_id"],
        "source_note_fingerprint": record_fingerprint(record), "status": record["status"]}
    text = "---\n" + "\n".join(k + ": " + quote(v) for k, v in metadata.items()) + "\n---\n\n" + record["content"] + "\n"
    snapshot = record["source_snapshot"]
    if snapshot.get("version") == 2:
        text += "\n## Frozen PDF fragment provenance\n\nSource anchors are verified PDF excerpts; the note is derived, not an AI index.\n\n"
    else:
        text += "\n## Frozen stored-summary provenance\n\n"
        text += "This export is a human-note projection, not PDF verification or AI indexing.\n\n"
    text += "```json\n" + json.dumps(snapshot, ensure_ascii=False, indent=2).replace("```", "\\u0060\\u0060\\u0060") + "\n```\n"
    if len(text.encode("utf-8")) > MAX_EXPORT_BYTES:
        raise MutationError("resource_limit", "Export exceeds the operation byte budget; nothing truncated")
    return text


class NoteProjectionService:
    def __init__(self, store, *, clock=None):
        self.store, self.root = store, store.root_dir
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def get_proposal(self, proposal_id):
        p = load_proposal_state(self.root)["proposals"].get(proposal_id)
        if not p or p.get("operation") != "note.markdown.project":
            raise MutationError("not_found", "Projection proposal not found")
        return p

    def propose(self, target_id, target, *, expected_target_hash, request_id=None):
        path = safe_target(target)
        note = self.store.get_research_artifact(target["artifact_id"])
        if not note or note.project_stage_id != target["stage_id"] or note.kind.value != "synthesis" or not note.source_snapshot:
            raise MutationError("scope_conflict", "Existing project source note required")
        with file_lock(path.parent if path.parent.exists() else self.root / "projection-prepare"):
            before = file_hash(path)
            if before != expected_target_hash:
                raise MutationError("target_version_conflict", "Export target changed; reread context")
            record = note.to_dict()
            body = render_note(record)
            identity = {"operation": "note.markdown.project", "target": {"target_id": target_id, **target},
                "mode": "update", "base_fingerprint": record_fingerprint(record),
                "normalized_changes": {"rendered_text": body, "rendered_hash": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(), "target_before_hash": before}}
            pid = "sha256:" + digest(identity)
            state = load_proposal_state(self.root)
            if pid in state["proposals"]:
                return state["proposals"][pid]
            now = self.clock()
            proposal = {"proposal_version": 1, "proposal_id": pid, **identity,
                "created_at": now.isoformat(), "expires_at": (now + timedelta(hours=24)).isoformat(),
                "diff": {"files": [{"path": str(path), "before_hash": before, "after_hash": identity["normalized_changes"]["rendered_hash"]}], "implicit_files": []},
                "policy": {"requires_confirmation": True, "side_effect_class": "external_file_write", "version": POLICY}, "warnings": ["Not an Obsidian constitution write or AI indexing."]}
            state["proposals"][pid] = proposal
            save_proposal_state(self.root, state)
            return proposal

    def receipt(self, proposal):
        value = load_proposal_state(self.root).get("projection_steps", {}).get(proposal["proposal_id"])
        return value if value and value.get("phase") == "completed" else None

    def apply(self, proposal_id, *, confirmation, request_id=None):
        proposal = self.get_proposal(proposal_id)
        if (not confirmation or confirmation.get("proposal_id") != proposal_id or confirmation.get("confirmed") is not True
                or confirmation.get("confirmed_by") != "interactive_user"):
            raise MutationError("confirmation_required", "Separate operator review is required")
        target = {k: v for k, v in proposal["target"].items() if k != "target_id"}
        path = safe_target(target)
        # Same ordering as source-note writes: source entity lock before target.
        with ExitStack() as stack:
            stack.enter_context(file_lock(self.root / "research_artifacts.json"))
            path.parent.mkdir(parents=True, exist_ok=True)
            safe_target(target)
            stack.enter_context(file_lock(path))
            state = load_proposal_state(self.root)
            previous = state.get("projection_steps", {}).get(proposal_id)
            if previous and previous["phase"] == "completed":
                return previous  # Historical receipt; never overwrite manual changes.
            if self.clock() >= datetime.fromisoformat(proposal["expires_at"]):
                raise MutationError("expired_proposal", "Export approval expired")
            note = self.store.get_research_artifact(target["artifact_id"])
            if record_fingerprint(note.to_dict() if note else None) != proposal["base_fingerprint"]:
                raise MutationError("version_conflict", "Source note changed; export was not regenerated")
            change = proposal["normalized_changes"]
            current = file_hash(path)
            after = change["rendered_hash"]
            if current != change["target_before_hash"] and not (previous and current == after):
                raise MutationError("target_version_conflict", "Manual target changes are preserved")
            step = previous or {"proposal_id": proposal_id, "operation": proposal["operation"], "phase": "intent",
                "target_path": str(path), "before_hash": current, "after_hash": after,
                "source_fingerprint": proposal["base_fingerprint"], "request_id": request_id,
                "observation": "single-file projection; no multi-file atomicity or exclusive-writer proof"}
            state.setdefault("projection_steps", {})[proposal_id] = step
            save_proposal_state(self.root, state)
            recovered = bool(previous and current == after)
            if not recovered:
                safe_target(target)
                if file_hash(path) != change["target_before_hash"]:
                    raise MutationError("target_version_conflict", "Target changed immediately before write")
                atomic_write_text(path, change["rendered_text"])
            if file_hash(path) != after:
                raise MutationError("target_version_conflict", "Target changed after export; inspect intent")
            step = {**step, "phase": "completed", "completed_at": self.clock().isoformat(),
                "recovery_observed_expected_bytes": recovered}
            state = load_proposal_state(self.root)
            state.setdefault("projection_steps", {})[proposal_id] = step
            state["commits"][proposal_id] = {"response": step, "committed_at": step["completed_at"]}
            save_proposal_state(self.root, state)
            return step
