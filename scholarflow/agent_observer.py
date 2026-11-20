"""Deterministic, provider-neutral observation of one Pi Agent turn.

The observer is deliberately outside Pi's reasoning loop.  It consumes the
durable Workbench event stream plus bounded state fingerprints and produces an
auditable record of outcome, trajectory, final-state evidence, and cost.  It
never uses hidden model reasoning as evidence and never stores the final answer
or tool payloads verbatim.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


AGENT_OBSERVATION_SCHEMA_VERSION = 1
STATE_SNAPSHOT_SCHEMA_VERSION = 1
MAX_HASH_FILE_BYTES = 8 * 1024 * 1024
MAX_HASH_TOTAL_BYTES = 32 * 1024 * 1024

_EXPECTED_WORKFLOW_STATE_TOOLS = {
    "sf_intake_create",
    "sf_intake_anchor",
    "sf_intake_prepare",
    "sf_intake_propose",
}
_CONFIRMED_WRITE_TOOLS = {"sf_inbox_ack"}
_EXTERNAL_WRITE_TOOLS = {"sf_kb_apply_write", "sf_zotero_tag_apply"}
_RESEARCH_WORKER_ALLOWED_TOOLS = {
    "sf_research_search",
    "sf_knowledge_health",
    "sf_knowledge_search",
    "sf_knowledge_sources",
    "web_search",
    "source_check",
    "fetch_content",
    "get_search_content",
}

_TRACKED_STATE_FILES = {
    "experiments.json": "typed_truth",
    "tasks.json": "typed_truth",
    "project_stages.json": "typed_truth",
    "papers.json": "typed_truth",
    "research_artifacts.json": "typed_truth",
    "knowledge_relations.json": "typed_truth",
    "intake_items.json": "typed_truth",
    "pi_mutations.json": "governance",
    "_knowledge/candidates.json": "governance",
    "_knowledge/source-map.json": "knowledge_index",
    "_knowledge/ingestion-manifest.json": "knowledge_index",
    "_manuscripts/catalog.json": "manuscript",
    "gateway_inbox.json": "gateway",
}

_STATUS_RE = re.compile(
    r"(?:\bstatus\b|状态)\s*[\"'`*]*\s*[:：=]\s*[\"'`*]*"
    r"([a-zA-Z][a-zA-Z0-9_-]{1,40}|[\u4e00-\u9fff]{1,12})",
    flags=re.IGNORECASE,
)
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", flags=re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>()\]\[\"']+", flags=re.IGNORECASE)
_REFERENCE_KEY_RE = re.compile(
    r"(?:doi|url|citation|evidence_ref|source_ref|document_ref|paper_id)$",
    flags=re.IGNORECASE,
)
_PROJECT_KEY_RE = re.compile(
    r"(?:project_id|project_stage_id|stage_id)$",
    flags=re.IGNORECASE,
)


def capture_authoritative_state(root_dir: Path) -> dict[str, Any]:
    """Capture bounded fingerprints of formal local state, never its content."""

    root = Path(root_dir)
    files: dict[str, dict[str, Any]] = {}
    hashed_bytes = 0
    for relative, category in _TRACKED_STATE_FILES.items():
        path = root / Path(relative)
        record: dict[str, Any] = {
            "category": category,
            "exists": False,
        }
        try:
            record["exists"] = path.is_file()
            if record["exists"]:
                stat = path.stat()
                record.update(
                    {
                        "size_bytes": stat.st_size,
                        "modified_ns": stat.st_mtime_ns,
                    }
                )
                if (
                    stat.st_size <= MAX_HASH_FILE_BYTES
                    and hashed_bytes + stat.st_size <= MAX_HASH_TOTAL_BYTES
                ):
                    remaining_budget = min(
                        MAX_HASH_FILE_BYTES,
                        MAX_HASH_TOTAL_BYTES - hashed_bytes,
                    )
                    with path.open("rb") as handle:
                        content = handle.read(remaining_budget + 1)
                    if len(content) <= remaining_budget:
                        hashed_bytes += len(content)
                        record["size_bytes"] = len(content)
                        record["sha256"] = hashlib.sha256(content).hexdigest()
                        _add_json_metadata(record, content)
                        record["measurement"] = "sha256"
                    else:
                        record["measurement"] = "size_and_mtime"
                        record["measurement_limit"] = "changed_during_capture"
                else:
                    record["measurement"] = "size_and_mtime"
                    record["measurement_limit"] = (
                        "per_file" if stat.st_size > MAX_HASH_FILE_BYTES else "total_snapshot"
                    )
        except OSError as exc:
            record["measurement"] = "unavailable"
            record["error"] = type(exc).__name__
        files[relative] = record
    stable = {
        name: {
            "category": record.get("category"),
            "identity": _file_identity(record),
            "revision": record.get("revision"),
            "object_counts": record.get("object_counts", {}),
        }
        for name, record in files.items()
    }
    return {
        "schema_version": STATE_SNAPSHOT_SCHEMA_VERSION,
        "captured_at": _now(),
        "fingerprint": _sha256_json(stable),
        "files": files,
    }


def observe_agent_turn(
    *,
    session_id: str,
    request_id: str,
    events: Iterable[Mapping[str, Any]],
    context_manifest: Mapping[str, Any] | None = None,
    before_state: Mapping[str, Any] | None = None,
    after_state: Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build one privacy-bounded observation from a completed or failed turn."""

    normalized_session = str(session_id or "").strip()
    normalized_request = str(request_id or "").strip()
    turn_events = _turn_events(events, normalized_request)
    manifest = dict(context_manifest or {})
    final_answer = _final_answer(turn_events)
    calls = _tool_calls(turn_events)
    state = _state_transition(before_state, after_state, calls)
    usage = _usage(turn_events, manifest, final_answer, calls)
    trajectory = _trajectory(turn_events, calls)
    citations = _citation_evidence(calls, final_answer)
    signals = _signals(
        turn_events,
        manifest,
        final_answer,
        calls,
        trajectory,
        citations,
        state,
    )
    severity = {item["severity"] for item in signals}
    status = "fail" if "error" in severity else "warn" if "warning" in severity else "pass"
    completion = _completion(turn_events)
    observation_id = "observation-" + hashlib.sha256(
        f"{normalized_session}:{normalized_request}".encode("utf-8")
    ).hexdigest()[:32]
    return {
        "schema_version": AGENT_OBSERVATION_SCHEMA_VERSION,
        "observation_id": observation_id,
        "session_id": normalized_session,
        "request_id": normalized_request,
        "created_at": created_at or _now(),
        "status": status,
        "outcome": {
            "completion": completion,
            "final_answer": {
                "present": bool(final_answer.strip()),
                "character_count": len(final_answer),
                "estimated_tokens": _estimate_tokens(final_answer),
                "sha256": hashlib.sha256(final_answer.encode("utf-8")).hexdigest()
                if final_answer
                else None,
                "content_persisted": False,
            },
            "citation_evidence": citations,
            "final_state": state,
        },
        "trajectory": trajectory,
        "cost": usage,
        "signals": signals,
        "measurement_notes": [
            "Final answer and tool payload contents are fingerprinted, not persisted.",
            "Hidden reasoning is excluded from all scores.",
            "External effects such as Zotero, Obsidian, or remote indexes require connector-specific receipts.",
        ],
    }


