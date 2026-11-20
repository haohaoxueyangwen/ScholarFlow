"""Supervised, durable Pi JSON-RPC host.

This is a control-plane primitive, not a second agent loop. It launches the
project-selected Pi CLI, sends only explicitly queued RPC envelopes, and
records bounded lifecycle state. The queue is replayable after a child crash;
interactive confirmations are answered negatively by the non-interactive
host so a deployment cannot silently approve governed writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
import uuid

from .event_bus import append_event
from .persistence import atomic_write_json


PI_RPC_STATE_FILENAME = "pi_rpc_state.json"
PI_RPC_QUEUE_FILENAME = "pi_rpc_queue.json"
PI_RPC_MAX_MESSAGE_CHARS = 80_000
PI_RPC_ALLOWED_TYPES = {
    "new_session",
    "prompt",
    "get_state",
    "get_commands",
    "get_session_stats",
    "abort",
}


class PiRpcHostError(RuntimeError):
    """Raised for invalid RPC host configuration or lifecycle failures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PiRpcConfig:
    command: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str] = field(default_factory=dict)
    trust_mode: str = "manual"
    request_timeout_seconds: float = 120.0
    max_queue: int = 20
    max_restarts: int = 3
    restart_window_seconds: int = 3_600
    max_consecutive_failures: int = 3
    circuit_backoff_seconds: int = 300
    stale_after_seconds: int = 180

    def __post_init__(self) -> None:
        if not self.command:
            raise PiRpcHostError("configuration_error", "Pi RPC command is required")
        if self.trust_mode not in {"manual", "approved_readonly"}:
            raise PiRpcHostError("configuration_error", "trust_mode must be manual or approved_readonly")
        if self.request_timeout_seconds <= 0:
            raise PiRpcHostError("configuration_error", "request timeout must be positive")
        if self.max_queue < 1 or self.max_restarts < 0 or self.max_consecutive_failures < 1:
            raise PiRpcHostError("configuration_error", "queue and restart limits are invalid")


class RpcProcess(Protocol):
    pid: int | None

    def start(self) -> None: ...

    def is_alive(self) -> bool: ...

    def request(self, payload: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]: ...

    def close(self) -> None: ...


