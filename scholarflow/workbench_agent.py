"""Interactive Pi RPC sessions for the local ScholarFlow workbench.

This module is a presentation/control adapter around Pi RPC. It does not plan,
route research workflows, execute a second model loop, or bypass ScholarFlow
mutation governance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable, Mapping, Protocol
import uuid

from .agent_observer import capture_authoritative_state, observe_agent_turn
from .context_router import format_context_route, route_context
from .conversation import Conversation
from .event_bus import append_event
from .persistence import atomic_write_json
from .pi_lifecycle import node_command
from .pi_rpc_host import PiRpcConfig, PiRpcHostError, SubprocessPiRpcProcess


WORKBENCH_AGENT_STATE_FILENAME = "workbench_agent_sessions.json"
INITIAL_RPC_TIMEOUT_SECONDS = 90.0
MAX_PROMPT_CHARS = 20_000
MAX_CONTEXT_REFS = 20
MAX_EVENTS = 1_000
SESSION_EVENT_SEGMENT_SIZE = 1_000
CONVERSATION_SNAPSHOT_INTERVAL = 256
MAX_CHECKPOINTS = 500
MAX_CONTEXT_MANIFESTS = 1_000
MAX_OBSERVATIONS = 1_000
SESSION_EVENT_SCHEMA_VERSION = 1
CONTEXT_MANIFEST_SCHEMA_VERSION = 2
DEFAULT_CONTEXT_TARGET_TOKENS = 400_000
DEFAULT_RESPONSE_RESERVE_TOKENS = 16_384
DEFAULT_KEEP_RECENT_TOKENS = 64_000
SESSION_SCOPES = {"global", "project", "task"}
DIALOG_METHODS = {"select", "confirm", "input", "editor"}
FIRE_AND_FORGET_METHODS = {
    "notify",
    "setStatus",
    "setWidget",
    "setTitle",
    "set_editor_text",
}
SENSITIVE_KEY_MARKERS = ("api_key", "password", "secret", "authorization")
DEFAULT_PI_PROVIDER = "deepseek"
DEFAULT_PI_MODEL = "deepseek-v4-flash"


class WorkbenchAgentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InteractiveRpcProcess(Protocol):
    pid: int | None

    def start(self) -> None: ...

    def is_alive(self) -> bool: ...

    def request_interactive(
        self,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
        event_sink: Callable[[dict[str, Any]], None] | None,
        ui_responder: Callable[[dict[str, Any], float], dict[str, Any]] | None,
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


@dataclass
class _ActiveSession:
    session_id: str
    process: InteractiveRpcProcess
    session_file: str | None
    pi_session_id: str | None
    name: str = "ScholarFlow Workbench"
    scope: str = "global"
    project_id: str | None = None
    parent_session_id: str | None = None
    title: str = "ScholarFlow Workbench"
    objective: str = ""
    status: str = "ready"
    created_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())
    events: list[dict[str, Any]] = field(default_factory=list)
    conversation: Conversation = field(default_factory=Conversation)
    next_sequence: int = 1
    pending_ui: dict[str, dict[str, Any]] = field(default_factory=dict)
    ui_responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    turn_state_before: dict[str, dict[str, Any]] = field(default_factory=dict)
    worker: threading.Thread | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    condition: threading.Condition = field(init=False)

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)


class WorkbenchAgentSessionService:
    """Manage hierarchical logical sessions over one active Pi subprocess.

    Global, project, and task sessions retain separate Pi session files.  Only
    one subprocess is kept alive at a time; switching sessions parks the old
    process and resumes the requested logical session.  Durable checkpoints
    are the bounded hand-off contract between the hierarchy levels.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        project_root: Path | None = None,
        process_factory: Callable[[PiRpcConfig], InteractiveRpcProcess] | None = None,
        request_timeout_seconds: float = 300.0,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.project_root = Path(project_root or Path(__file__).resolve().parents[1])
        self.process_factory = process_factory or SubprocessPiRpcProcess
        self.request_timeout_seconds = float(request_timeout_seconds)
        self._sessions: dict[str, _ActiveSession] = {}
        self._conversation_cache: dict[str, tuple[tuple[int, ...], Conversation]] = {}
        self._lock = threading.RLock()

    def create(
        self,
        *,
        name: str = "ScholarFlow Workbench",
        resume_session_id: str | None = None,
        reuse_latest: bool = True,
        scope: str = "global",
        project_id: str | None = None,
        parent_session_id: str | None = None,
        title: str | None = None,
        objective: str | None = None,
    ) -> dict[str, Any]:
        normalized_name = str(name or "").strip()[:120] or "ScholarFlow Workbench"
        metadata = _normalize_session_metadata(
            scope=scope,
            project_id=project_id,
            parent_session_id=parent_session_id,
            title=title or normalized_name,
            objective=objective,
        )
        with self._lock:
            records = self._load_records()
            _validate_parent_session(records, metadata)
            prior = None
            if resume_session_id:
                prior = records.get(str(resume_session_id))
                if prior is None:
                    raise WorkbenchAgentError(
                        "not_found",
                        f"workbench Pi session '{resume_session_id}' was not found",
                    )
                _validate_resume_metadata(prior, metadata)
            elif reuse_latest and metadata["scope"] != "task":
                prior = _latest_matching_record(records, metadata)
            session_id = (
                str(resume_session_id)
                if resume_session_id
                else str((prior or {}).get("session_id") or "")
                if prior
                else f"wb-pi-{uuid.uuid4().hex}"
            )
            already_active = self._sessions.get(session_id)
            if (
                already_active is not None
                and already_active.status not in {"stopped", "failed", "interrupted"}
                and already_active.process.is_alive()
            ):
                return self.get(session_id)
            self._park_other_sessions(session_id)
            session_file = str((prior or {}).get("session_file") or "") or None
            config = PiRpcConfig(
                command=self._command(
                    name=normalized_name,
                    session_file=session_file,
                ),
                cwd=self.project_root,
                trust_mode="approved_readonly",
                request_timeout_seconds=self.request_timeout_seconds,
            )
            process = self.process_factory(config)
            try:
                process.start()
                state_response = process.request_interactive(
                    {
                        "id": f"state-{uuid.uuid4().hex}",
                        "type": "get_state",
                    },
                    timeout_seconds=min(
                        INITIAL_RPC_TIMEOUT_SECONDS,
                        self.request_timeout_seconds,
                    ),
                    event_sink=None,
                    ui_responder=None,
                )
            except Exception:
                process.close()
                raise
            state = dict(state_response.get("data") or {})
            snapshot = self._load_conversation_snapshot(session_id)
            if snapshot is None:
                durable_events = self._load_session_events(session_id)
                conversation = Conversation(durable_events, session_id=session_id)
            else:
                durable_events = self._load_session_events(session_id, after=snapshot.sequence)
                conversation = snapshot
                for durable_event in durable_events:
                    conversation.append(durable_event)
            had_durable_events = bool(conversation.sequence or durable_events)
            prior_cursor = dict((prior or {}).get("resume_cursor") or {})
            latest_sequence = max(
                conversation.sequence,
                int(durable_events[-1].get("sequence", 0)) if durable_events else 0,
                int(prior_cursor.get("sequence", 0) or 0),
            )
            recent_events = self._load_session_events(
                session_id,
                after=max(0, latest_sequence - MAX_EVENTS),
                limit=MAX_EVENTS,
            )
            active = _ActiveSession(
                session_id=session_id,
                process=process,
                session_file=str(state.get("sessionFile") or "") or session_file,
                pi_session_id=str(state.get("sessionId") or "") or None,
                name=normalized_name,
                scope=metadata["scope"],
                project_id=metadata["project_id"],
                parent_session_id=metadata["parent_session_id"],
                title=metadata["title"],
                objective=metadata["objective"],
                created_at=str((prior or {}).get("created_at") or _now()),
                events=recent_events,
                conversation=conversation,
                next_sequence=latest_sequence + 1,
            )
            self._sessions[session_id] = active
            if conversation.sequence:
                try:
                    self._write_conversation_snapshot(session_id, conversation)
                except OSError:
                    pass
            self._append(
                active,
                {
                    "type": "workbench_session_started",
                    "resumed": prior is not None,
                    "model": _model_view(state.get("model")),
                    "thinkingLevel": state.get("thinkingLevel"),
                },
            )
            if prior is not None and not had_durable_events:
                try:
                    messages_response = process.request_interactive(
                        {
                            "id": f"messages-{uuid.uuid4().hex}",
                            "type": "get_messages",
                        },
                        timeout_seconds=min(
                            INITIAL_RPC_TIMEOUT_SECONDS,
                            self.request_timeout_seconds,
                        ),
                        event_sink=None,
                        ui_responder=None,
                    )
                    self._restore_messages(
                        active,
                        list(
                            dict(messages_response.get("data") or {}).get(
                                "messages"
                            )
                            or []
                        ),
                    )
                except (PiRpcHostError, TypeError, ValueError):
                    self._append(
                        active,
                        {
                            "type": "workbench_history_unavailable",
                            "message": "Pi session resumed, but prior messages could not be restored.",
                        },
                    )
            self._save(active)
            append_event(
                self.data_dir,
                event_type="workbench.pi.session.started",
                payload={
                    "session_id": session_id,
                    "resumed": prior is not None,
                    "pi_session_id": active.pi_session_id,
                    "scope": active.scope,
                    "project_id": active.project_id,
                },
            )
            return self.get(session_id)

    def get(self, session_id: str) -> dict[str, Any]:
        normalized = str(session_id or "").strip()
        with self._lock:
            active = self._sessions.get(normalized)
        if active is not None:
            with active.lock:
                return self._view(active)
        record = self._load_records().get(normalized)
        if record is None:
            raise WorkbenchAgentError(
                "not_found",
                f"workbench Pi session '{normalized}' was not found",
            )
        return self._inactive_view(record)

    def list(self) -> dict[str, Any]:
        records = self._load_records()
        for record in records.values():
            record["alive"] = False
            record["resumable"] = bool(record.get("session_file"))
            if record.get("status") in {"ready", "running"}:
                record["status"] = "interrupted"
        with self._lock:
            active_sessions = list(self._sessions.items())
        for session_id, active in active_sessions:
            with active.lock:
                records[session_id] = {
                    **self._view(active),
                    "resumable": bool(active.session_file),
                }
        items = sorted(
            records.values(),
            key=lambda item: (str(item.get("updated_at") or ""), str(item.get("session_id") or "")),
            reverse=True,
        )
        return {"count": len(items), "items": items[:100]}

    def checkpoints(
        self,
        *,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_session = str(session_id or "").strip()
        normalized_project = str(project_id or "").strip()
        items = list(self._load_state()["checkpoints"].values())
        if normalized_session:
            items = [item for item in items if item.get("session_id") == normalized_session]
        if normalized_project:
            items = [item for item in items if item.get("project_id") == normalized_project]
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {"count": len(items), "items": items[:100]}

    def context_manifests(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_session = str(session_id or "").strip()
        if normalized_session not in self._load_records():
            raise WorkbenchAgentError(
                "not_found",
                f"workbench Pi session '{normalized_session}' was not found",
            )
        normalized_request = str(request_id or "").strip()
        items = [
            dict(item)
            for item in self._load_state()["context_manifests"].values()
            if item.get("session_id") == normalized_session
            and (
                not normalized_request
                or item.get("request_id") == normalized_request
            )
        ]
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {"count": len(items), "items": items[:100]}

    def observations(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_session = str(session_id or "").strip()
        if normalized_session not in self._load_records():
            raise WorkbenchAgentError(
                "not_found",
                f"workbench Pi session '{normalized_session}' was not found",
            )
        normalized_request = str(request_id or "").strip()
        items = [
            dict(item)
            for item in self._load_state()["observations"].values()
            if item.get("session_id") == normalized_session
            and (
                not normalized_request
                or item.get("request_id") == normalized_request
            )
        ]
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {"count": len(items), "items": items[:100]}

    def create_checkpoint(
        self,
        session_id: str,
        *,
        title: str | None = None,
        summary: str | None = None,
        decisions: list[str] | None = None,
        open_questions: list[str] | None = None,
        next_actions: list[str] | None = None,
        evidence_refs: list[str] | None = None,
        automatic: bool = False,
    ) -> dict[str, Any]:
        record = self.get(session_id)
        transcript = self._conversation(str(session_id))
        coverage = transcript.handoff()
        structured = transcript.structured_handoff() if not summary else None
        if structured:
            summary = structured["summary"]
            decisions = decisions if decisions is not None else structured["decisions"]
            open_questions = open_questions if open_questions is not None else structured["open_questions"]
            next_actions = next_actions if next_actions is not None else structured["next_actions"]
            evidence_refs = evidence_refs if evidence_refs is not None else structured["evidence_refs"]
        derived_summary = str(summary or "").strip()
        if not derived_summary:
            derived_summary = coverage["summary"]
        if not derived_summary:
            derived_summary = "No conversation summary was available; use the linked formal project state."
        checkpoint = {
            "checkpoint_id": f"checkpoint-{uuid.uuid4().hex}",
            "session_id": record["session_id"],
            "scope": record.get("scope", "global"),
            "project_id": record.get("project_id"),
            "parent_session_id": record.get("parent_session_id"),
            "title": str(title or record.get("title") or "Session checkpoint").strip()[:160],
            "summary": derived_summary,
            "summary_kind": "pi_structured_handoff" if structured else "supplied_handoff" if summary else "verbatim_recent_messages",
            "coverage": {key: value for key, value in coverage.items() if key != "summary"},
            "authoritative": False,
            "requires_fact_verification": True,
            "decisions": _normalize_checkpoint_list(decisions),
            "open_questions": _normalize_checkpoint_list(open_questions),
            "next_actions": _normalize_checkpoint_list(next_actions),
            "evidence_refs": _normalize_context_refs(evidence_refs or []),
            "automatic": bool(automatic),
            "created_at": _now(),
        }
        with self._lock:
            state = self._load_state()
            state["checkpoints"][checkpoint["checkpoint_id"]] = checkpoint
            ordered = sorted(
                state["checkpoints"].values(),
                key=lambda item: str(item.get("created_at") or ""),
                reverse=True,
            )[:MAX_CHECKPOINTS]
            state["checkpoints"] = {item["checkpoint_id"]: item for item in ordered}
            self._write_state(state)
        append_event(
            self.data_dir,
            event_type="workbench.pi.checkpoint.created",
            payload={
                "checkpoint_id": checkpoint["checkpoint_id"],
                "session_id": checkpoint["session_id"],
                "scope": checkpoint["scope"],
                "project_id": checkpoint["project_id"],
                "automatic": checkpoint["automatic"],
            },
        )
        return checkpoint

    def events(self, session_id: str, *, after: int = 0) -> dict[str, Any]:
        normalized_session = str(session_id or "").strip()
        cursor = max(0, int(after))
        with self._lock:
            active = self._sessions.get(normalized_session)
        if active is not None:
            with active.lock:
                items = [
                    dict(item)
                    for item in active.events
                    if int(item.get("sequence", 0)) > cursor
                ]
                next_cursor = max(cursor, active.next_sequence - 1)
                return {
                    "session_id": active.session_id,
                    "status": active.status,
                    "items": items,
                    "next_cursor": next_cursor,
                    "event_gap": bool(
                        next_cursor > cursor
                        and (not active.events or cursor < int(active.events[0]["sequence"]) - 1)
                    ),
                    "messages": active.conversation.page(),
                    "resume_cursor": _resume_cursor(
                        active.session_id,
                        active.events,
                        next_cursor,
                    ),
                    "pending_ui": [
                        _public_ui_request(item)
                        for item in active.pending_ui.values()
                    ],
                }
        record = self._load_records().get(normalized_session)
        if record is None:
            raise WorkbenchAgentError(
                "not_found",
                f"workbench Pi session '{normalized_session}' was not found",
            )
        snapshot = self._load_conversation_snapshot(normalized_session)
        items = self._load_session_events(normalized_session, after=cursor, limit=MAX_EVENTS)
        record_cursor = dict(record.get("resume_cursor") or {})
        next_cursor = max(
            cursor,
            snapshot.sequence if snapshot else 0,
            int(record_cursor.get("sequence", 0) or 0),
            int(items[-1].get("sequence", 0)) if items else 0,
        )
        status = str(record.get("status") or "interrupted")
        if status in {"ready", "running"}:
            status = "interrupted"
        return {
            "session_id": normalized_session,
            "status": status,
            "items": items,
            "next_cursor": next_cursor,
            "event_gap": bool(
                (snapshot and snapshot.sequence > cursor)
                or (items and cursor < int(items[0]["sequence"]) - 1)
            ),
            "messages": self._conversation(normalized_session).page(),
            "resume_cursor": _resume_cursor(
                normalized_session,
                items,
                next_cursor,
            ),
            "pending_ui": [],
        }

    def _conversation(self, session_id: str) -> Conversation:
        active = self._sessions.get(session_id)
        if active is not None:
            return active.conversation
        signature = self._session_event_signature(session_id)
        cached = self._conversation_cache.get(session_id)
        if cached is None or cached[0] != signature:
            conversation = self._load_conversation_snapshot(session_id)
            snapshot_sequence = conversation.sequence if conversation else 0
            if conversation is None:
                events = self._load_session_events(session_id)
                conversation = Conversation(events, session_id=session_id)
            else:
                events = self._load_session_events(session_id, after=conversation.sequence)
                for event in events:
                    conversation.append(event)
            if conversation.sequence and conversation.sequence > snapshot_sequence:
                try:
                    self._write_conversation_snapshot(session_id, conversation)
                except OSError:
                    pass
            signature = self._session_event_signature(session_id)
            cached = (signature, conversation)
            self._conversation_cache[session_id] = cached
        return cached[1]

    def messages(self, session_id: str, *, before: int = 0, limit: int = 50) -> dict[str, Any]:
        self.get(session_id)
        active = self._sessions.get(session_id)
        if active is not None:
            with active.lock:
                return active.conversation.page(before=before, limit=limit)
        return self._conversation(session_id).page(before=before, limit=limit)

    def prompt(
        self,
        session_id: str,
        *,
        message: str,
        context_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        active = self._active(session_id)
        normalized_message = str(message or "").strip()
        if not normalized_message:
            raise WorkbenchAgentError("invalid_argument", "message is required")
        if len(normalized_message) > MAX_PROMPT_CHARS:
            raise WorkbenchAgentError(
                "invalid_argument",
                f"message exceeds the {MAX_PROMPT_CHARS} character limit",
            )
        normalized_refs = _normalize_context_refs(context_refs or [])
        with active.lock:
            if active.status == "running":
                raise WorkbenchAgentError(
                    "session_busy",
                    "the Pi session is already processing a prompt",
                )
            if active.status in {"stopped", "failed"} or not active.process.is_alive():
                raise WorkbenchAgentError(
                    "session_unavailable",
                    "the Pi session is not running; resume or create a session",
                )
            request_id = f"prompt-{uuid.uuid4().hex}"
            context_budget = _context_budget(self.project_root)
            context_route = route_context(
                normalized_message,
                scope=active.scope,
                context_refs=normalized_refs,
                context_target_tokens=context_budget["compaction_trigger_tokens"],
            )
            checkpoint_policy = context_route["checkpoint_policy"]
            coordination_items = self._coordination_checkpoints(
                active,
                max_items=int(checkpoint_policy["max_items"]),
            )
            coordination_context = _format_coordination_context(
                coordination_items,
                max_characters=int(checkpoint_policy["max_characters"]),
            )
            contextual_message = _contextual_prompt(
                normalized_message,
                normalized_refs,
                coordination_context=coordination_context,
                context_route=context_route,
            )
            context_manifest = self._build_context_manifest(
                active,
                request_id=request_id,
                user_message=normalized_message,
                contextual_message=contextual_message,
                context_refs=normalized_refs,
                coordination_items=coordination_items,
                context_route=context_route,
                context_budget=context_budget,
            )
            self._save_context_manifest(context_manifest)
            try:
                active.turn_state_before[request_id] = capture_authoritative_state(
                    self.data_dir
                )
            except Exception as exc:
                active.turn_state_before.pop(request_id, None)
                try:
                    self._append(
                        active,
                        {
                            "type": "workbench_observation_snapshot_failed",
                            "request_id": request_id,
                            "phase": "before",
                            "error": f"{type(exc).__name__}: {exc}"[:1_000],
                        },
                    )
                except Exception:
                    pass
            payload = {
                "id": request_id,
                "type": "prompt",
                "message": contextual_message,
            }
            active.status = "running"
            active.updated_at = _now()
            self._append(
                active,
                {
                    "type": "workbench_context_manifest_created",
                    "request_id": request_id,
                    "manifest_id": context_manifest["manifest_id"],
                    "estimated_new_prompt_tokens": context_manifest["budget"][
                        "estimated_new_prompt_tokens"
                    ],
                    "source_count": len(context_manifest["sources"]),
                    "workload": context_route["workload"],
                },
            )
            self._append(
                active,
                {
                    "type": "workbench_user_message",
                    "request_id": request_id,
                    "message": normalized_message,
                    "context_refs": normalized_refs,
                    "context_manifest_id": context_manifest["manifest_id"],
                },
            )
            active.worker = threading.Thread(
                target=self._run_prompt,
                args=(active, payload),
                name=f"scholarflow-workbench-pi-{session_id[-8:]}",
                daemon=True,
            )
            active.worker.start()
            self._save(active)
            return {
                "accepted": True,
                "session_id": session_id,
                "request_id": request_id,
                "context_refs": normalized_refs,
                "context_manifest_id": context_manifest["manifest_id"],
                "context_workload": context_route["workload"],
            }

    def respond_ui(
        self,
        session_id: str,
        request_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        active = self._active(session_id)
        normalized_id = str(request_id or "").strip()
        with active.condition:
            request = active.pending_ui.get(normalized_id)
            if request is None:
                raise WorkbenchAgentError(
                    "not_found",
                    f"pending Pi UI request '{normalized_id}' was not found",
                )
            response = _normalize_ui_response(request, payload)
            active.ui_responses[normalized_id] = response
            active.pending_ui.pop(normalized_id, None)
            self._append(
                active,
                {
                    "type": "workbench_ui_response",
                    "request_id": normalized_id,
                    "prompt_request_id": request.get("_prompt_request_id"),
                    "method": request.get("method"),
                    "cancelled": bool(response.get("cancelled")),
                    "confirmed": response.get("confirmed"),
                },
            )
            active.condition.notify_all()
            return {"accepted": True, "request_id": normalized_id}

    def stop(self, session_id: str) -> dict[str, Any]:
        active = self._active(session_id)
        active.process.close()
        with active.condition:
            active.status = "stopped"
            active.updated_at = _now()
            active.pending_ui.clear()
            active.condition.notify_all()
            self._append(active, {"type": "workbench_session_stopped"})
            self._save(active)
        append_event(
            self.data_dir,
            event_type="workbench.pi.session.stopped",
            payload={"session_id": session_id},
        )
        return self.get(session_id)

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for active in sessions:
            if active.process.is_alive():
                active.process.close()
            with active.condition:
                if active.status not in {"failed", "stopped"}:
                    active.status = "interrupted"
                    active.updated_at = _now()
                    active.pending_ui.clear()
                    active.condition.notify_all()
                    self._append(
                        active,
                        {
                            "type": "workbench_session_interrupted",
                            "reason": "service_shutdown",
                        },
                    )
                    self._save(active)

    def _run_prompt(
        self,
        active: _ActiveSession,
        payload: dict[str, Any],
    ) -> None:
        try:
            response = active.process.request_interactive(
                payload,
                timeout_seconds=self.request_timeout_seconds,
                event_sink=lambda event: self._append(
                    active,
                    event,
                    request_id=str(payload["id"]),
                ),
                ui_responder=lambda event, timeout: self._wait_for_ui(
                    active,
                    event,
                    timeout,
                    prompt_request_id=str(payload["id"]),
                ),
            )
            if not response.get("success", False):
                raise WorkbenchAgentError(
                    "prompt_rejected",
                    str(response.get("error") or "Pi rejected the prompt"),
                )
            with active.lock:
                active.status = "ready"
                active.updated_at = _now()
                self._append(
                    active,
                    {
                        "type": "workbench_prompt_completed",
                        "request_id": payload["id"],
                    },
                )
                self._record_observation(active, str(payload["id"]))
                self._save(active)
                if self._should_auto_checkpoint(active):
                    self.create_checkpoint(
                        active.session_id,
                        title=f"{active.title} · automatic hand-off",
                        automatic=True,
                    )
        except Exception as exc:
            with active.condition:
                active.status = "failed"
                active.updated_at = _now()
                active.pending_ui.clear()
                active.condition.notify_all()
                self._append(
                    active,
                    {
                        "type": "workbench_prompt_failed",
                        "request_id": payload["id"],
                        "error": f"{type(exc).__name__}: {exc}"[:1_000],
                    },
                )
                self._record_observation(active, str(payload["id"]))
                self._save(active)

    def _wait_for_ui(
        self,
        active: _ActiveSession,
        event: dict[str, Any],
        timeout_seconds: float,
        *,
        prompt_request_id: str,
    ) -> dict[str, Any]:
        request_id = str(event.get("id") or "")
        if str(event.get("method") or "") not in DIALOG_METHODS:
            return {
                "type": "extension_ui_response",
                "id": request_id,
                "cancelled": True,
            }
        with active.condition:
            active.pending_ui[request_id] = {
                **dict(event),
                "_prompt_request_id": prompt_request_id,
            }
            active.updated_at = _now()
            self._save(active)
            ready = active.condition.wait_for(
                lambda: (
                    request_id in active.ui_responses
                    or active.status in {"stopped", "failed", "interrupted"}
                ),
                timeout=max(0.0, float(timeout_seconds)),
            )
            active.pending_ui.pop(request_id, None)
            if ready and request_id in active.ui_responses:
                return active.ui_responses.pop(request_id)
            return {
                "type": "extension_ui_response",
                "id": request_id,
                "cancelled": True,
            }

    def _append(
        self,
        active: _ActiveSession,
        event: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> None:
        with active.lock:
            raw = dict(_redact_sensitive(dict(event)))
            event_type = str(raw.pop("type", "unknown") or "unknown")
            for canonical_key in (
                "schema_version",
                "event_id",
                "session_id",
                "sequence",
                "recorded_at",
                "source",
            ):
                raw.pop(canonical_key, None)
            if request_id and not raw.get("request_id"):
                raw["request_id"] = request_id
            normalized = {
                "schema_version": SESSION_EVENT_SCHEMA_VERSION,
                "event_id": f"wb-event-{uuid.uuid4().hex}",
                "session_id": active.session_id,
                "sequence": active.next_sequence,
                "recorded_at": _now(),
                "source": (
                    "workbench"
                    if event_type.startswith("workbench_")
                    else "pi"
                ),
                "type": event_type,
                **raw,
            }
            active.next_sequence += 1
            active.events.append(normalized)
            active.conversation.append(normalized)
            if len(active.events) > MAX_EVENTS:
                active.events = active.events[-MAX_EVENTS:]
            self._append_session_event(active.session_id, normalized)
            if (
                normalized["sequence"] % CONVERSATION_SNAPSHOT_INTERVAL == 0
                or event_type in {
                    "message_end",
                    "agent_settled",
                    "workbench_prompt_completed",
                    "workbench_prompt_failed",
                    "workbench_session_stopped",
                    "workbench_session_parked",
                }
            ):
                try:
                    self._write_conversation_snapshot(active.session_id, active.conversation)
                except OSError:
                    pass  # A derived snapshot must not change the Pi turn outcome.

    def _restore_messages(
        self,
        active: _ActiveSession,
        messages: list[Any],
    ) -> None:
        for message in messages[-100:]:
            if not isinstance(message, Mapping):
                continue
            role = str(message.get("role") or "")
            content = message.get("content")
            if role == "user":
                user_text = _message_text(content)
                if user_text:
                    self._append(
                        active,
                        {
                            "type": "workbench_user_message",
                            "message": _display_user_message(user_text),
                            "context_refs": [],
                            "restored": True,
                        },
                    )
            elif role == "assistant":
                assistant_text = _message_text(content)
                if assistant_text:
                    self._append(
                        active,
                        {
                            "type": "message_update",
                            "assistantMessageEvent": {
                                "type": "text_delta",
                                "delta": assistant_text,
                            },
                            "restored": True,
                        },
                    )
                    self._append(
                        active,
                        {"type": "agent_settled", "restored": True},
                    )

    def _active(self, session_id: str) -> _ActiveSession:
        normalized = str(session_id or "").strip()
        with self._lock:
            active = self._sessions.get(normalized)
        if active is None:
            raise WorkbenchAgentError(
                "not_found",
                f"active workbench Pi session '{normalized}' was not found",
            )
        return active

    def _view(self, active: _ActiveSession) -> dict[str, Any]:
        checkpoints = self.checkpoints(session_id=active.session_id)["items"]
        manifests = self.context_manifests(active.session_id)["items"]
        observations = self.observations(active.session_id)["items"]
        return {
            **self._record(active),
            "pid": active.process.pid,
            "alive": active.process.is_alive(),
            "pending_ui": [
                _public_ui_request(item)
                for item in active.pending_ui.values()
            ],
            "latest_sequence": active.next_sequence - 1,
            "latest_checkpoint": checkpoints[0] if checkpoints else None,
            "latest_context_manifest": manifests[0] if manifests else None,
            "latest_observation": observations[0] if observations else None,
        }

    def _record(self, active: _ActiveSession) -> dict[str, Any]:
        return {
            "session_id": active.session_id,
            "pi_session_id": active.pi_session_id,
            "session_file": active.session_file,
            "status": active.status,
            "name": active.name,
            "scope": active.scope,
            "project_id": active.project_id,
            "parent_session_id": active.parent_session_id,
            "title": active.title,
            "objective": active.objective,
            "created_at": active.created_at,
            "updated_at": active.updated_at,
            "resume_cursor": _resume_cursor(
                active.session_id,
                active.events,
                active.next_sequence - 1,
            ),
        }

    def _inactive_view(self, record: Mapping[str, Any]) -> dict[str, Any]:
        status = str(record.get("status") or "interrupted")
        if status in {"ready", "running"}:
            status = "interrupted"
        session_id = str(record.get("session_id") or "")
        checkpoints = self.checkpoints(session_id=session_id)["items"]
        manifests = self.context_manifests(session_id)["items"]
        observations = self.observations(session_id)["items"]
        return {
            **dict(record),
            "status": status,
            "alive": False,
            "resumable": bool(record.get("session_file")),
            "pending_ui": [],
            "latest_checkpoint": checkpoints[0] if checkpoints else None,
            "latest_context_manifest": manifests[0] if manifests else None,
            "latest_observation": observations[0] if observations else None,
        }

    def _load_records(self) -> dict[str, dict[str, Any]]:
        return self._load_state()["sessions"]

    def _load_state(self) -> dict[str, Any]:
        path = self.data_dir / WORKBENCH_AGENT_STATE_FILENAME
        payload: Mapping[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                payload = loaded if isinstance(loaded, Mapping) else {}
            except (OSError, json.JSONDecodeError):
                payload = {}
        sessions: dict[str, dict[str, Any]] = {}
        for key, value in dict(payload.get("sessions") or {}).items():
            if not isinstance(value, Mapping):
                continue
            record = dict(value)
            record.setdefault("session_id", str(key))
            record.setdefault("scope", "global")
            record.setdefault("project_id", None)
            record.setdefault("parent_session_id", None)
            record.setdefault("title", record.get("name") or "ScholarFlow Workbench")
            record.setdefault("objective", "")
            sessions[str(key)] = record
        checkpoints = {
            str(key): dict(value)
            for key, value in dict(payload.get("checkpoints") or {}).items()
            if isinstance(value, Mapping)
        }
        context_manifests = {
            str(key): dict(value)
            for key, value in dict(payload.get("context_manifests") or {}).items()
            if isinstance(value, Mapping)
        }
        observations = {
            str(key): dict(value)
            for key, value in dict(payload.get("observations") or {}).items()
            if isinstance(value, Mapping)
        }
        return {
            "sessions": sessions,
            "checkpoints": checkpoints,
            "context_manifests": context_manifests,
            "observations": observations,
        }

    def _write_state(self, state: Mapping[str, Any]) -> None:
        atomic_write_json(
            self.data_dir / WORKBENCH_AGENT_STATE_FILENAME,
            {
                "version": 4,
                "sessions": dict(state.get("sessions") or {}),
                "checkpoints": dict(state.get("checkpoints") or {}),
                "context_manifests": dict(
                    state.get("context_manifests") or {}
                ),
                "observations": dict(state.get("observations") or {}),
            },
        )

    def _save_context_manifest(self, manifest: Mapping[str, Any]) -> None:
        with self._lock:
            state = self._load_state()
            normalized = dict(manifest)
            state["context_manifests"][normalized["manifest_id"]] = normalized
            ordered = sorted(
                state["context_manifests"].values(),
                key=lambda item: str(item.get("created_at") or ""),
                reverse=True,
            )[:MAX_CONTEXT_MANIFESTS]
            state["context_manifests"] = {
                item["manifest_id"]: item for item in ordered
            }
            self._write_state(state)

    def _save_observation(self, observation: Mapping[str, Any]) -> None:
        with self._lock:
            state = self._load_state()
            normalized = dict(observation)
            state["observations"][normalized["observation_id"]] = normalized
            ordered = sorted(
                state["observations"].values(),
                key=lambda item: str(item.get("created_at") or ""),
                reverse=True,
            )[:MAX_OBSERVATIONS]
            state["observations"] = {
                item["observation_id"]: item for item in ordered
            }
            self._write_state(state)

    def _record_observation(
        self,
        active: _ActiveSession,
        request_id: str,
    ) -> None:
        try:
            before_state = active.turn_state_before.pop(request_id, None)
            after_state = capture_authoritative_state(self.data_dir)
            manifests = self.context_manifests(
                active.session_id,
                request_id=request_id,
            )["items"]
            observation = observe_agent_turn(
                session_id=active.session_id,
                request_id=request_id,
                events=active.events,
                context_manifest=manifests[0] if manifests else None,
                before_state=before_state,
                after_state=after_state,
            )
            self._save_observation(observation)
        except Exception as exc:
            active.turn_state_before.pop(request_id, None)
            try:
                self._append(
                    active,
                    {
                        "type": "workbench_observation_failed",
                        "request_id": request_id,
                        "error": f"{type(exc).__name__}: {exc}"[:1_000],
                    },
                )
            except Exception:
                pass
            return

        try:
            self._append(
                active,
                {
                    "type": "workbench_observation_created",
                    "request_id": request_id,
                    "observation_id": observation["observation_id"],
                    "status": observation["status"],
                    "signal_count": len(observation["signals"]),
                    "tool_call_count": observation["trajectory"][
                        "tool_call_count"
                    ],
                    "latency_ms": observation["cost"]["latency_ms"],
                },
            )
        except Exception:
            # Observation persistence is authoritative; a secondary session-log
            # failure must not alter the completed Pi prompt outcome.
            pass
        try:
            append_event(
                self.data_dir,
                event_type="workbench.pi.turn.observed",
                payload={
                    "session_id": active.session_id,
                    "request_id": request_id,
                    "observation_id": observation["observation_id"],
                    "status": observation["status"],
                    "signal_codes": [
                        item["code"] for item in observation["signals"]
                    ],
                },
            )
        except Exception as exc:
            try:
                self._append(
                    active,
                    {
                        "type": "workbench_observation_audit_failed",
                        "request_id": request_id,
                        "error": f"{type(exc).__name__}: {exc}"[:1_000],
                    },
                )
            except Exception:
                pass

    def _session_event_log_path(self, session_id: str) -> Path:
        return self.data_dir / "_workbench_session_events" / f"{self._session_digest(session_id)}.jsonl"

    @staticmethod
    def _session_digest(session_id: str) -> str:
        return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()

    def _session_event_segment_dir(self, session_id: str) -> Path:
        return self.data_dir / "_workbench_session_events" / self._session_digest(session_id)

    def _session_conversation_snapshot_path(self, session_id: str) -> Path:
        return self.data_dir / "_workbench_conversation_snapshots" / f"{self._session_digest(session_id)}.json"

    def _append_session_event(
        self,
        session_id: str,
        event: Mapping[str, Any],
    ) -> None:
        sequence = int(event.get("sequence") or 0)
        if sequence < 1:
            raise ValueError("a durable session event requires a positive sequence")
        segment = (sequence - 1) // SESSION_EVENT_SEGMENT_SIZE
        path = self._session_event_segment_dir(session_id) / f"segment-{segment:08d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    dict(event),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    def _load_session_events(
        self,
        session_id: str,
        *,
        after: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        cursor = max(0, int(after))
        bounded_limit = max(1, min(int(limit), 10_000)) if limit is not None else None
        sources = [self._session_event_log_path(session_id)]
        segment_dir = self._session_event_segment_dir(session_id)
        if segment_dir.exists():
            segment_paths = sorted(segment_dir.glob("segment-*.jsonl"))
            for segment_path in segment_paths:
                match = re.fullmatch(r"segment-(\d{8})\.jsonl", segment_path.name)
                if match and (int(match.group(1)) + 1) * SESSION_EVENT_SEGMENT_SIZE <= cursor:
                    continue
                sources.append(segment_path)
        events_by_sequence: dict[int, dict[str, Any]] = {}
        for path in sources:
            if not path.is_file():
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(item, Mapping):
                            continue
                        if item.get("session_id") != session_id:
                            continue
                        if item.get("schema_version") != SESSION_EVENT_SCHEMA_VERSION:
                            continue
                        try:
                            sequence = int(item.get("sequence", 0))
                        except (TypeError, ValueError):
                            continue
                        if sequence <= cursor:
                            continue
                        events_by_sequence[sequence] = dict(item)
                        if bounded_limit is not None and len(events_by_sequence) > bounded_limit * 2:
                            keep = sorted(events_by_sequence)[-bounded_limit:]
                            events_by_sequence = {key: events_by_sequence[key] for key in keep}
            except OSError:
                continue
        keys = sorted(events_by_sequence)
        if bounded_limit is not None:
            keys = keys[-bounded_limit:]
        return [events_by_sequence[key] for key in keys]

    def _load_conversation_snapshot(self, session_id: str) -> Conversation | None:
        path = self._session_conversation_snapshot_path(session_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return Conversation.from_snapshot(payload, session_id=session_id)

    def _write_conversation_snapshot(self, session_id: str, conversation: Conversation) -> None:
        atomic_write_json(self._session_conversation_snapshot_path(session_id), conversation.snapshot())

    def _session_event_signature(self, session_id: str) -> tuple[int, ...]:
        paths = [
            self._session_event_log_path(session_id),
            self._session_conversation_snapshot_path(session_id),
            *sorted(self._session_event_segment_dir(session_id).glob("segment-*.jsonl")),
        ]
        signature = [len(paths)]
        for path in paths:
            try:
                stat = path.stat()
                signature.extend((stat.st_mtime_ns, stat.st_size))
            except OSError:
                signature.extend((0, 0))
        return tuple(signature)

    def _save(self, active: _ActiveSession) -> None:
        with self._lock:
            state = self._load_state()
            state["sessions"][active.session_id] = self._record(active)
            self._write_state(state)

    def _park_other_sessions(self, selected_session_id: str) -> None:
        for session_id, active in list(self._sessions.items()):
            if session_id == selected_session_id or not active.process.is_alive():
                continue
            if active.status == "running":
                raise WorkbenchAgentError(
                    "session_busy",
                    "another Pi session is still running; wait for it before switching context",
                )
            active.process.close()
            with active.condition:
                active.status = "interrupted"
                active.updated_at = _now()
                active.pending_ui.clear()
                active.condition.notify_all()
                self._append(
                    active,
                    {
                        "type": "workbench_session_interrupted",
                        "reason": "context_switch",
                    },
                )
                self._save(active)
            self._sessions.pop(session_id, None)

    def _coordination_checkpoints(
        self,
        active: _ActiveSession,
        *,
        max_items: int = 10,
    ) -> list[dict[str, Any]]:
        checkpoints = list(self._load_state()["checkpoints"].values())
        if active.scope == "global":
            selected = checkpoints
        elif active.scope == "project":
            selected = [
                item for item in checkpoints
                if item.get("project_id") == active.project_id
            ]
        else:
            selected = [
                item for item in checkpoints
                if item.get("session_id") == active.session_id
                or item.get("session_id") == active.parent_session_id
            ]
        selected.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        results: list[dict[str, Any]] = []
        seen_sessions: set[str] = set()
        for item in selected:
            source_session = str(item.get("session_id") or "")
            if source_session in seen_sessions:
                continue
            seen_sessions.add(source_session)
            results.append(dict(item))
            if len(results) >= max(0, int(max_items)):
                break
        return results

    def _coordination_context(self, active: _ActiveSession) -> str:
        return _format_coordination_context(
            self._coordination_checkpoints(active)
        )

    def _build_context_manifest(
        self,
        active: _ActiveSession,
        *,
        request_id: str,
        user_message: str,
        contextual_message: str,
        context_refs: list[str],
        coordination_items: list[dict[str, Any]],
        context_route: Mapping[str, Any],
        context_budget: Mapping[str, Any],
    ) -> dict[str, Any]:
        sources: list[dict[str, Any]] = [
            {
                "source_type": "user_request",
                "source_id": request_id,
                "reason": "current_user_turn",
                "character_count": len(user_message),
                "estimated_tokens": _estimate_tokens(user_message),
            },
            {
                "source_type": "pi_session_context",
                "source_id": active.pi_session_id,
                "reason": "native_session_history_and_compaction",
                "measurement": "owned_by_pi_not_materialized_in_manifest",
                "estimated_tokens": None,
            },
        ]
        sources.extend(
            {
                "source_type": "object_reference",
                "source_id": ref,
                "reason": "user_selected_reference",
                "character_count": len(ref),
                "estimated_tokens": _estimate_tokens(ref),
            }
            for ref in context_refs
        )
        for item in coordination_items:
            summary = str(item.get("summary") or "")
            sources.append(
                {
                    "source_type": "checkpoint",
                    "source_id": item.get("checkpoint_id"),
                    "source_session_id": item.get("session_id"),
                    "scope": item.get("scope"),
                    "reason": "bounded_coordination_handoff",
                    "version": item.get("created_at"),
                    "character_count": len(summary),
                    "estimated_tokens": _estimate_tokens(summary),
                }
            )
        budget = dict(context_budget)
        budget["estimate_scope"] = "new_prompt_only"
        budget["estimated_new_prompt_tokens"] = _estimate_tokens(
            contextual_message
        )
        return {
            "schema_version": CONTEXT_MANIFEST_SCHEMA_VERSION,
            "manifest_id": f"context-{uuid.uuid4().hex}",
            "session_id": active.session_id,
            "request_id": request_id,
            "scope": active.scope,
            "project_id": active.project_id,
            "created_at": _now(),
            "policy_version": context_route["policy_version"],
            "prompt_sha256": hashlib.sha256(
                contextual_message.encode("utf-8")
            ).hexdigest(),
            "budget": budget,
            "route": dict(context_route),
            "sources": sources,
            "excluded_sources": [
                {
                    "source_type": "typed_truth",
                    "reason": "deferred to governed sf_* tools",
                },
                {
                    "source_type": "light_rag",
                    "reason": "deferred to provenance-checked retrieval",
                },
                {
                    "source_type": "generic_memory",
                    "reason": "on-demand preference/procedure hint only; never authoritative",
                },
                {
                    "source_type": "unselected_coordination_checkpoints",
                    "reason": (
                        "bounded by the workload-specific latest-per-session "
                        "checkpoint policy"
                    ),
                }
            ],
        }

    def _should_auto_checkpoint(self, active: _ActiveSession) -> bool:
        prompt_count = active.conversation.user_count
        if prompt_count == 0 or prompt_count % 8:
            return False
        existing = self.checkpoints(session_id=active.session_id)["items"]
        return not existing or existing[0].get("coverage", {}).get("through_sequence") != active.conversation.sequence

    def _command(
        self,
        *,
        name: str,
        session_file: str | None,
    ) -> tuple[str, ...]:
        cli = (
            self.project_root
            / "packages"
            / "scholarflow-pi"
            / "node_modules"
            / "@earendil-works"
            / "pi-coding-agent"
            / "dist"
            / "cli.js"
        )
        if not cli.exists():
            raise WorkbenchAgentError(
                "runtime_unavailable",
                "project-local Pi CLI is missing; install packages/scholarflow-pi",
            )
        session_dir = self.data_dir / "_pi_sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        command = [
            node_command(),
            str(cli),
            "--provider",
            os.environ.get("SCHOLARFLOW_PI_PROVIDER", DEFAULT_PI_PROVIDER),
            "--model",
            os.environ.get("SCHOLARFLOW_PI_MODEL", DEFAULT_PI_MODEL),
            "--mode",
            "rpc",
            "--session-dir",
            str(session_dir),
            "--no-builtin-tools",
            "--approve",
            "--name",
            name,
        ]
        if session_file:
            resolved = Path(session_file).resolve()
            if not resolved.is_relative_to(session_dir.resolve()):
                raise WorkbenchAgentError(
                    "scope_violation",
                    "Pi session file is outside the workbench session directory",
                )
            command.extend(["--session", str(resolved)])
        return tuple(command)


def _contextual_prompt(
    message: str,
    context_refs: list[str],
    *,
    coordination_context: str = "",
    context_route: Mapping[str, Any] | None = None,
) -> str:
    normalized_coordination = str(coordination_context or "").strip()
    route_block = format_context_route(dict(context_route)) if context_route else ""
    if not context_refs and not normalized_coordination and not route_block:
        return message
    refs = "\n".join(f"- {item}" for item in context_refs) or "- none"
    lane = _context_lane(context_refs)
    lane_gate = (
        f'\nRequired first tool call: sf_lane_enter with lane="{lane}". '
        "Do not call any referenced domain tool before lane activation."
        if lane
        else ""
    )
    coordination = (
        "\n\nBounded coordination checkpoints (conversation hand-offs only; "
        "verify every factual claim with sf_* tools):\n"
        f"{normalized_coordination}"
        if normalized_coordination
        else ""
    )
    routing = f"\n\n{route_block}" if route_block else ""
    return (
        "ScholarFlow workbench context (user-selected references only; verify "
        "all current facts with sf_* tools and do not treat this block as "
        f"research truth):\n{refs}{lane_gate}{coordination}{routing}"
        f"\n\nUser request:\n{message}"
    )


def _format_coordination_context(
    items: list[dict[str, Any]],
    *,
    max_characters: int = 10_000,
) -> str:
    selected = items[:10]
    quota = max(0, int(max_characters)) // max(1, len(selected))
    lines = []
    for item in selected:
        header = f"- [{item.get('scope')}] {item.get('title')} ({item.get('checkpoint_id')}; unverified handoff):\n"
        body = str(item.get("summary") or "")
        structured = "\n".join(f"{key}: {json.dumps(item.get(key) or [], ensure_ascii=False)}" for key in ("decisions", "open_questions", "next_actions", "evidence_refs"))
        available = max(0, quota - len(header) - len(structured) - 100)
        if len(body) > available:
            half = available // 2
            body = body[:half] + "\n[excerpt only; retrieve full checkpoint before relying on omitted content]\n" + (body[-half:] if half else "")
        lines.append(header + body + "\n" + structured)
    return "\n".join(lines)[:max(0, int(max_characters))]


def _estimate_tokens(value: str) -> int:
    text = str(value or "")
    if not text:
        return 0
    ascii_count = sum(ord(character) < 128 for character in text)
    non_ascii_count = len(text) - ascii_count
    return max(1, (ascii_count + 3) // 4 + non_ascii_count)


def _context_budget(project_root: Path) -> dict[str, Any]:
    target = _positive_int(
        os.environ.get("SCHOLARFLOW_CONTEXT_TARGET_TOKENS"),
        DEFAULT_CONTEXT_TARGET_TOKENS,
    )
    reserve = DEFAULT_RESPONSE_RESERVE_TOKENS
    keep_recent = DEFAULT_KEEP_RECENT_TOKENS
    settings_path = Path(project_root) / ".pi" / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        compaction = dict(settings.get("compaction") or {})
        reserve = _positive_int(compaction.get("reserveTokens"), reserve)
        keep_recent = _positive_int(
            compaction.get("keepRecentTokens"),
            keep_recent,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return {
        "estimator": "ascii_div_4_plus_non_ascii_v1",
        "context_window_tokens": target + reserve,
        "compaction_trigger_tokens": target,
        "response_reserve_tokens": reserve,
        "keep_recent_tokens": keep_recent,
        "settings_source": str(settings_path),
    }


def _positive_int(value: Any, default: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return default
    return normalized if normalized > 0 else default


def _resume_cursor(
    session_id: str,
    events: list[dict[str, Any]],
    sequence: int,
) -> dict[str, Any]:
    normalized_sequence = max(0, int(sequence))
    latest = next(
        (
            item
            for item in reversed(events)
            if int(item.get("sequence", 0)) == normalized_sequence
        ),
        None,
    )
    return {
        "schema_version": SESSION_EVENT_SCHEMA_VERSION,
        "session_id": session_id,
        "sequence": normalized_sequence,
        "event_id": (latest or {}).get("event_id"),
        "recorded_at": (latest or {}).get("recorded_at"),
    }


def _normalize_session_metadata(
    *,
    scope: str,
    project_id: str | None,
    parent_session_id: str | None,
    title: str | None,
    objective: str | None,
) -> dict[str, Any]:
    normalized_scope = str(scope or "global").strip().casefold()
    if normalized_scope not in SESSION_SCOPES:
        raise WorkbenchAgentError(
            "invalid_argument",
            "session scope must be global, project, or task",
        )
    normalized_project = str(project_id or "").strip() or None
    normalized_parent = str(parent_session_id or "").strip() or None
    if normalized_scope in {"project", "task"} and not normalized_project:
        raise WorkbenchAgentError(
            "invalid_argument",
            f"{normalized_scope} sessions require project_id",
        )
    if normalized_scope == "task" and not normalized_parent:
        raise WorkbenchAgentError(
            "invalid_argument",
            "task sessions require a parent project session",
        )
    if normalized_scope == "global":
        normalized_project = None
        normalized_parent = None
    return {
        "scope": normalized_scope,
        "project_id": normalized_project,
        "parent_session_id": normalized_parent,
        "title": str(title or "ScholarFlow Workbench").strip()[:160]
        or "ScholarFlow Workbench",
        "objective": str(objective or "").strip()[:1_000],
    }


def _validate_parent_session(
    records: Mapping[str, Mapping[str, Any]],
    metadata: Mapping[str, Any],
) -> None:
    parent_id = str(metadata.get("parent_session_id") or "")
    if not parent_id:
        return
    parent = records.get(parent_id)
    if parent is None:
        raise WorkbenchAgentError(
            "invalid_argument",
            f"parent session '{parent_id}' was not found",
        )
    parent_scope = str(parent.get("scope") or "global")
    child_scope = str(metadata.get("scope") or "global")
    valid = (
        (child_scope == "project" and parent_scope == "global")
        or (child_scope == "task" and parent_scope == "project")
    )
    if not valid:
        raise WorkbenchAgentError(
            "invalid_argument",
            f"{child_scope} sessions cannot use a {parent_scope} parent",
        )
    if child_scope == "task" and parent.get("project_id") != metadata.get("project_id"):
        raise WorkbenchAgentError(
            "invalid_argument",
            "task and parent project sessions must belong to the same project",
        )


def _validate_resume_metadata(
    prior: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> None:
    for field_name in ("scope", "project_id", "parent_session_id"):
        prior_value = prior.get(field_name)
        if field_name == "scope":
            prior_value = prior_value or "global"
        requested = metadata.get(field_name)
        if prior_value != requested:
            raise WorkbenchAgentError(
                "invalid_argument",
                f"resumed session {field_name} does not match the requested hierarchy",
            )


def _latest_matching_record(
    records: Mapping[str, Mapping[str, Any]],
    metadata: Mapping[str, Any],
) -> dict[str, Any] | None:
    matches = [
        dict(item)
        for item in records.values()
        if str(item.get("scope") or "global") == metadata.get("scope")
        and item.get("project_id") == metadata.get("project_id")
        and item.get("parent_session_id") == metadata.get("parent_session_id")
        and str(item.get("status") or "") != "stopped"
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: str(item.get("updated_at") or ""))


def _normalize_checkpoint_list(values: list[str] | None) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise WorkbenchAgentError("invalid_argument", "checkpoint fields must be arrays")
    normalized = []
    for value in values[:50]:
        item = str(value or "").strip()
        if item and item not in normalized:
            normalized.append(item[:500])
    return normalized


def _checkpoint_summary(events: list[dict[str, Any]]) -> str:
    return Conversation(events).handoff()["summary"]


def _context_lane(context_refs: list[str]) -> str | None:
    lanes = set()
    for ref in context_refs:
        if ref.startswith("paper:"):
            lanes.add("paper")
        elif ref.startswith("experiment:"):
            lanes.add("experiment")
        elif ref.startswith("research_artifact:"):
            lanes.add("review")
        elif ref.startswith("task:") or ":" not in ref:
            lanes.add("project")
    return next(iter(lanes)) if len(lanes) == 1 else None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, Mapping) and item.get("type") == "text":
            value = str(item.get("text") or "").strip()
            if value:
                parts.append(value)
    return "\n".join(parts)


def _display_user_message(value: str) -> str:
    marker = "\n\nUser request:\n"
    return value.split(marker, 1)[1].strip() if marker in value else value


def _normalize_context_refs(values: list[str]) -> list[str]:
    if len(values) > MAX_CONTEXT_REFS:
        raise WorkbenchAgentError(
            "invalid_argument",
            f"context_refs may contain at most {MAX_CONTEXT_REFS} items",
        )
    normalized: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or len(item) > 200:
            raise WorkbenchAgentError(
                "invalid_argument",
                "context refs must be non-empty and at most 200 characters",
            )
        if item not in normalized:
            normalized.append(item)
    return normalized


def _normalize_ui_response(
    request: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    method = str(request.get("method") or "")
    response: dict[str, Any] = {
        "type": "extension_ui_response",
        "id": request.get("id"),
    }
    if bool(payload.get("cancelled")):
        response["cancelled"] = True
        return response
    if method == "confirm":
        if not isinstance(payload.get("confirmed"), bool):
            raise WorkbenchAgentError(
                "invalid_argument",
                "confirmed must be a boolean",
            )
        response["confirmed"] = payload["confirmed"]
        return response
    value = payload.get("value")
    if not isinstance(value, str):
        raise WorkbenchAgentError(
            "invalid_argument",
            "value must be a string",
        )
    if len(value) > MAX_PROMPT_CHARS:
        raise WorkbenchAgentError(
            "invalid_argument",
            "UI response value is too large",
        )
    if method == "select" and value not in list(request.get("options") or []):
        raise WorkbenchAgentError(
            "invalid_argument",
            "selected value is not one of the offered options",
        )
    response["value"] = value
    return response


def _public_ui_request(event: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "method",
        "title",
        "message",
        "placeholder",
        "prefill",
        "options",
        "timeout",
    }
    return {
        key: _redact_sensitive(value)
        for key, value in event.items()
        if key in allowed
    }


def _model_view(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "provider": value.get("provider"),
        "id": value.get("id"),
        "name": value.get("name"),
    }


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = re.sub(
                r"(?<!^)(?=[A-Z])",
                "_",
                str(key),
            ).lower().replace("-", "_")
            if (
                any(marker in normalized for marker in SENSITIVE_KEY_MARKERS)
                or normalized == "token"
                or normalized.endswith("_token")
            ):
                result[str(key)] = "<redacted>" if item else item
            else:
                result[str(key)] = _redact_sensitive(item)
        return result
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
