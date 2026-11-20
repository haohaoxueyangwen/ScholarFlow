"""Evidence-bound candidate knowledge with explicit human promotion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .enums import KnowledgeRelationType
from .event_bus import append_event
from .models import KnowledgeRelation
from .mutations import MutationError, record_fingerprint
from .persistence import atomic_write_json, file_lock
from .proposal_state import valid_confirmation
from .storage import ScholarFlowStore

CANDIDATE_KINDS = {"entity", "claim", "edge"}
CANDIDATE_STATUSES = {"pending", "promoted", "rejected"}
REVIEW_DECISIONS = {"promote", "reject"}
STATE_VERSION = 1
PROPOSAL_TTL = timedelta(hours=24)


class CandidateKnowledgeService:
    """Store untrusted extraction output outside confirmed research truth."""

    def __init__(self, store: ScholarFlowStore):
        self.store = store
        self.root_dir = store.root_dir
        self.path = self.root_dir / "_knowledge" / "candidates.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def submit(
        self,
        *,
        source_ref: str,
        source_version: str,
        source_text: str,
        extractor_name: str,
        extractor_version: str,
        candidates: Iterable[Mapping[str, Any]],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_source = _required(source_ref, "source_ref", maximum=500)
        normalized_version = _required(
            source_version,
            "source_version",
            maximum=500,
        )
        text = str(source_text or "")
        if not text.strip():
            raise MutationError("invalid_argument", "source_text is required")
        if len(text) > 5_000_000:
            raise MutationError(
                "invalid_argument",
                "source_text exceeds the 5000000 character limit",
            )
        extractor = {
            "name": _required(extractor_name, "extractor_name", maximum=200),
            "version": _required(
                extractor_version,
                "extractor_version",
                maximum=200,
            ),
        }
        source_text_hash = _digest(text)
        raw_candidates = list(candidates)
        if len(raw_candidates) > 1000:
            raise MutationError(
                "invalid_argument",
                "candidate batch exceeds the 1000 item limit",
            )
        normalized_items = [
            _normalize_candidate(
                item,
                source_ref=normalized_source,
                source_version=normalized_version,
                source_text=text,
                source_text_hash=source_text_hash,
                extractor=extractor,
            )
            for item in raw_candidates
        ]
        normalized = list({
            item["candidate_id"]: item
            for item in normalized_items
        }.values())
        if not normalized:
            raise MutationError(
                "invalid_argument",
                "at least one candidate is required",
            )
        created: list[str] = []
        unchanged: list[str] = []
        with file_lock(self.path):
            state = self._load()
            for candidate in normalized:
                candidate_id = candidate["candidate_id"]
                existing = state["candidates"].get(candidate_id)
                if existing is None:
                    state["candidates"][candidate_id] = candidate
                    created.append(candidate_id)
                elif _candidate_identity(existing) == _candidate_identity(candidate):
                    unchanged.append(candidate_id)
                else:
                    raise MutationError(
                        "candidate_identity_conflict",
                        f"candidate '{candidate_id}' has conflicting content",
                    )
            if created:
                self._save(state)
        event = append_event(
            self.root_dir,
            event_type="knowledge.candidates.submitted",
            payload={
                "source_ref": normalized_source,
                "source_text_hash": source_text_hash,
                "created": created,
                "unchanged": unchanged,
                "pi_tool_call_id": request_id,
            },
        )
        return {
            "status": "accepted",
            "source_ref": normalized_source,
            "source_text_hash": source_text_hash,
            "created": created,
            "unchanged": unchanged,
            "audit_event_id": event["event_id"],
        }

    def list(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        source_ref: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        normalized_status = _optional_choice(
            status,
            CANDIDATE_STATUSES,
            "status",
        )
        normalized_kind = _optional_choice(kind, CANDIDATE_KINDS, "kind")
        query_terms = str(query or "").strip().casefold().split()
        bounded_limit = max(1, min(int(limit), 500))
        items = list(self._load()["candidates"].values())
        items = [
            item
            for item in items
            if (normalized_status is None or item["status"] == normalized_status)
            and (normalized_kind is None or item["kind"] == normalized_kind)
            and (source_ref is None or item["source_ref"] == source_ref)
            and all(
                term in json.dumps(item, sort_keys=True, ensure_ascii=False).casefold()
                for term in query_terms
            )
        ]
        items.sort(
            key=lambda item: (
                item["updated_at"],
                item["candidate_id"],
            ),
            reverse=True,
        )
        return {
            "count": min(len(items), bounded_limit),
            "total": len(items),
            "items": items[:bounded_limit],
        }

    def get(self, candidate_id: str) -> dict[str, Any]:
        normalized = _required(candidate_id, "candidate_id", maximum=200)
        candidate = self._load()["candidates"].get(normalized)
        if candidate is None:
            raise MutationError(
                "not_found",
                f"knowledge candidate '{normalized}' was not found",
            )
        return dict(candidate)

    def propose_review(
        self,
        candidate_id: str,
        *,
        decision: str,
        note: str = "",
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        candidate = self.get(candidate_id)
        if candidate["status"] != "pending":
            raise MutationError(
                "invalid_state",
                "only pending candidates can be reviewed",
            )
        normalized_decision = _choice(
            decision,
            REVIEW_DECISIONS,
            "decision",
        )
        normalized_note = str(note or "").strip()[:2000]
        identity = {
            "operation": "knowledge_candidate.review",
            "target": {"candidate_id": candidate["candidate_id"]},
            "decision": normalized_decision,
            "note": normalized_note,
            "base_fingerprint": record_fingerprint(candidate),
        }
        proposal_id = _digest(_canonical(identity))
        effective_now = _utc(now)
        proposal = {
            "proposal_version": 1,
            "proposal_id": proposal_id,
            **identity,
            "diff": {
                "status": {
                    "before": "pending",
                    "after": (
                        "promoted"
                        if normalized_decision == "promote"
                        else "rejected"
                    ),
                }
            },
            "policy": {
                "side_effect_class": "domain_write",
                "requires_confirmation": True,
                "reasons": [
                    "promotes or rejects machine-extracted knowledge"
                ],
            },
            "created_at": effective_now.isoformat(),
            "expires_at": (effective_now + PROPOSAL_TTL).isoformat(),
        }
        with file_lock(self.path):
            state = self._load()
            state["proposals"].setdefault(proposal_id, proposal)
            self._save(state)
        append_event(
            self.root_dir,
            event_type="knowledge.candidate.review.proposed",
            payload={
                "proposal_id": proposal_id,
                "candidate_id": candidate["candidate_id"],
                "decision": normalized_decision,
                "pi_tool_call_id": request_id,
            },
        )
        return proposal

    def get_review_proposal(self, proposal_id: str) -> dict[str, Any]:
        normalized = _required(proposal_id, "proposal_id", maximum=200)
        proposal = self._load()["proposals"].get(normalized)
        if proposal is None:
            raise MutationError(
                "not_found",
                f"candidate review proposal '{normalized}' was not found",
            )
        return dict(proposal)

    def list_review_proposals(self) -> dict[str, Any]:
        state = self._load()
        items = [
            dict(proposal)
            for proposal_id, proposal in state["proposals"].items()
            if proposal_id not in state["commits"]
            and proposal_id not in state["rejections"]
        ]
        items.sort(
            key=lambda item: (
                item["created_at"],
                item["proposal_id"],
            ),
            reverse=True,
        )
        return {"count": len(items), "items": items}

    def has_review_proposal(self, proposal_id: str) -> bool:
        return proposal_id in self._load()["proposals"]

    def review_proposal_history(self) -> dict[str, Any]:
        state = self._load()
        items = [
            {**dict(receipt), "status": "applied"}
            for receipt in state["commits"].values()
        ]
        items.extend(dict(receipt) for receipt in state["rejections"].values())
        return {"count": len(items), "items": items}

    def reject_review_proposal(
        self,
        proposal_id: str,
        *,
        rejected_by: str,
        rejected_at: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        normalized = _required(proposal_id, "proposal_id", maximum=200)
        with file_lock(self.path):
            state = self._load()
            if normalized in state["commits"]:
                raise MutationError(
                    "proposal_already_committed",
                    "the proposal has already been applied",
                )
            proposal = state["proposals"].get(normalized)
            if proposal is None:
                raise MutationError(
                    "not_found",
                    f"candidate review proposal '{normalized}' was not found",
                )
            existing = state["rejections"].get(normalized)
            if existing is not None:
                return {**existing, "idempotent_replay": True}
            receipt = {
                "proposal_id": normalized,
                "operation": proposal["operation"],
                "target": proposal.get("target", {}),
                "decision": "rejected",
                "status": "rejected",
                "rejected_by": rejected_by,
                "rejected_at": rejected_at,
                "reason": reason,
                "request_id": request_id,
                "idempotent_replay": False,
            }
            state["rejections"][normalized] = receipt
            self._save(state)
        event = append_event(
            self.root_dir,
            event_type="knowledge.candidate.review.proposal_rejected",
            payload={
                "proposal_id": normalized,
                "candidate_id": proposal["target"]["candidate_id"],
                "reason": reason,
                "pi_tool_call_id": request_id,
            },
        )
        receipt["audit_event_id"] = event["event_id"]
        with file_lock(self.path):
            state = self._load()
            if normalized in state["rejections"]:
                state["rejections"][normalized] = dict(receipt)
                self._save(state)
        return receipt

    def apply_review(
        self,
        proposal_id: str,
        *,
        confirmation: Mapping[str, Any] | None,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized = _required(proposal_id, "proposal_id", maximum=200)
        with file_lock(self.path):
            state = self._load()
            committed = state["commits"].get(normalized)
            if committed is not None:
                response = dict(committed)
                response["idempotent_replay"] = True
                return response
            if normalized in state["rejections"]:
                raise MutationError(
                    "proposal_rejected",
                    "the proposal was rejected by the user",
                )
            proposal = state["proposals"].get(normalized)
            if proposal is None:
                raise MutationError(
                    "not_found",
                    f"candidate review proposal '{normalized}' was not found",
                )
            if _utc(now) > datetime.fromisoformat(proposal["expires_at"]):
                raise MutationError("expired_proposal", "the proposal expired")
            if not valid_confirmation(confirmation, normalized):
                raise MutationError(
                    "confirmation_required",
                    "interactive user confirmation is required",
                )
            candidate_id = proposal["target"]["candidate_id"]
            candidate = state["candidates"].get(candidate_id)
            if candidate is None:
                raise MutationError(
                    "not_found",
                    f"knowledge candidate '{candidate_id}' was not found",
                )
            if record_fingerprint(candidate) != proposal["base_fingerprint"]:
                raise MutationError(
                    "stale_proposal",
                    "the candidate changed after review was proposed",
                )
            updated = dict(candidate)
            updated["status"] = (
                "promoted"
                if proposal["decision"] == "promote"
                else "rejected"
            )
            updated["review"] = {
                "decision": proposal["decision"],
                "note": proposal["note"],
                "confirmed_by": confirmation["confirmed_by"],
                "confirmed_at": confirmation["confirmed_at"],
            }
            updated["updated_at"] = _utc(now).isoformat()
            if proposal["decision"] == "promote":
                updated["promoted_ref"] = self._promote(candidate)
            state["candidates"][candidate_id] = updated
            response = {
                "proposal_id": normalized,
                "operation": proposal["operation"],
                "record": updated,
                "idempotent_replay": False,
            }
            state["commits"][normalized] = response
            self._save(state)
        event = append_event(
            self.root_dir,
            event_type="knowledge.candidate.review.committed",
            payload={
                "proposal_id": normalized,
                "candidate_id": candidate_id,
                "decision": proposal["decision"],
                "promoted_ref": updated.get("promoted_ref"),
                "pi_tool_call_id": request_id,
            },
        )
        response["audit_event_id"] = event["event_id"]
        with file_lock(self.path):
            state = self._load()
            if normalized in state["commits"]:
                state["commits"][normalized] = dict(response)
                self._save(state)
        return response

    def _promote(self, candidate: Mapping[str, Any]) -> str:
        if candidate["kind"] != "edge":
            return f"{candidate['kind']}:{candidate['candidate_id']}"
        payload = candidate["payload"]
        relation_id = "relation-candidate-" + candidate["candidate_id"].split(
            "candidate-",
            1,
        )[-1][:20]
        expected = KnowledgeRelation(
            relation_id=relation_id,
            subject_ref=payload["subject_ref"],
            predicate=KnowledgeRelationType(payload["predicate"]),
            object_ref=payload["object_ref"],
            evidence_refs=[
                candidate["source_ref"],
                (
                    f"source-span:{candidate['source_text_hash']}:"
                    f"{candidate['evidence']['block_id']}"
                ),
            ],
            confidence=candidate["confidence"],
            note=(
                f"Promoted from {candidate['candidate_id']}; "
                f"quote: {candidate['evidence']['quote']}"
            )[:2000],
        )
        existing = self.store.get_knowledge_relation(relation_id)
        if existing is None:
            self.store.create_knowledge_relation(expected)
        elif _relation_identity(existing.to_dict()) != _relation_identity(
            expected.to_dict()
        ):
            raise MutationError(
                "promotion_conflict",
                f"relation '{relation_id}' conflicts with this candidate",
            )
        return f"knowledge_relation:{relation_id}"

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": STATE_VERSION,
                "revision": 0,
                "candidates": {},
                "proposals": {},
                "commits": {},
                "rejections": {},
            }
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("version") != STATE_VERSION:
            raise MutationError(
                "candidate_store_version_unsupported",
                "candidate knowledge store version is unsupported",
            )
        payload.setdefault("revision", 0)
        payload.setdefault("candidates", {})
        payload.setdefault("proposals", {})
        payload.setdefault("commits", {})
        payload.setdefault("rejections", {})
        return payload

    def _save(self, state: dict[str, Any]) -> None:
        state["version"] = STATE_VERSION
        state["revision"] = int(state.get("revision", 0)) + 1
        atomic_write_json(self.path, state)


def _normalize_candidate(
    raw: Mapping[str, Any],
    *,
    source_ref: str,
    source_version: str,
    source_text: str,
    source_text_hash: str,
    extractor: Mapping[str, str],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise MutationError("invalid_argument", "candidate must be an object")
    kind = _choice(raw.get("kind"), CANDIDATE_KINDS, "candidate kind")
    quote = _required(
        raw.get("evidence_quote"),
        "evidence_quote",
        maximum=2000,
    )
    occurrences = source_text.count(quote)
    if occurrences != 1:
        raise MutationError(
            "evidence_span_invalid",
            "evidence_quote must occur exactly once in source_text",
            details={"occurrences": occurrences},
        )
    confidence = raw.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise MutationError(
            "invalid_argument",
            "candidate confidence must be numeric",
        )
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise MutationError(
            "invalid_argument",
            "candidate confidence must be between 0 and 1",
        )
    if kind == "entity":
        name = _required(raw.get("name"), "entity name", maximum=500)
        payload = {
            "name": name,
            "normalized_name": str(
                raw.get("normalized_name") or name
            ).strip().casefold()[:500],
            "entity_type": _required(
                raw.get("entity_type"),
                "entity_type",
                maximum=100,
            ),
            "aliases": _string_list(raw.get("aliases", ()), maximum=50),
        }
    elif kind == "claim":
        payload = {
            "text": _required(raw.get("text"), "claim text", maximum=2000),
            "claim_type": _required(
                raw.get("claim_type"),
                "claim_type",
                maximum=100,
            ),
        }
    else:
        predicate = _choice(
            raw.get("predicate"),
            {item.value for item in KnowledgeRelationType},
            "edge predicate",
        )
        payload = {
            "subject_ref": _required(
                raw.get("subject_ref"),
                "subject_ref",
                maximum=500,
            ),
            "predicate": predicate,
            "object_ref": _required(
                raw.get("object_ref"),
                "object_ref",
                maximum=500,
            ),
        }
        if payload["subject_ref"] == payload["object_ref"]:
            raise MutationError(
                "invalid_argument",
                "edge subject_ref and object_ref must differ",
            )
    identity = {
        "kind": kind,
        "source_ref": source_ref,
        "source_version": source_version,
        "source_text_hash": source_text_hash,
        "payload": payload,
        "evidence": {
            "quote": quote,
            "quote_start": source_text.index(quote),
            "quote_end": source_text.index(quote) + len(quote),
            "block_id": _required(
                raw.get("block_id"),
                "block_id",
                maximum=200,
            ),
            "page": _optional_positive_int(raw.get("page"), "page"),
        },
        "extractor": dict(extractor),
        "confidence": confidence,
    }
    candidate_id = "candidate-" + sha256(
        _canonical(identity).encode("utf-8")
    ).hexdigest()[:24]
    timestamp = _utc().isoformat()
    return {
        "candidate_id": candidate_id,
        **identity,
        "status": "pending",
        "review": None,
        "promoted_ref": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _candidate_identity(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: candidate.get(key)
        for key in (
            "candidate_id",
            "kind",
            "source_ref",
            "source_version",
            "source_text_hash",
            "payload",
            "evidence",
            "extractor",
            "confidence",
        )
    }


def _relation_identity(relation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: relation.get(key)
        for key in (
            "relation_id",
            "subject_ref",
            "predicate",
            "object_ref",
            "evidence_refs",
            "confidence",
        )
    }


def _required(value: Any, field: str, *, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise MutationError("invalid_argument", f"{field} is required")
    if len(normalized) > maximum:
        raise MutationError(
            "invalid_argument",
            f"{field} exceeds the {maximum} character limit",
        )
    return normalized


def _choice(value: Any, allowed: set[str], field: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in allowed:
        raise MutationError(
            "invalid_argument",
            f"{field} must be one of: {', '.join(sorted(allowed))}",
        )
    return normalized


def _optional_choice(
    value: Any,
    allowed: set[str],
    field: str,
) -> str | None:
    if value is None or not str(value).strip():
        return None
    return _choice(value, allowed, field)


def _string_list(value: Any, *, maximum: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise MutationError("invalid_argument", "aliases must be an array")
    items = [str(item).strip() for item in value if str(item).strip()]
    if len(items) > maximum:
        raise MutationError(
            "invalid_argument",
            f"aliases exceeds the {maximum} item limit",
        )
    return list(dict.fromkeys(items))


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise MutationError("invalid_argument", f"{field} must be an integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise MutationError(
            "invalid_argument",
            f"{field} must be an integer",
        ) from exc
    if normalized < 1:
        raise MutationError(
            "invalid_argument",
            f"{field} must be positive",
        )
    return normalized


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


def _utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