class SubprocessPiRpcProcess:
    """JSONL Pi RPC process with a reader thread for Windows pipe support."""

    def __init__(self, config: PiRpcConfig) -> None:
        self.config = config
        self.child: subprocess.Popen[str] | None = None
        self.pid: int | None = None
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr = ""
        self._stderr_lock = threading.Lock()

    def start(self) -> None:
        if self.is_alive():
            return
        environment = os.environ.copy()
        environment.update({str(key): str(value) for key, value in self.config.environment.items()})
        self.child = subprocess.Popen(
            list(self.config.command),
            cwd=str(self.config.cwd),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            shell=False,
        )
        self.pid = self.child.pid
        self._reader_thread = threading.Thread(target=self._read_stdout, name="scholarflow-pi-rpc", daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, name="scholarflow-pi-rpc-stderr", daemon=True)
        self._stderr_thread.start()

    def is_alive(self) -> bool:
        return bool(self.child is not None and self.child.poll() is None)

    def request(self, payload: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        return self.request_interactive(
            payload,
            timeout_seconds=timeout_seconds,
            event_sink=None,
            ui_responder=None,
        )

    def request_interactive(
        self,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
        event_sink: Callable[[dict[str, Any]], None] | None,
        ui_responder: Callable[[dict[str, Any], float], dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Send one RPC command while exposing its event stream.

        The ordinary supervisor calls this without callbacks and retains its
        fail-closed behavior. An explicitly interactive host may provide a UI
        responder that waits for a user decision.
        """
        if not self.is_alive() or self.child is None or self.child.stdin is None:
            raise PiRpcHostError("process_unavailable", "Pi RPC process is not running")
        request_id = str(payload.get("id") or f"rpc-{uuid.uuid4().hex}")
        envelope = dict(payload)
        envelope["id"] = request_id
        try:
            self.child.stdin.write(json.dumps(envelope, ensure_ascii=False) + "\n")
            self.child.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise PiRpcHostError("process_unavailable", "Pi RPC stdin is closed") from exc
        deadline = time.monotonic() + float(timeout_seconds)
        response: dict[str, Any] | None = None
        waits_for_settlement = envelope.get("type") == "prompt"
        settled = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PiRpcHostError("timeout", f"Pi RPC request '{request_id}' timed out")
            try:
                event = self._events.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if not self.is_alive():
                    raise PiRpcHostError("process_exited", self.stderr or "Pi RPC process exited")
                continue
            if event.get("type") == "extension_ui_request":
                if event_sink is not None:
                    event_sink(dict(event))
                if _ui_request_expects_response(event):
                    if ui_responder is None:
                        ui_response = _negative_ui_response(event)
                    else:
                        ui_response = ui_responder(dict(event), remaining)
                    self.respond_to_ui(ui_response)
                continue
            if event.get("type") == "response" and str(event.get("id") or "") == request_id:
                response = dict(event)
                if event_sink is not None:
                    event_sink(dict(event))
                if not waits_for_settlement:
                    return response
                if settled:
                    return response
                continue
            if event_sink is not None:
                event_sink(dict(event))
            if waits_for_settlement and event.get("type") == "agent_settled":
                settled = True
                if response is not None:
                    return response

    @property
    def stderr(self) -> str:
        with self._stderr_lock:
            return self._stderr[-20_000:]

    def close(self) -> None:
        child = self.child
        if child is None:
            return
        try:
            if child.stdin is not None:
                child.stdin.close()
        except OSError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        for stream in (child.stdout, child.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self.pid = None

    def _read_stdout(self) -> None:
        child = self.child
        if child is None or child.stdout is None:
            return
        for line in child.stdout:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                self._events.put(payload)

    def _read_stderr(self) -> None:
        child = self.child
        if child is None or child.stderr is None:
            return
        for line in child.stderr:
            with self._stderr_lock:
                self._stderr = (self._stderr + line)[-20_000:]

    def _respond_to_ui(self, event: dict[str, Any]) -> None:
        self.respond_to_ui(_negative_ui_response(event))

    def respond_to_ui(self, response: Mapping[str, Any]) -> None:
        child = self.child
        if child is None or child.stdin is None:
            raise PiRpcHostError("process_unavailable", "Pi RPC stdin is closed")
        normalized = dict(response)
        if normalized.get("type") != "extension_ui_response":
            raise PiRpcHostError("invalid_argument", "invalid extension UI response")
        if not str(normalized.get("id") or "").strip():
            raise PiRpcHostError("invalid_argument", "extension UI response id is required")
        try:
            child.stdin.write(json.dumps(normalized, ensure_ascii=False) + "\n")
            child.stdin.flush()
        except OSError as exc:
            raise PiRpcHostError("process_unavailable", "Pi RPC stdin is closed") from exc

    def _wait_for_settled(self, deadline: float) -> None:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PiRpcHostError("timeout", "Pi prompt did not settle before the request timeout")
            try:
                event = self._events.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if not self.is_alive():
                    raise PiRpcHostError("process_exited", self.stderr or "Pi RPC process exited")
                continue
            if event.get("type") == "extension_ui_request":
                self._respond_to_ui(event)
            elif event.get("type") == "agent_end" or event.get("type") == "agent_settled":
                return


def _ui_request_expects_response(event: Mapping[str, Any]) -> bool:
    return str(event.get("method") or "") in {"select", "confirm", "input", "editor"}


def _negative_ui_response(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "extension_ui_response",
        "id": event.get("id"),
        "cancelled": True,
    }


class PiRpcSupervisor:
    """Durable queue and restart supervisor around one Pi RPC child."""

    def __init__(
        self,
        store_root: Path,
        config: PiRpcConfig,
        *,
        process_factory: Callable[[PiRpcConfig], RpcProcess] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store_root = Path(store_root)
        self.config = config
        self._process_factory = process_factory or SubprocessPiRpcProcess
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._process: RpcProcess | None = None

    def start(self) -> dict[str, Any]:
        state = load_pi_rpc_state(self.store_root)
        state.update(_config_state(self.config))
        if self._process is None or not self._process.is_alive():
            self._process = self._process_factory(self.config)
            self._process.start()
        effective_now = _utc(self._now())
        state.update({
            "status": "running",
            "pid": getattr(self._process, "pid", None),
            "started_at": state.get("started_at") or effective_now.isoformat(),
            "last_heartbeat_at": effective_now.isoformat(),
            "last_error": None,
            "circuit_state": "closed",
            "circuit_open_until": None,
        })
        save_pi_rpc_state(self.store_root, state)
        append_event(self.store_root, event_type="pi.rpc.started", payload=_safe_state_payload(state), now=effective_now)
        return dict(state)

    def stop(self) -> dict[str, Any]:
        if self._process is not None:
            self._process.close()
            self._process = None
        state = load_pi_rpc_state(self.store_root)
        state.update({
            "status": "stopped",
            "pid": None,
            "stopped_at": _utc(self._now()).isoformat(),
            "last_heartbeat_at": state.get("last_heartbeat_at"),
        })
        save_pi_rpc_state(self.store_root, state)
        append_event(self.store_root, event_type="pi.rpc.stopped", payload={}, now=self._now())
        return dict(state)

    def enqueue(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized = _normalize_request(payload)
        items = load_pi_rpc_queue(self.store_root)
        request_id = str(normalized["id"])
        existing = next((item for item in items if item.get("id") == request_id), None)
        if existing is not None:
            return dict(existing)
        if len(items) >= self.config.max_queue:
            raise PiRpcHostError("queue_full", f"Pi RPC queue is full ({self.config.max_queue})")
        items.append(normalized)
        save_pi_rpc_queue(self.store_root, items)
        append_event(
            self.store_root,
            event_type="pi.rpc.queued",
            payload={"request_id": request_id, "rpc_type": normalized["type"], "queue_depth": len(items)},
            now=self._now(),
        )
        return dict(normalized)

    def run_once(self) -> dict[str, Any]:
        effective_now = _utc(self._now())
        state = load_pi_rpc_state(self.store_root)
        if _circuit_is_open(state, effective_now):
            return {"status": "circuit_open", "state": state, "queue_depth": len(load_pi_rpc_queue(self.store_root))}
        if self._process is None or not self._process.is_alive():
            if state.get("status") == "running":
                if not self._record_restart_or_open(state, effective_now):
                    save_pi_rpc_state(self.store_root, state)
                    return {"status": "circuit_open", "state": state, "queue_depth": len(load_pi_rpc_queue(self.store_root))}
            try:
                self.start()
            except Exception as exc:
                self._record_failure(state, effective_now, exc)
                save_pi_rpc_state(self.store_root, state)
                return {"status": "failed", "error": state["last_error"], "state": state, "queue_depth": len(load_pi_rpc_queue(self.store_root))}
            state = load_pi_rpc_state(self.store_root)
        state["last_heartbeat_at"] = effective_now.isoformat()
        items = load_pi_rpc_queue(self.store_root)
        if not items:
            save_pi_rpc_state(self.store_root, state)
            return {"status": "idle", "state": state, "queue_depth": 0}
        request = items[0]
        try:
            if self._process is None:
                raise PiRpcHostError("process_unavailable", "Pi RPC process is not running")
            response = self._process.request(request, timeout_seconds=self.config.request_timeout_seconds)
        except Exception as exc:
            self._record_failure(state, effective_now, exc)
            save_pi_rpc_state(self.store_root, state)
            append_event(
                self.store_root,
                event_type="pi.rpc.request.failed",
                payload={"request_id": request["id"], "error": state["last_error"], "queue_depth": len(items)},
                now=effective_now,
            )
            return {"status": "failed", "request_id": request["id"], "error": state["last_error"], "state": state, "queue_depth": len(items)}
        items = items[1:]
        save_pi_rpc_queue(self.store_root, items)
        state.update({
            "status": "running",
            "last_response_at": effective_now.isoformat(),
            "last_heartbeat_at": effective_now.isoformat(),
            "consecutive_failures": 0,
            "last_error": None,
            "circuit_state": "closed",
            "circuit_open_until": None,
        })
        _resolve_escalation(self.store_root, state, effective_now)
        save_pi_rpc_state(self.store_root, state)
        append_event(
            self.store_root,
            event_type="pi.rpc.request.completed",
            payload={"request_id": request["id"], "rpc_type": request["type"], "queue_depth": len(items)},
            now=effective_now,
        )
        return {"status": "completed", "request_id": request["id"], "response": response, "state": state, "queue_depth": len(items)}

    def run_loop(self, *, max_iterations: int | None = None, sleep_seconds: float = 1.0) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        iteration = 0
        while max_iterations is None or iteration < max_iterations:
            result = self.run_once()
            items.append({"iteration": iteration + 1, "status": result.get("status"), "queue_depth": result.get("queue_depth")})
            iteration += 1
            if max_iterations is None or iteration < max_iterations:
                time.sleep(max(0.0, float(sleep_seconds)))
        return {"status": "loop_finished", "iterations": iteration, "items": items, "state": load_pi_rpc_state(self.store_root)}

    def _record_restart_or_open(self, state: dict[str, Any], now: datetime) -> bool:
        window_started = _parse_time(state.get("restart_window_started_at"))
        if window_started is None or (now - window_started).total_seconds() > self.config.restart_window_seconds:
            state["restart_window_started_at"] = now.isoformat()
            state["restart_count"] = 0
        if int(state.get("restart_count", 0)) >= self.config.max_restarts:
            state["status"] = "circuit_open"
            state["circuit_state"] = "open"
            state["circuit_open_until"] = (now.timestamp() + self.config.circuit_backoff_seconds)
            state["last_error"] = "restart budget exhausted"
            append_event(self.store_root, event_type="pi.rpc.circuit_open", payload={"reason": state["last_error"]}, now=now)
            _open_escalation(self.store_root, state, now)
            return False
        state["restart_count"] = int(state.get("restart_count", 0)) + 1
        state["status"] = "restarting"
        return True

    def _record_failure(self, state: dict[str, Any], now: datetime, error: Exception) -> None:
        state["status"] = "degraded"
        state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
        state["last_error"] = f"{type(error).__name__}: {error}"[:1_000]
        if state["consecutive_failures"] >= self.config.max_consecutive_failures:
            state["circuit_state"] = "open"
            state["status"] = "circuit_open"
            state["circuit_open_until"] = now.timestamp() + self.config.circuit_backoff_seconds
            _open_escalation(self.store_root, state, now)


def load_pi_rpc_state(store_root: Path) -> dict[str, Any]:
    path = Path(store_root) / PI_RPC_STATE_FILENAME
    defaults = _default_state()
    if not path.exists():
        return defaults
    payload = json.loads(path.read_text(encoding="utf-8"))
    defaults.update(payload)
    return defaults


def save_pi_rpc_state(store_root: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_json(Path(store_root) / PI_RPC_STATE_FILENAME, dict(payload))


def load_pi_rpc_queue(store_root: Path) -> list[dict[str, Any]]:
    path = Path(store_root) / PI_RPC_QUEUE_FILENAME
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [dict(item) for item in payload.get("items", []) if isinstance(item, dict)]


def save_pi_rpc_queue(store_root: Path, items: Sequence[Mapping[str, Any]]) -> None:
    atomic_write_json(Path(store_root) / PI_RPC_QUEUE_FILENAME, {"version": 1, "items": [dict(item) for item in items]})


def pi_rpc_status(store_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    state = load_pi_rpc_state(store_root)
    queue_depth = len(load_pi_rpc_queue(store_root))
    effective_now = _utc(now or datetime.now(timezone.utc))
    heartbeat = _parse_time(state.get("last_heartbeat_at"))
    stale = bool(
        state.get("status") == "running"
        and heartbeat is not None
        and (effective_now - heartbeat).total_seconds() > int(state.get("stale_after_seconds", 180))
    )
    status = "stale" if stale else str(state.get("status") or "unconfigured")
    return {
        "status": status,
        "configured": bool(state.get("configured")),
        "pid": state.get("pid"),
        "queue_depth": queue_depth,
        "last_heartbeat_at": state.get("last_heartbeat_at"),
        "last_response_at": state.get("last_response_at"),
        "restart_count": state.get("restart_count", 0),
        "consecutive_failures": state.get("consecutive_failures", 0),
        "circuit_state": state.get("circuit_state", "closed"),
        "last_error": state.get("last_error"),
        "trust_mode": state.get("trust_mode"),
        "escalation": state.get("escalation"),
    }


def acknowledge_pi_rpc_escalation(
    store_root: Path,
    *,
    escalation_id: str,
    confirmed: bool,
    confirmed_by: str = "user",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Acknowledge one active escalation without silently resetting failures."""

    if not confirmed:
        raise PiRpcHostError("confirmation_required", "explicit confirmation is required")
    state = load_pi_rpc_state(store_root)
    escalation = state.get("escalation")
    normalized_id = str(escalation_id or "").strip()
    if not isinstance(escalation, dict) or escalation.get("escalation_id") != normalized_id:
        raise PiRpcHostError("not_found", "Pi RPC escalation was not found")
    if escalation.get("status") == "resolved":
        return dict(escalation)
    if escalation.get("status") == "acknowledged":
        return dict(escalation)
    effective_now = _utc(now or datetime.now(timezone.utc))
    escalation.update(
        {
            "status": "acknowledged",
            "acknowledged_at": effective_now.isoformat(),
            "acknowledged_by": str(confirmed_by or "user").strip() or "user",
        }
    )
    state["escalation"] = escalation
    save_pi_rpc_state(store_root, state)
    append_event(
        store_root,
        event_type="pi.rpc.escalation.acknowledged",
        payload={
            "escalation_id": normalized_id,
            "acknowledged_by": escalation["acknowledged_by"],
        },
        now=effective_now,
    )
    return dict(escalation)


def _normalize_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    rpc_type = str(normalized.get("type") or "").strip()
    if rpc_type not in PI_RPC_ALLOWED_TYPES:
        raise PiRpcHostError("invalid_argument", f"unsupported Pi RPC type '{rpc_type}'")
    normalized["type"] = rpc_type
    normalized["id"] = str(normalized.get("id") or f"rpc-{uuid.uuid4().hex}")
    if len(json.dumps(normalized, ensure_ascii=False)) > PI_RPC_MAX_MESSAGE_CHARS:
        raise PiRpcHostError("invalid_argument", "Pi RPC message is too large")
    return normalized


def _config_state(config: PiRpcConfig) -> dict[str, Any]:
    command_digest = hashlib.sha256("\0".join(config.command).encode("utf-8")).hexdigest()[:16]
    return {
        "configured": True,
        "command_digest": command_digest,
        "trust_mode": config.trust_mode,
        "max_queue": config.max_queue,
        "max_restarts": config.max_restarts,
        "max_consecutive_failures": config.max_consecutive_failures,
        "stale_after_seconds": config.stale_after_seconds,
    }


def _default_state() -> dict[str, Any]:
    return {
        "status": "unconfigured",
        "configured": False,
        "pid": None,
        "command_digest": None,
        "trust_mode": None,
        "started_at": None,
        "stopped_at": None,
        "last_heartbeat_at": None,
        "last_response_at": None,
        "restart_window_started_at": None,
        "restart_count": 0,
        "consecutive_failures": 0,
        "circuit_state": "closed",
        "circuit_open_until": None,
        "last_error": None,
        "escalation": None,
        "max_queue": 20,
        "max_restarts": 3,
        "max_consecutive_failures": 3,
        "stale_after_seconds": 180,
    }


def _circuit_is_open(state: Mapping[str, Any], now: datetime) -> bool:
    if state.get("circuit_state") != "open":
        return False
    until = state.get("circuit_open_until")
    try:
        return until is not None and now.timestamp() < float(until)
    except (TypeError, ValueError):
        return False


def _safe_state_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": state.get("status"),
        "pid": state.get("pid"),
        "trust_mode": state.get("trust_mode"),
        "configured": bool(state.get("configured")),
        "restart_count": state.get("restart_count", 0),
    }


def _open_escalation(store_root: Path, state: dict[str, Any], now: datetime) -> None:
    existing = state.get("escalation")
    if isinstance(existing, dict) and existing.get("status") in {"open", "acknowledged"}:
        return
    escalation = {
        "escalation_id": f"pi-rpc-{uuid.uuid4().hex}",
        "status": "open",
        "channel": "workbench_runtime_health",
        "opened_at": now.isoformat(),
        "acknowledged_at": None,
        "acknowledged_by": None,
        "resolved_at": None,
        "failure_count": int(state.get("consecutive_failures", 0)),
        "reason": str(state.get("last_error") or "Pi RPC circuit opened")[:1_000],
        "reset_semantics": "Acknowledgement records human awareness; only a successful queued request resolves the escalation and resets the failure streak.",
    }
    state["escalation"] = escalation
    append_event(
        store_root,
        event_type="pi.rpc.escalation.opened",
        payload={
            "escalation_id": escalation["escalation_id"],
            "channel": escalation["channel"],
            "failure_count": escalation["failure_count"],
            "reason": escalation["reason"],
        },
        now=now,
    )


def _resolve_escalation(store_root: Path, state: dict[str, Any], now: datetime) -> None:
    escalation = state.get("escalation")
    if not isinstance(escalation, dict) or escalation.get("status") not in {"open", "acknowledged"}:
        return
    escalation.update({"status": "resolved", "resolved_at": now.isoformat()})
    state["escalation"] = escalation
    append_event(
        store_root,
        event_type="pi.rpc.escalation.resolved",
        payload={"escalation_id": escalation["escalation_id"]},
        now=now,
    )


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
