"""Lossless presentation projection of Pi events; never research truth."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any, Iterable, Mapping


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(str(part.get("text") or "") for part in (content or []) if isinstance(part, Mapping) and part.get("type") == "text") if isinstance(content, list) else ""


class Conversation:
    def __init__(self, events: Iterable[Mapping[str, Any]] = (), *, session_id: str = ""):
        self.session_id = str(session_id or "").strip()
        self.items: list[dict[str, Any]] = []
        self.assistant: dict[str, Any] | None = None
        self.tools: dict[str, dict[str, Any]] = {}
        self.sequence = 0
        self.user_count = 0
        for event in events:
            self.append(event)

    def append(self, event: Mapping[str, Any]) -> None:
        self.session_id = str(event.get("session_id") or self.session_id).strip()
        sequence = int(event.get("sequence") or 0)
        if sequence <= self.sequence:
            return
        self.sequence = sequence
        kind = event.get("type")
        if kind == "workbench_user_message":
            self.finish()
            self.user_count += 1
            self.items.append({"id": self._item_id("user", sequence), "sequence": sequence, "kind": "user", "text": str(event.get("message") or ""), "detail": " · ".join(event.get("context_refs") or [])})
        elif kind == "message_update":
            delta = event.get("assistantMessageEvent") or {}
            if isinstance(delta, Mapping) and delta.get("type") == "text_delta" and isinstance(delta.get("delta"), str):
                self._assistant(sequence)["text"] += delta["delta"]
        elif kind == "message_end":
            message = event.get("message") or {}
            if isinstance(message, Mapping) and message.get("role") == "assistant":
                complete = message_text(message.get("content"))
                if complete:
                    self._assistant(sequence)["text"] = complete
                self.finish()
        elif kind == "tool_execution_start":
            self.finish()
            tool_id = str(event.get("toolCallId") or sequence)
            item = {"id": self._item_id("tool", tool_id), "sequence": sequence, "kind": "tool", "text": str(event.get("toolName") or "ScholarFlow tool"), "pending": True, "detail": "调用开始"}
            self.tools[tool_id] = item
            self.items.append(item)
        elif kind == "tool_execution_end":
            item = self.tools.get(str(event.get("toolCallId") or sequence))
            if item:
                item.update(pending=False, detail="调用失败" if event.get("isError") else "调用完成")
        elif kind in {"agent_settled", "workbench_prompt_completed", "workbench_prompt_failed", "workbench_session_stopped", "workbench_session_parked"}:
            self.finish()
            if kind == "workbench_prompt_failed":
                self.items.append({"id": self._item_id("error", sequence), "sequence": sequence, "kind": "error", "text": str(event.get("error") or "Pi 会话失败")})

    def _item_id(self, kind: str, key: Any) -> str:
        prefix = f"{self.session_id}:" if self.session_id else ""
        return f"{prefix}{kind}-{key}"

    def _assistant(self, sequence: int) -> dict[str, Any]:
        if self.assistant is None:
            self.assistant = {"id": self._item_id("assistant", sequence), "sequence": sequence, "kind": "assistant", "text": "", "pending": True}
            self.items.append(self.assistant)
        return self.assistant

    def finish(self) -> None:
        if self.assistant is not None:
            self.assistant["pending"] = False
        self.assistant = None

    def page(self, *, before: int = 0, limit: int = 50) -> dict[str, Any]:
        items = [item for item in self.items if not before or item["sequence"] < before]
        selected = items[-max(1, min(int(limit), 200)):]
        return {"items": deepcopy(selected), "has_more": len(items) > len(selected), "before_sequence": selected[0]["sequence"] if selected else 0, "through_sequence": self.sequence, "total": len(self.items)}

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "user_count": self.user_count,
            "items": deepcopy(self.items),
            "tools": {key: item["id"] for key, item in self.tools.items()},
        }

    @classmethod
    def from_snapshot(cls, value: Any, *, session_id: str = "") -> Conversation | None:
        if not isinstance(value, Mapping) or value.get("schema_version") != 1:
            return None
        stored_session = str(value.get("session_id") or "")
        if session_id and stored_session and stored_session != session_id:
            return None
        raw_items = value.get("items")
        raw_tools = value.get("tools", {})
        if not isinstance(raw_items, list) or not isinstance(raw_tools, Mapping):
            return None
        items: list[dict[str, Any]] = []
        try:
            for raw in raw_items:
                if not isinstance(raw, Mapping):
                    return None
                item_id = str(raw["id"])
                sequence = int(raw["sequence"])
                kind = str(raw["kind"])
                text = raw["text"]
                if not item_id or sequence < 0 or kind not in {"user", "assistant", "tool", "error"} or not isinstance(text, str):
                    return None
                item = {"id": item_id, "sequence": sequence, "kind": kind, "text": text}
                if raw.get("detail") is not None:
                    item["detail"] = str(raw["detail"])
                if "pending" in raw:
                    item["pending"] = bool(raw["pending"])
                items.append(item)
            sequence = int(value.get("sequence", 0))
            user_count = int(value.get("user_count", 0))
            if sequence < 0 or user_count < 0 or (items and sequence < max(item["sequence"] for item in items)):
                return None
            items_by_id = {item["id"]: item for item in items}
            tools = {
                str(key): items_by_id[str(item_id)]
                for key, item_id in raw_tools.items()
                if str(item_id) in items_by_id
            }
        except (KeyError, TypeError, ValueError, StopIteration):
            return None
        conversation = cls(session_id=session_id or stored_session)
        conversation.items = items
        conversation.sequence = sequence
        conversation.user_count = user_count
        conversation.assistant = next(
            (item for item in reversed(items) if item["kind"] == "assistant" and item.get("pending")),
            None,
        )
        conversation.tools = tools
        return conversation

    def handoff(self) -> dict[str, Any]:
        selected = [item for item in self.items if item["kind"] in {"user", "assistant"}][-12:]
        return {"summary": "\n\n".join(f"{item['kind'].title()}: {item['text']}" for item in selected), "message_ids": [item["id"] for item in selected], "through_sequence": self.sequence, "complete_messages": True, "omitted_message_count": sum(item["kind"] in {"user", "assistant"} for item in self.items) - len(selected)}

    def structured_handoff(self) -> dict[str, Any] | None:
        latest = next((item for item in reversed(self.items) if item["kind"] == "assistant" and not item.get("pending")), None)
        if latest is None:
            return None
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", latest["text"].strip())
        try:
            payload = json.loads(value)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or payload.get("schema") != "scholarflow.handoff/v1" or not isinstance(payload.get("summary"), str):
            return None
        fields = ("decisions", "open_questions", "next_actions", "evidence_refs")
        if any(not isinstance(payload.get(key, []), list) or not all(isinstance(item, str) for item in payload.get(key, [])) for key in fields):
            return None
        return {key: payload.get(key, [] if key in fields else "") for key in ("summary", *fields)}