def aggregate_observations(observations: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize repeated nondeterministic runs as a distribution."""

    items = [dict(item) for item in observations]
    count = len(items)
    successful = sum(
        item.get("outcome", {}).get("completion") == "completed"
        and item.get("status") != "fail"
        for item in items
    )
    clean = sum(item.get("status") == "pass" for item in items)
    latencies = sorted(
        float(value)
        for item in items
        if (value := item.get("cost", {}).get("latency_ms")) is not None
    )
    signal_counts = Counter(
        str(signal.get("code"))
        for item in items
        for signal in item.get("signals", [])
        if signal.get("code")
    )
    interval = _wilson_interval(successful, count)
    return {
        "schema_version": 1,
        "attempts": count,
        "status_counts": dict(Counter(str(item.get("status") or "unknown") for item in items)),
        "successful_completion_count": successful,
        "successful_completion_rate": round(successful / count, 4) if count else 0.0,
        "successful_completion_wilson_95": interval,
        "clean_pass_count": clean,
        "clean_pass_rate": round(clean / count, 4) if count else 0.0,
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 3) if latencies else None,
        },
        "reported_tokens": {
            "input": _sum_cost(items, "input_tokens"),
            "output": _sum_cost(items, "output_tokens"),
            "cache_read": _sum_cost(items, "cache_read_tokens"),
            "reasoning": _sum_cost(items, "reasoning_tokens"),
            "total": _sum_cost(items, "total_tokens"),
        },
        "reported_cost_total": round(
            sum(float(item.get("cost", {}).get("reported_cost_total") or 0) for item in items),
            9,
        ),
        "signal_counts": dict(sorted(signal_counts.items())),
    }


def _add_json_metadata(record: dict[str, Any], content: bytes) -> None:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(payload, Mapping):
        return
    if isinstance(payload.get("revision"), (int, float)):
        record["revision"] = int(payload["revision"])
    for key in ("items", "proposals", "commits", "candidates", "sources"):
        value = payload.get(key)
        if isinstance(value, (Mapping, list)):
            record.setdefault("object_counts", {})[key] = len(value)


def _turn_events(events: Iterable[Mapping[str, Any]], request_id: str) -> list[dict[str, Any]]:
    candidates = [dict(item) for item in events if isinstance(item, Mapping)]
    selected = [
        item
        for item in candidates
        if str(item.get("request_id") or "") == request_id
        or str(item.get("prompt_request_id") or "") == request_id
    ]
    if not selected:
        selected = candidates
    return sorted(selected, key=lambda item: int(item.get("sequence", 0) or 0))


def _completion(events: list[dict[str, Any]]) -> str:
    types = {str(item.get("type") or "") for item in events}
    if "workbench_prompt_failed" in types:
        return "failed"
    if "workbench_prompt_completed" in types or "agent_settled" in types:
        return "completed"
    return "unfinished"


def _final_answer(events: list[dict[str, Any]]) -> str:
    ended: list[str] = []
    for event in events:
        if event.get("type") != "message_end":
            continue
        message = event.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        text = _content_text(message.get("content"))
        if text:
            ended.append(text)
    if ended:
        return ended[-1]
    deltas: list[str] = []
    for event in events:
        if event.get("type") != "message_update":
            continue
        update = event.get("assistantMessageEvent")
        if isinstance(update, Mapping) and update.get("type") == "text_delta":
            deltas.append(str(update.get("delta") or ""))
    return "".join(deltas)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, Mapping) and item.get("type") == "text" and item.get("text")
    )


def _tool_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    endings: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("type") == "tool_execution_end":
            endings[str(event.get("toolCallId") or "")] = dict(event)
        if event.get("type") == "turn_end":
            for result in event.get("toolResults") or []:
                if isinstance(result, Mapping):
                    call_id = str(result.get("toolCallId") or "")
                    endings.setdefault(
                        call_id,
                        {**dict(result), "recorded_at": event.get("recorded_at"), "sequence": event.get("sequence")},
                    )
    calls: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "tool_execution_start":
            continue
        call_id = str(event.get("toolCallId") or "")
        ending = endings.get(call_id)
        result = ending.get("result") if ending else None
        if result is None and ending:
            result = ending
        result_text = _jsonish_text(result)
        expected_empty_memory = (
            event.get("toolName") == "memory_search"
            and "No memories in extended store yet" in result_text
        )
        is_error = bool(ending.get("isError")) if ending else False
        if expected_empty_memory:
            is_error = False
        args = dict(event.get("args") or {})
        signature = _sha256_json({"name": event.get("toolName"), "args": args})
        calls.append(
            {
                "id": call_id,
                "name": str(event.get("toolName") or "unknown"),
                "args_signature": signature,
                "status": "error" if is_error else "success" if ending else "unfinished",
                "is_error": is_error,
                "start_sequence": int(event.get("sequence", 0) or 0),
                "end_sequence": int(ending.get("sequence", 0) or 0) if ending else None,
                "started_at": event.get("recorded_at"),
                "ended_at": ending.get("recorded_at") if ending else None,
                "latency_ms": _duration_ms(event.get("recorded_at"), ending.get("recorded_at") if ending else None),
                "result_character_count": len(result_text),
                "result_sha256": hashlib.sha256(result_text.encode("utf-8")).hexdigest()
                if result_text
                else None,
                "result_data": _parse_jsonish(result),
                "args": args,
            }
        )
    return calls


def _trajectory(events: list[dict[str, Any]], calls: list[dict[str, Any]]) -> dict[str, Any]:
    signatures = Counter((item["name"], item["args_signature"]) for item in calls)
    repeated = [
        {"tool": name, "args_signature": signature, "count": count}
        for (name, signature), count in sorted(signatures.items())
        if count > 1
    ]
    lanes = [
        str(item.get("args", {}).get("lane") or "")
        for item in calls
        if item["name"] == "sf_lane_enter" and item.get("args", {}).get("lane")
    ]
    responses = [
        item
        for item in events
        if item.get("type") == "workbench_ui_response"
        and item.get("method") == "confirm"
    ]
    apply_calls = [item for item in calls if _is_apply_tool(item["name"])]
    approved_response_ids = {
        str(item.get("request_id") or "")
        for item in responses
        if item.get("confirmed") is True
    }
    approval_sequences = [
        int(item.get("sequence", 0) or 0)
        for item in events
        if item.get("type") == "extension_ui_request"
        and item.get("method") == "confirm"
        and str(item.get("id") or "") in approved_response_ids
    ]
    confirmed_apply_count = sum(
        any(
            int(call.get("start_sequence") or 0) <= sequence
            <= int(call.get("end_sequence") or 2**63 - 1)
            for sequence in approval_sequences
        )
        for call in apply_calls
    )
    retrieval_calls = [item for item in calls if _is_retrieval_tool(item["name"])]
    delegated = _delegated_worker_metrics(calls)
    return {
        "tool_call_count": len(calls),
        "unique_tools": sorted({item["name"] for item in calls}),
        "tool_error_count": sum(item["is_error"] for item in calls),
        "unfinished_tool_count": sum(item["status"] == "unfinished" for item in calls),
        "tool_calls": [
            {key: value for key, value in item.items() if key not in {"args", "result_data"}}
            for item in calls
        ],
        "repeated_calls": repeated,
        "lane_entries": lanes,
        "confirmation_requests": sum(
            item.get("type") == "extension_ui_request" and item.get("method") == "confirm"
            for item in events
        ),
        "confirmation_approved": sum(item.get("confirmed") is True for item in responses),
        "confirmation_denied": sum(item.get("confirmed") is False or item.get("cancelled") is True for item in responses),
        "apply_call_count": len(apply_calls),
        "successful_apply_count": sum(item["status"] == "success" for item in apply_calls),
        "confirmed_apply_count": confirmed_apply_count,
        "retrieval_call_count": len(retrieval_calls),
        "retrieved_item_count": sum(_result_count(item.get("result_data")) for item in retrieval_calls),
        "compaction_count": sum(item.get("type") == "compaction_end" for item in events),
        "delegated_worker_count": delegated["worker_count"],
        "delegated_worker_success_count": delegated["successful_worker_count"],
        "delegated_worker_failure_count": delegated["failed_worker_count"],
        "delegated_unique_source_count": delegated["unique_source_count"],
        "delegated_worker_forbidden_tools": delegated["forbidden_tools"],
    }


def _state_transition(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    calls: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return {
            "status": "not_measured",
            "before_fingerprint": before.get("fingerprint") if isinstance(before, Mapping) else None,
            "after_fingerprint": after.get("fingerprint") if isinstance(after, Mapping) else None,
            "changed_files": [],
            "external_effects": "require_connector_receipts",
        }
    before_files = dict(before.get("files") or {})
    after_files = dict(after.get("files") or {})
    changed = []
    changed_details = []
    categories = Counter()
    for name in sorted(set(before_files) | set(after_files)):
        left = before_files.get(name, {})
        right = after_files.get(name, {})
        left_identity = _file_identity(left)
        right_identity = _file_identity(right)
        if left_identity == right_identity:
            continue
        category = str(right.get("category") or left.get("category") or "unknown")
        changed.append(name)
        categories[category] += 1
        changed_details.append(
            {
                "path": name,
                "category": category,
                "verification": "content_hash"
                if _content_identity_available(left, right)
                else "metadata_only",
            }
        )
    apply_calls = [item for item in calls if _is_apply_tool(item["name"]) and item["status"] == "success"]
    external_write_tools = sorted(
        item["name"] for item in apply_calls if _is_external_write_tool(item["name"])
    )
    proposal_calls = [
        item for item in calls if _is_proposal_tool(item["name"]) and item["status"] == "success"
    ]
    workflow_calls = [
        item for item in calls if _is_workflow_state_tool(item["name"]) and item["status"] == "success"
    ]
    changed_category_set = set(categories)
    fully_content_verified = all(
        item["verification"] == "content_hash" for item in changed_details
    )
    if not changed:
        status = "reported_only" if apply_calls else "unchanged"
    elif apply_calls:
        status = (
            "locally_verified_change"
            if fully_content_verified
            else "locally_observed_change"
        )
    elif proposal_calls and changed_category_set <= (
        {"governance", "typed_truth"}
        if any(_is_workflow_state_tool(item["name"]) for item in proposal_calls)
        else {"governance"}
    ):
        # Intake proposal emission also advances the typed intake workflow.
        status = "proposal_recorded"
    elif workflow_calls and changed_category_set <= {"typed_truth"}:
        status = "workflow_state_recorded"
    else:
        status = "concurrent_or_unexpected_change"
    return {
        "status": status,
        "before_fingerprint": before.get("fingerprint"),
        "after_fingerprint": after.get("fingerprint"),
        "changed_files": changed,
        "changed_file_details": changed_details,
        "changed_categories": dict(sorted(categories.items())),
        "content_verified_change_count": sum(
            item["verification"] == "content_hash" for item in changed_details
        ),
        "metadata_only_change_count": sum(
            item["verification"] == "metadata_only" for item in changed_details
        ),
        "external_write_tools": external_write_tools,
        "external_effects": "unverified" if external_write_tools else "not_applicable",
    }


def _citation_evidence(calls: list[dict[str, Any]], final_answer: str) -> dict[str, Any]:
    returned: set[str] = set()
    for call in calls:
        if call["status"] != "success" or not _is_citation_retrieval_tool(call["name"]):
            continue
        returned.update(_reference_values(call.get("result_data")))
    cited = {item for item in returned if item.casefold() in final_answer.casefold()}
    return {
        "returned_reference_count": len(returned),
        "cited_reference_count": len(cited),
        "missing_reference_count": len(returned - cited),
        "returned_reference_hashes": sorted(
            hashlib.sha256(item.encode("utf-8")).hexdigest() for item in returned
        )[:100],
        "cited_reference_hashes": sorted(
            hashlib.sha256(item.encode("utf-8")).hexdigest() for item in cited
        )[:100],
    }


def _usage(
    events: list[dict[str, Any]],
    manifest: Mapping[str, Any],
    final_answer: str,
    calls: list[dict[str, Any]],
) -> dict[str, Any]:
    totals = Counter()
    reported_cost = 0.0
    providers: set[str] = set()
    models: set[str] = set()
    for event in events:
        if event.get("type") != "message_end":
            continue
        message = event.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, Mapping):
            continue
        providers.add(str(message.get("provider") or "unknown"))
        models.add(str(message.get("model") or "unknown"))
        for source, target in (
            ("input", "input_tokens"),
            ("output", "output_tokens"),
            ("cacheRead", "cache_read_tokens"),
            ("cacheWrite", "cache_write_tokens"),
            ("reasoning", "reasoning_tokens"),
            ("totalTokens", "total_tokens"),
        ):
            value = usage.get(source)
            if isinstance(value, (int, float)):
                totals[target] += value
        cost = usage.get("cost")
        if isinstance(cost, Mapping) and isinstance(cost.get("total"), (int, float)):
            reported_cost += float(cost["total"])
    delegated = _delegated_worker_metrics(calls)
    delegated_usage = delegated["usage"]
    for target, source in (
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("cache_read_tokens", "cacheRead"),
        ("cache_write_tokens", "cacheWrite"),
        ("reasoning_tokens", "reasoning"),
        ("total_tokens", "totalTokens"),
    ):
        totals[target] += int(delegated_usage.get(source) or 0)
    reported_cost += float(delegated_usage.get("cost") or 0)
    providers.update(delegated["providers"])
    models.update(delegated["models"])
    started = next((item.get("recorded_at") for item in events if item.get("type") == "workbench_user_message"), None)
    ended = next(
        (item.get("recorded_at") for item in reversed(events) if item.get("type") in {"workbench_prompt_completed", "workbench_prompt_failed"}),
        None,
    )
    budget = dict(manifest.get("budget") or {})
    index_calls = [
        item for item in calls
        if re.search(r"knowledge|kb|index|ingest|sync", item["name"], flags=re.IGNORECASE)
        and (_is_apply_tool(item["name"]) or "sync" in item["name"] or "ingest" in item["name"])
    ]
    return {
        "latency_ms": _duration_ms(started, ended),
        "estimated_new_prompt_tokens": budget.get("estimated_new_prompt_tokens"),
        "estimated_response_tokens": _estimate_tokens(final_answer),
        "input_tokens": int(totals["input_tokens"]),
        "output_tokens": int(totals["output_tokens"]),
        "cache_read_tokens": int(totals["cache_read_tokens"]),
        "cache_write_tokens": int(totals["cache_write_tokens"]),
        "reasoning_tokens": int(totals["reasoning_tokens"]),
        "total_tokens": int(totals["total_tokens"]),
        "reported_cost_total": round(reported_cost, 9),
        "reported_cost_currency": "provider_native_unspecified" if reported_cost else None,
        "providers": sorted(providers),
        "models": sorted(models),
        "delegated_workers": {
            "worker_count": delegated["worker_count"],
            "successful_worker_count": delegated["successful_worker_count"],
            "failed_worker_count": delegated["failed_worker_count"],
            "unique_source_count": delegated["unique_source_count"],
            "usage": delegated_usage,
        },
        "retrieval_volume": {
            "calls": sum(_is_retrieval_tool(item["name"]) for item in calls),
            "items": sum(_result_count(item.get("result_data")) for item in calls if _is_retrieval_tool(item["name"])),
        },
        "index_build": {
            "call_count": len(index_calls),
            "tool_latency_ms": round(sum(float(item.get("latency_ms") or 0) for item in index_calls), 3),
            "external_compute_cost": "not_reported_by_connector",
        },
    }


def _signals(
    events: list[dict[str, Any]],
    manifest: Mapping[str, Any],
    final_answer: str,
    calls: list[dict[str, Any]],
    trajectory: Mapping[str, Any],
    citations: Mapping[str, Any],
    state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []

    def add(code: str, severity: str, message: str, evidence: Mapping[str, Any] | None = None) -> None:
        signals.append(
            {
                "code": code,
                "severity": severity,
                "message": message,
                "evidence": dict(evidence or {}),
            }
        )

    completion = _completion(events)
    if completion == "failed":
        add("turn_failed", "error", "The Pi turn ended in a Workbench failure.")
    elif completion == "unfinished":
        add("turn_unfinished", "error", "No settled or terminal Workbench event was observed.")
    if completion == "completed" and not final_answer.strip():
        add("missing_final_answer", "warning", "The turn completed without a visible final answer.")
    failed_tools = [item["name"] for item in calls if item["is_error"]]
    if failed_tools:
        add("tool_error", "error", "One or more tool calls failed.", {"tools": failed_tools})
    unfinished_tools = [item["name"] for item in calls if item["status"] == "unfinished"]
    if unfinished_tools:
        add("unfinished_tool_trace", "warning", "Tool start events lack matching terminal events.", {"tools": unfinished_tools})
    if trajectory.get("repeated_calls"):
        add(
            "repeated_work",
            "warning",
            "The turn repeated an identical tool call.",
            {"calls": trajectory["repeated_calls"]},
        )
    if trajectory.get("delegated_worker_failure_count"):
        add(
            "research_worker_partial_failure",
            "warning",
            "One or more bounded research workers failed or returned an invalid evidence bundle.",
            {
                "worker_count": trajectory.get("delegated_worker_count", 0),
                "failed_worker_count": trajectory.get("delegated_worker_failure_count", 0),
            },
        )
    if trajectory.get("delegated_worker_forbidden_tools"):
        add(
            "research_worker_boundary_violation",
            "error",
            "A research worker trace reported a tool outside the read-only evidence boundary.",
            {"tools": trajectory["delegated_worker_forbidden_tools"]},
        )
    workload = str(dict(manifest.get("route") or {}).get("workload") or "")
    max_calls = {
        "factual_query": 3,
        "sequential_decision": 12,
        "project_review": 16,
        "cross_project_synthesis": 20,
        "open_research": 20,
    }.get(workload)
    if max_calls is not None and len(calls) > max_calls:
        add(
            "excessive_tooling",
            "warning",
            "Tool count exceeded the route-specific advisory budget.",
            {"workload": workload, "observed": len(calls), "budget": max_calls},
        )
    lanes = [item for item in trajectory.get("lane_entries", []) if item]
    expected_lane = _manifest_lane(manifest)
    if len(set(lanes)) > 1 or expected_lane and lanes and any(item != expected_lane for item in lanes):
        add(
            "goal_drift",
            "warning",
            "The tool lane diverged from the selected object context.",
            {"expected_lane": expected_lane, "observed_lanes": lanes},
        )
    apply_calls = [item for item in calls if _is_apply_tool(item["name"])]
    if apply_calls and int(trajectory.get("confirmed_apply_count") or 0) < len(apply_calls):
        add(
            "unauthorized_apply_attempt",
            "error",
            "An apply tool was attempted without a recorded interactive approval.",
            {"tools": [item["name"] for item in apply_calls]},
        )
    if state.get("status") == "concurrent_or_unexpected_change":
        add(
            "unexpected_state_change",
            "warning",
            "Formal local state changed without a matching successful state-changing tool in this turn.",
            {"changed_files": state.get("changed_files", [])},
        )
    confirmed_successful_apply_count = min(
        int(trajectory.get("confirmed_apply_count") or 0),
        int(trajectory.get("successful_apply_count") or 0),
    )
    external_write_tools = list(state.get("external_write_tools") or [])
    if confirmed_successful_apply_count and (
        state.get("status") in {"reported_only", "not_measured"} or external_write_tools
    ):
        add(
            "final_state_unverified",
            "warning",
            "A confirmed apply completed, but the observer lacks a connector receipt or complete final-state evidence.",
            {
                "final_state_status": state.get("status"),
                "external_write_tools": external_write_tools,
            },
        )
    if state.get("status") == "locally_observed_change":
        add(
            "final_state_partially_verified",
            "warning",
            "A confirmed apply changed formal state, but at least one changed file exceeded the content-hash budget.",
            {
                "changed_files": state.get("changed_files", []),
                "metadata_only_change_count": state.get("metadata_only_change_count", 0),
            },
        )
    if citations.get("returned_reference_count") and not citations.get("cited_reference_count") and final_answer.strip():
        add(
            "citation_loss",
            "warning",
            "Retrieval returned citable references but none survived into the final answer.",
            {"returned_reference_count": citations.get("returned_reference_count")},
        )
    project_id = str(manifest.get("project_id") or "")
    if project_id and workload != "cross_project_synthesis":
        observed_projects = {
            value
            for call in calls
            if _is_retrieval_tool(call["name"])
            for value in _project_values(call.get("result_data"))
        }
        foreign = sorted(value for value in observed_projects if value != project_id)
        if foreign:
            add(
                "retrieval_contamination",
                "warning",
                "Project-scoped retrieval returned records from another project.",
                {
                    "expected_project_id_hash": hashlib.sha256(
                        project_id.encode("utf-8")
                    ).hexdigest(),
                    "foreign_project_id_hashes": [
                        hashlib.sha256(item.encode("utf-8")).hexdigest()
                        for item in foreign[:20]
                    ],
                },
            )
    checkpoint_count = sum(
        source.get("source_type") == "checkpoint"
        for source in manifest.get("sources") or []
        if isinstance(source, Mapping)
    )
    typed_reads = [item for item in calls if _is_typed_truth_read(item["name"])]
    if checkpoint_count and workload in {"factual_query", "project_review", "cross_project_synthesis"} and final_answer and not typed_reads:
        add(
            "summary_amplification_risk",
            "warning",
            "The final answer used coordination context without a current typed-state read.",
            {"checkpoint_count": checkpoint_count, "workload": workload},
        )
    compactions = [item for item in events if item.get("type") == "compaction_end"]
    if compactions:
        last_sequence = int(compactions[-1].get("sequence", 0) or 0)
        reads_after = [
            item for item in events
            if int(item.get("sequence", 0) or 0) > last_sequence
            and item.get("type") == "tool_execution_start"
            and _is_typed_truth_read(str(item.get("toolName") or ""))
        ]
        summary = _jsonish_text(compactions[-1].get("result"))
        summary_claims = _status_claims(summary)
        current_claims = {
            claim
            for call in calls
            if _is_typed_truth_read(call["name"])
            for claim in _status_claims(_jsonish_text(call.get("result_data")))
        }
        final_claims = _status_claims(final_answer)
        contradicted = summary_claims & final_claims and current_claims and not (final_claims & current_claims)
        if contradicted:
            add(
                "compaction_contradiction",
                "error",
                "The final answer repeated a compacted status contradicted by a later typed read.",
                {"summary_statuses": sorted(summary_claims), "current_statuses": sorted(current_claims)},
            )
        elif workload in {"factual_query", "project_review", "cross_project_synthesis"} and not reads_after:
            add(
                "compaction_verification_gap",
                "warning",
                "A factual turn did not re-read typed state after compaction.",
                {"workload": workload},
            )
    return signals


def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, Mapping):
        parsed = {str(key): _parse_jsonish(item) for key, item in value.items()}
        content = parsed.get("content")
        if isinstance(content, list):
            text_items = [item.get("text") for item in content if isinstance(item, Mapping) and item.get("text")]
            if len(text_items) == 1:
                candidate = _parse_jsonish(text_items[0])
                if not isinstance(candidate, str):
                    parsed["parsed_content"] = candidate
        return parsed
    if isinstance(value, list):
        return [_parse_jsonish(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return _parse_jsonish(json.loads(stripped))
            except json.JSONDecodeError:
                return value
    return value


def _jsonish_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _reference_values(value: Any, *, key: str = "") -> set[str]:
    refs: set[str] = set()
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            refs.update(_reference_values(child, key=str(child_key)))
        return refs
    if isinstance(value, list):
        for child in value:
            refs.update(_reference_values(child, key=key))
        return refs
    if isinstance(value, str):
        if _REFERENCE_KEY_RE.search(key) and 2 <= len(value) <= 500:
            refs.add(value.strip().rstrip(".,;"))
        refs.update(item.rstrip(".,;") for item in _DOI_RE.findall(value))
        refs.update(item.rstrip(".,;") for item in _URL_RE.findall(value))
    return {item for item in refs if item}


def _file_identity(record: Mapping[str, Any]) -> tuple[Any, ...]:
    if record.get("sha256"):
        return (record.get("exists"), "sha256", record.get("sha256"))
    return (
        record.get("exists"),
        record.get("measurement"),
        record.get("size_bytes"),
        record.get("modified_ns"),
    )


def _content_identity_available(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    """Return true when every existing side of a transition has a content hash."""

    return all(
        not record.get("exists") or bool(record.get("sha256"))
        for record in (before, after)
    )


def _project_values(value: Any, *, key: str = "") -> set[str]:
    values: set[str] = set()
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            values.update(_project_values(child, key=str(child_key)))
    elif isinstance(value, list):
        for child in value:
            values.update(_project_values(child, key=key))
    elif isinstance(value, str) and _PROJECT_KEY_RE.search(key) and 1 < len(value) <= 200:
        values.add(value)
    return values


def _result_count(value: Any) -> int:
    counts: list[int] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"count", "total", "total_matches"} and isinstance(child, int):
                counts.append(max(0, child))
            elif key in {"items", "results", "sources", "hits"} and isinstance(child, list):
                counts.append(len(child))
            elif isinstance(child, (Mapping, list)):
                counts.append(_result_count(child))
    elif isinstance(value, list):
        counts.append(len(value))
    return max(counts, default=0)


def _manifest_lane(manifest: Mapping[str, Any]) -> str | None:
    lanes: set[str] = set()
    for source in manifest.get("sources") or []:
        if not isinstance(source, Mapping) or source.get("source_type") != "object_reference":
            continue
        ref = str(source.get("source_id") or "")
        if ref.startswith("paper"):
            lanes.add("paper")
        elif ref.startswith("experiment"):
            lanes.add("experiment")
        elif ref.startswith("research_artifact"):
            lanes.add("review")
        elif ref.startswith(("stage", "project", "task")) or ":" not in ref:
            lanes.add("project")
    return next(iter(lanes)) if len(lanes) == 1 else None


def _is_apply_tool(name: str) -> bool:
    normalized = str(name)
    return normalized in _CONFIRMED_WRITE_TOOLS or bool(
        re.search(r"(?:^|_)apply(?:_|$)", normalized)
    )


def _is_proposal_tool(name: str) -> bool:
    return bool(re.search(r"(?:^|_)propose(?:_|$)", str(name)))


def _is_workflow_state_tool(name: str) -> bool:
    return str(name) in _EXPECTED_WORKFLOW_STATE_TOOLS


def _is_external_write_tool(name: str) -> bool:
    normalized = str(name)
    return normalized in _EXTERNAL_WRITE_TOOLS or bool(
        _is_apply_tool(normalized)
        and re.search(r"(?:^sf_)(?:obsidian|overleaf)_", normalized)
    )


def _is_retrieval_tool(name: str) -> bool:
    normalized = str(name)
    return bool(
        re.search(r"(?:_search|_get|_sources|_health|_evidence_bundle|memory_search|session_search)$", normalized)
        and not _is_apply_tool(normalized)
    )


def _is_citation_retrieval_tool(name: str) -> bool:
    normalized = str(name)
    return bool(
        normalized in {
            "sf_evidence_bundle",
            "sf_knowledge_search",
            "sf_paper_get",
            "sf_paper_search",
            "sf_research_delegate",
        }
        or re.search(r"(?:web|scholar|literature|citation).*(?:search|get)$", normalized)
    )


def _delegated_worker_metrics(calls: list[dict[str, Any]]) -> dict[str, Any]:
    totals = Counter()
    worker_count = 0
    successful = 0
    failed = 0
    unique_sources = 0
    providers: set[str] = set()
    models: set[str] = set()
    forbidden_tools: set[str] = set()
    for call in calls:
        if call.get("name") != "sf_research_delegate":
            continue
        result = call.get("result_data")
        if not isinstance(result, Mapping):
            continue
        details = result.get("details")
        if not isinstance(details, Mapping):
            parsed = result.get("parsed_content")
            details = parsed if isinstance(parsed, Mapping) else {}
        worker_count += int(details.get("worker_count") or 0)
        successful += int(details.get("successful_worker_count") or 0)
        failed += int(details.get("failed_worker_count") or 0)
        unique_sources += int(details.get("unique_source_count") or 0)
        usage = details.get("aggregate_usage")
        if isinstance(usage, Mapping):
            for key in (
                "input",
                "output",
                "cacheRead",
                "cacheWrite",
                "reasoning",
                "totalTokens",
                "cost",
                "turns",
            ):
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    totals[key] += value
        for worker in details.get("workers") or []:
            if not isinstance(worker, Mapping):
                continue
            if worker.get("provider"):
                providers.add(str(worker["provider"]))
            if worker.get("model"):
                models.add(str(worker["model"]))
            for name in worker.get("tool_names") or []:
                normalized = str(name)
                if normalized not in _RESEARCH_WORKER_ALLOWED_TOOLS:
                    forbidden_tools.add(normalized)
    return {
        "worker_count": worker_count,
        "successful_worker_count": successful,
        "failed_worker_count": failed,
        "unique_source_count": unique_sources,
        "usage": {key: totals[key] for key in (
            "input", "output", "cacheRead", "cacheWrite", "reasoning", "totalTokens", "cost", "turns"
        )},
        "providers": providers,
        "models": models,
        "forbidden_tools": sorted(forbidden_tools),
    }


def _is_typed_truth_read(name: str) -> bool:
    normalized = str(name)
    return normalized.startswith("sf_") and _is_retrieval_tool(normalized) and not re.search(
        r"knowledge|memory|session|zotero", normalized
    )


def _status_claims(value: str) -> set[str]:
    return {item.casefold() for item in _STATUS_RE.findall(str(value or ""))}


def _duration_ms(start: Any, end: Any) -> float | None:
    if not start or not end:
        return None
    try:
        left = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        right = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round(max(0.0, (right - left).total_seconds() * 1000), 3)


def _estimate_tokens(value: str) -> int:
    if not value:
        return 0
    return max(1, math.ceil(len(value.encode("utf-8")) / 4))


def _sha256_json(value: Any) -> str:
    material = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _sum_cost(items: list[dict[str, Any]], key: str) -> int:
    return int(sum(int(item.get("cost", {}).get(key) or 0) for item in items))


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 3)
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(values[lower], 3)
    weighted = values[lower] * (upper - position) + values[upper] * (position - lower)
    return round(weighted, 3)


def _wilson_interval(successes: int, attempts: int) -> dict[str, float]:
    if attempts <= 0:
        return {"low": 0.0, "high": 0.0}
    z = 1.959963984540054
    proportion = successes / attempts
    denominator = 1 + z * z / attempts
    center = (proportion + z * z / (2 * attempts)) / denominator
    margin = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * attempts)) / attempts) / denominator
    return {"low": round(max(0.0, center - margin), 4), "high": round(min(1.0, center + margin), 4)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
