"""Deterministic control-plane CLI for the Pi-native ScholarFlow runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .confirmation_store import list_pairings
from .domain_queries import DomainQueryError, ScholarFlowQueryService
from .gateway import (
    acknowledge_gateway_message,
    approve_gateway_pairing,
    gateway_status,
    list_gateway_inbox,
    poll_gateway_inbox_once,
    receive_gateway_input,
    start_gateway,
    stop_gateway,
)
from .runtime_supervisor import (
    pause_runtime_supervisor,
    resume_runtime_supervisor,
    run_runtime_supervisor_once,
    run_runtime_supervisor_loop,
    runtime_doctor,
    runtime_health_summary,
    runtime_supervisor_status,
    runtime_trend_report,
    start_runtime_supervisor,
    stop_runtime_supervisor,
)
from .quality import quality_status
from .pi_rpc_host import PiRpcHostError, acknowledge_pi_rpc_escalation, pi_rpc_status
from .scheduler import list_automation_jobs, run_due_jobs, schedule_default_jobs
from .storage import ScholarFlowStore


OBJECT_TYPES = ("project_stage", "task", "paper", "experiment", "research_artifact")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ScholarFlow Pi-native control plane (interactive reasoning runs in Pi)."
    )
    parser.add_argument("--data-dir", default=str(Path(".tmp") / "scholarflow-cli"))
    parser.add_argument("--vault-root", default=".", help="Obsidian vault root for deterministic maintenance jobs.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Inspect the Pi-native runtime boundary.")
    search = subparsers.add_parser("list", help="Read domain objects without invoking an agent.")
    search.add_argument("--type", choices=OBJECT_TYPES, required=True)
    search.add_argument("--query", default="")
    search.add_argument("--status", default=None)
    search.add_argument("--project-stage-id", default=None)
    search.add_argument("--kind", default=None, help="Optional research-artifact kind.")
    search.add_argument("--limit", type=int, default=20)
    show = subparsers.add_parser("show", help="Read one domain object without invoking an agent.")
    show.add_argument("--type", choices=OBJECT_TYPES, required=True)
    show.add_argument("--id", required=True)

    start = subparsers.add_parser("gateway-start", help="Start the deterministic inbound queue.")
    start.add_argument("--trust-mode", choices=("pairing", "open"), default="pairing")
    subparsers.add_parser("gateway-stop", help="Stop the inbound queue.")
    subparsers.add_parser("gateway-status", help="Show gateway state.")
    receive = subparsers.add_parser("gateway-receive", help="Queue external text for Pi; never interpret it here.")
    receive.add_argument("--sender-id", required=True)
    receive.add_argument("--source", default="gateway")
    receive.add_argument("--input", required=True)
    approve = subparsers.add_parser("approve-pairing", help="Approve one external sender pairing.")
    approve.add_argument("--id", required=True)
    approve.add_argument("--approved-by", default="user")
    inbox = subparsers.add_parser("gateway-inbox", help="List queued messages for Pi.")
    inbox.add_argument("--status", default="pending")
    inbox.add_argument("--limit", type=int, default=100)
    subparsers.add_parser("gateway-poll-once", help="Poll the inbound queue once without agent reasoning.")
    ack = subparsers.add_parser("gateway-ack", help="Confirm the outcome of a message handled by Pi.")
    ack.add_argument("--message-id", required=True)
    ack.add_argument("--outcome", choices=("processed", "dismissed", "failed"), default="processed")
    ack.add_argument("--note", default=None)
    ack.add_argument("--confirmed", action="store_true", required=True)
    ack.add_argument("--confirmed-by", default="user")
    subparsers.add_parser("pairings", help="List external sender pairings.")
    subparsers.add_parser("automation-schedule-defaults", help="Schedule vault maintenance and Pi workflow requests.")
    subparsers.add_parser("automation-status", help="List deterministic automation jobs.")
    subparsers.add_parser("automation-run-due", help="Run due deterministic jobs and queue due Pi workflows.")
    subparsers.add_parser("quality-status", help="Audit stale experiments, workflow health, memory ownership, and Skills.")
    runtime_start = subparsers.add_parser("runtime-start", help="Start the deterministic supervisor state machine.")
    runtime_start.add_argument("--poll-interval-seconds", type=int, default=600)
    subparsers.add_parser("runtime-stop", help="Stop the deterministic supervisor state machine.")
    subparsers.add_parser("runtime-pause", help="Pause ticks without discarding supervisor state.")
    subparsers.add_parser("runtime-resume", help="Resume a paused supervisor.")
    subparsers.add_parser("runtime-status", help="Show deterministic supervisor state.")
    health = subparsers.add_parser("runtime-health", help="Summarize runtime, gateway, automation, and event health.")
    health.add_argument("--limit", type=int, default=50)
    report = subparsers.add_parser("runtime-report", help="Report bounded runtime trends and failure reasons.")
    report.add_argument("--limit", type=int, default=200)
    report.add_argument("--top-failures", type=int, default=5)
    subparsers.add_parser("runtime-doctor", help="Check control-plane storage, vault, gateway, jobs, and health.")
    subparsers.add_parser("runtime-run-once", help="Execute one supervisor tick and queue due Pi workflows.")
    subparsers.add_parser("pi-rpc-status", help="Show supervised Pi RPC state and active escalation.")
    escalation_ack = subparsers.add_parser(
        "pi-rpc-escalation-ack",
        help="Acknowledge an active Pi RPC escalation without resetting its circuit.",
    )
    escalation_ack.add_argument("--escalation-id", required=True)
    escalation_ack.add_argument("--confirmed", action="store_true", required=True)
    escalation_ack.add_argument("--confirmed-by", default="user")
    loop = subparsers.add_parser("runtime-loop", help="Run the deterministic supervisor loop.")
    loop.add_argument("--max-ticks", type=int, default=None)
    loop.add_argument("--sleep-seconds", type=float, default=None)
    for command_parser in subparsers.choices.values():
        command_parser.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS)
    return parser


def execute(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.data_dir)
    store = ScholarFlowStore(root)
    query = ScholarFlowQueryService(store)
    if args.command == "doctor":
        repository = Path(__file__).resolve().parents[1]
        return {
            "runtime": "pi-native",
            "interactive_agent": "@earendil-works/pi-coding-agent",
            "legacy_agent_runtime_removed": True,
            "data_dir": str(root.resolve()),
            "pi_settings": str(repository / ".pi" / "settings.json"),
            "pi_package": str(repository / "packages" / "scholarflow-pi"),
            "next_command": "pi --approve",
        }
    if args.command == "list":
        method = {
            "project_stage": query.search_project_stages,
            "task": query.search_tasks,
            "paper": query.search_papers,
            "experiment": query.search_experiments,
            "research_artifact": query.search_research_artifacts,
        }[args.type]
        kwargs: dict[str, Any] = {"query": args.query, "status": args.status, "limit": args.limit}
        if args.type in {"task", "experiment", "research_artifact"}:
            kwargs["project_stage_id"] = args.project_stage_id
        if args.type == "research_artifact":
            kwargs["kind"] = args.kind
        return method(**kwargs)
    if args.command == "show":
        return {
            "project_stage": query.get_project_stage,
            "task": query.get_task,
            "paper": query.get_paper,
            "experiment": query.get_experiment,
            "research_artifact": query.get_research_artifact,
        }[args.type](args.id)
    if args.command == "gateway-start":
        return start_gateway(root, trust_mode=args.trust_mode)
    if args.command == "gateway-stop":
        return stop_gateway(root)
    if args.command == "gateway-status":
        return gateway_status(root)
    if args.command == "gateway-receive":
        return receive_gateway_input(store, sender_id=args.sender_id, source=args.source, raw_input=args.input)
    if args.command == "approve-pairing":
        return approve_gateway_pairing(root, pairing_id=args.id, approved_by=args.approved_by)
    if args.command == "gateway-inbox":
        return list_gateway_inbox(root, status=args.status, limit=args.limit)
    if args.command == "gateway-poll-once":
        return poll_gateway_inbox_once(root)
    if args.command == "gateway-ack":
        return acknowledge_gateway_message(
            root,
            message_id=args.message_id,
            outcome=args.outcome,
            note=args.note,
            confirmed=args.confirmed,
            confirmed_by=args.confirmed_by,
        )
    if args.command == "pairings":
        return list_pairings(root)
    if args.command == "automation-schedule-defaults":
        return schedule_default_jobs(root)
    if args.command == "automation-status":
        return list_automation_jobs(root)
    if args.command == "automation-run-due":
        return run_due_jobs(store, vault_root=Path(args.vault_root))
    if args.command == "quality-status":
        return quality_status(store, repository_root=Path(__file__).resolve().parents[1])
    if args.command == "runtime-start":
        return start_runtime_supervisor(root, poll_interval_seconds=args.poll_interval_seconds)
    if args.command == "runtime-stop":
        return stop_runtime_supervisor(root)
    if args.command == "runtime-pause":
        return pause_runtime_supervisor(root)
    if args.command == "runtime-resume":
        return resume_runtime_supervisor(root)
    if args.command == "runtime-status":
        return runtime_supervisor_status(root)
    if args.command == "runtime-health":
        return runtime_health_summary(root, recent_limit=args.limit)
    if args.command == "runtime-report":
        return runtime_trend_report(root, recent_limit=args.limit, failure_top_k=args.top_failures)
    if args.command == "runtime-doctor":
        return runtime_doctor(root, vault_root=Path(args.vault_root))
    if args.command == "runtime-run-once":
        return run_runtime_supervisor_once(store, vault_root=Path(args.vault_root))
    if args.command == "pi-rpc-status":
        return pi_rpc_status(root)
    if args.command == "pi-rpc-escalation-ack":
        return acknowledge_pi_rpc_escalation(
            root,
            escalation_id=args.escalation_id,
            confirmed=args.confirmed,
            confirmed_by=args.confirmed_by,
        )
    if args.command == "runtime-loop":
        return run_runtime_supervisor_loop(
            store,
            vault_root=Path(args.vault_root),
            max_ticks=args.max_ticks,
            sleep_seconds=args.sleep_seconds,
        )
    raise ValueError(f"unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = execute(args)
    except (DomainQueryError, PiRpcHostError, ValueError, OSError, json.JSONDecodeError) as exc:
        payload = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
        print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
