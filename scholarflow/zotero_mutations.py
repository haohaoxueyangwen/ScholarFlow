"""Governed Zotero external mutations for the Pi-native runtime.

The service deliberately supports one narrow write primitive first: adding
tags to an existing Zotero item. A proposal captures the exact item version
and complete tag list read from Zotero. Apply re-reads that version, sends the
full merged tag list with optimistic concurrency, and records an idempotent
commit receipt. This keeps external writes reviewable and prevents a stale
proposal from overwriting a newer Zotero edit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .event_bus import append_event
from .mutations import MutationError, record_fingerprint, required_text
from .proposal_state import (
    load_proposal_state,
    raise_if_rejected,
    save_proposal_state,
    valid_confirmation,
)
from .zotero_client import ZoteroClient, ZoteroClientResult

PROPOSAL_VERSION = 1
PROPOSAL_TTL = timedelta(hours=24)
OPERATION = "zotero.item.tags.update"


class ZoteroMutationService:
    """Proposal/apply boundary for explicit Zotero tag synchronization."""

    def __init__(self, root_dir: Path, *, client: ZoteroClient) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.client = client

    def propose_add_tags(
        self,
        item_key: str,
        tags: list[str],
        *,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_key = required_text(item_key, "item_key")
        requested_tags = _normalize_tag_names(tags)
        if not requested_tags:
            raise MutationError("invalid_argument", "at least one non-empty tag is required")

        snapshot = self._read_snapshot(normalized_key)
        existing_names = {item["tag"] for item in snapshot["tags"]}
        added_tags = [tag for tag in requested_tags if tag not in existing_names]
        if not added_tags:
            raise MutationError("no_changes", "all requested Zotero tags are already present")
        final_tags = [*snapshot["tags"], *({"tag": tag} for tag in added_tags)]

        identity = {
            "operation": OPERATION,
            "target": {"item_key": normalized_key},
            "normalized_changes": {"tags": final_tags, "added_tags": added_tags},
            "base_snapshot": snapshot,
            "base_fingerprint": record_fingerprint(snapshot),
        }
        proposal_id = "sha256:" + hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
        state = load_proposal_state(self.root_dir)
        existing = state["proposals"].get(proposal_id)
        if existing is not None:
            return dict(existing)

        for candidate_id, candidate in state["proposals"].items():
            if candidate_id in state["commits"] or candidate_id in state["rejections"]:
                continue
            if candidate.get("operation") == OPERATION and candidate.get("target", {}).get("item_key") == normalized_key:
                raise MutationError(
                    "target_reserved",
                    f"Zotero item '{normalized_key}' already has an uncommitted tag proposal",
                    details={"item_key": normalized_key, "proposal_id": candidate_id},
                )

        effective_now = _coerce_now(now)
        proposal = {
            "proposal_version": PROPOSAL_VERSION,
            "proposal_id": proposal_id,
            "operation": OPERATION,
            "target": {"item_key": normalized_key},
            "normalized_changes": {"tags": final_tags, "added_tags": added_tags},
            "base_snapshot": snapshot,
            "base_fingerprint": record_fingerprint(snapshot),
            "diff": {
                "tags": {"before": snapshot["tags"], "after": final_tags},
                "added_tags": {"before": [], "after": added_tags},
                "version": {"before": snapshot["version"], "after": "optimistic-concurrency-checked"},
            },
            "policy": {
                "side_effect_class": "external_write",
                "requires_confirmation": True,
                "reasons": ["Explicit Zotero tag synchronization requested by the user"],
            },
            "warnings": ["Apply re-reads the item and rejects a changed Zotero version."],
            "created_at": effective_now.isoformat(),
            "expires_at": (effective_now + PROPOSAL_TTL).isoformat(),
        }
        state["proposals"][proposal_id] = proposal
        save_proposal_state(self.root_dir, state)
        self._event("pi.zotero.mutation.proposed", proposal, request_id)
        return proposal

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        normalized = required_text(proposal_id, "proposal_id")
        proposal = load_proposal_state(self.root_dir)["proposals"].get(normalized)
        if proposal is None:
            raise MutationError("not_found", f"Zotero mutation proposal '{normalized}' was not found")
        return dict(proposal)

    def apply(
        self,
        proposal_id: str,
        *,
        confirmation: Mapping[str, Any] | None,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized = required_text(proposal_id, "proposal_id")
        state = load_proposal_state(self.root_dir)
        committed = state["commits"].get(normalized)
        if committed is not None:
            replay = dict(committed["response"])
            replay["idempotent_replay"] = True
            self._event(
                "pi.zotero.mutation.replayed",
                {"proposal_id": normalized, "operation": replay.get("operation", OPERATION), "target": {}},
                request_id,
            )
            return replay
        raise_if_rejected(state, normalized)

        proposal = state["proposals"].get(normalized)
        if proposal is None:
            raise MutationError("not_found", f"Zotero mutation proposal '{normalized}' was not found")
        effective_now = _coerce_now(now)
        if effective_now > datetime.fromisoformat(proposal["expires_at"]):
            self._reject("expired_proposal", proposal, request_id)
        if not valid_confirmation(confirmation, normalized):
            self._reject("confirmation_required", proposal, request_id)

        self._event("pi.zotero.mutation.confirmed", proposal, request_id)
        item_key = proposal["target"]["item_key"]
        current = self._read_snapshot(item_key)
        if record_fingerprint(current) != proposal["base_fingerprint"]:
            self._event("pi.zotero.mutation.stale", proposal, request_id, reason="target_changed")
            raise MutationError(
                "stale_proposal",
                "the Zotero item changed after this proposal was created",
                details={"item_key": item_key},
            )

        changes = proposal["normalized_changes"]
        result = self.client.update_tags(item_key, list(changes["tags"]), version=int(current["version"]))
        if result.status == "stale":
            self._event("pi.zotero.mutation.stale", proposal, request_id, reason="upstream_precondition_failed")
            raise MutationError("stale_proposal", "Zotero rejected the version precondition", details={"item_key": item_key})
        if result.status == "conflict":
            self._event("pi.zotero.mutation.conflict", proposal, request_id, reason="upstream_locked")
            raise MutationError("external_conflict", "Zotero is temporarily locked; no change was committed", details={"item_key": item_key})
        if result.status != "ok":
            self._event("pi.zotero.mutation.rejected", proposal, request_id, reason="external_write_failed", status=result.status)
            raise MutationError(
                "external_write_failed",
                result.error or "Zotero did not accept the tag update",
                details={"item_key": item_key, "status": result.status, "backend": result.backend},
            )

        committed_event = self._event(
            "pi.zotero.mutation.committed",
            proposal,
            request_id,
            backend=result.backend,
            version=current["version"],
            added_tags=list(changes["added_tags"]),
        )
        response = {
            "proposal_id": normalized,
            "operation": OPERATION,
            "backend": result.backend,
            "item_key": item_key,
            "version_before": current["version"],
            "version_after": result.extra.get("version_after"),
            "tags": list(changes["tags"]),
            "added_tags": list(changes["added_tags"]),
            "audit_event_id": committed_event["event_id"],
            "idempotent_replay": False,
        }
        state["commits"][normalized] = {
            "proposal_id": normalized,
            "committed_at": effective_now.isoformat(),
            "event_id": committed_event["event_id"],
            "response": response,
        }
        save_proposal_state(self.root_dir, state)
        return response

    def _read_snapshot(self, item_key: str) -> dict[str, Any]:
        result = self.client.get_item(item_key)
        if result.status != "ok":
            raise MutationError(
                "external_read_failed",
                result.error or "Zotero item could not be read",
                details={"item_key": item_key, "status": result.status, "backend": result.backend},
            )
        payload = result.data
        if not isinstance(payload, dict):
            raise MutationError("external_read_failed", "Zotero item response was not an object", details={"item_key": item_key})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        version = _version_from_item(payload, result)
        if version is None:
            raise MutationError(
                "version_required",
                "Zotero item response did not include a numeric version",
                details={"item_key": item_key, "backend": result.backend},
            )
        tags = _normalize_tags(data.get("tags", [])) if isinstance(data, dict) else []
        return {"item_key": item_key, "version": version, "tags": tags}

    def _event(self, event_type: str, proposal: Mapping[str, Any], request_id: str | None, **extra: Any) -> dict[str, Any]:
        return append_event(
            self.root_dir,
            event_type=event_type,
            payload={
                "proposal_id": proposal.get("proposal_id"),
                "operation": proposal.get("operation", OPERATION),
                "target": proposal.get("target", {}),
                "pi_tool_call_id": request_id,
                **extra,
            },
        )

    def _reject(self, code: str, proposal: Mapping[str, Any], request_id: str | None) -> None:
        self._event("pi.zotero.mutation.rejected", proposal, request_id, reason=code)
        if code == "confirmation_required":
            message = "interactive user confirmation is required"
        else:
            message = "the Zotero mutation proposal has expired"
        raise MutationError(code, message, details={"proposal_id": proposal.get("proposal_id")})


def _normalize_tag_names(tags: list[str]) -> list[str]:
    if not isinstance(tags, list):
        raise MutationError("invalid_argument", "tags must be a list of strings")
    result: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        if not isinstance(raw, str):
            raise MutationError("invalid_argument", "tags must be a list of strings")
        tag = raw.strip()
        if tag and tag not in seen:
            result.append(tag)
            seen.add(tag)
    return result


def _normalize_tags(raw_tags: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_tags, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, Any]] = set()
    for raw in raw_tags:
        if isinstance(raw, str):
            tag = raw.strip()
            tag_type = None
        elif isinstance(raw, dict):
            tag = str(raw.get("tag") or "").strip()
            tag_type = raw.get("type")
        else:
            continue
        if not tag:
            continue
        identity = (tag, tag_type)
        if identity in seen:
            continue
        item: dict[str, Any] = {"tag": tag}
        if tag_type is not None:
            item["type"] = tag_type
        result.append(item)
        seen.add(identity)
    return result


def _version_from_item(payload: Mapping[str, Any], result: ZoteroClientResult) -> int | None:
    candidates: list[Any] = [payload.get("version"), result.extra.get("version")]
    meta = payload.get("meta")
    if isinstance(meta, Mapping):
        candidates.append(meta.get("version"))
    for candidate in candidates:
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
        if isinstance(candidate, str) and candidate.strip().isdigit():
            return int(candidate.strip())
    return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _coerce_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
