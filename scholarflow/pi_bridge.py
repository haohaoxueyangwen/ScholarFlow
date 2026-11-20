"""Versioned command bridge between Pi tools and ScholarFlow domain storage.

This module is intentionally deterministic and must not make model calls.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from .domain_queries import DomainQueryError, ScholarFlowQueryService
from .evidence import prepare_evidence_bundle
from .event_bus import append_event
from .experiment_observer import observe_experiment
from .gateway import acknowledge_gateway_message, get_gateway_message, list_gateway_inbox
from .intake import IntakeService
from .knowledge_engine import build_knowledge_engine, prepare_knowledge_bundle
from .knowledge_candidates import CandidateKnowledgeService
from .knowledge_ingestion import KnowledgeIngestionManifest, summarize_ingestion_manifest
from .manuscript import ManuscriptService
from .mutations import MutationError, TaskMutationService
from .paper_service import PaperService
from .project_knowledge import ProjectKnowledgeService
from .quality import quality_status
from .research_mutations import KBMutationService, ResearchMutationService
from .research_workflow import build_topic_mind_map, list_research_perspectives
from .runtime_supervisor import get_pi_host_status
from .storage import ScholarFlowStore
from .zotero_client import build_zotero_client, describe_zotero_client
from .zotero_mutations import ZoteroMutationService

PROTOCOL_VERSION = 2


def search_project_stages(
    store: ScholarFlowStore,
    *,
    query: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Compatibility wrapper for the first Pi bridge test surface."""

    return ScholarFlowQueryService(store).search_project_stages(
        query=query,
        status=status,
        limit=limit,
        offset=offset,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ScholarFlow Pi domain bridge")
    parser.add_argument("--data-dir", required=True, help="ScholarFlow data root controlled by the Pi host.")
    parser.add_argument("--request-id", default=None, help="Optional host tool-call id used for observability.")
    parser.add_argument("--vault-dir", default=None, help="Optional Obsidian vault root controlled by the Pi host.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health", help="Return bridge and storage protocol information.")
    subparsers.add_parser("bridge-describe", help="Describe the versioned bridge operation registry.")
    subparsers.add_parser("quality-status", help="Audit runtime, memory ownership, and Skill quality without mutation.")
    subparsers.add_parser("runtime-host-status", help="Read the supervised Pi host status and pending request count.")

    perspectives = subparsers.add_parser("research-perspectives", help="List deterministic research perspectives.")
    mind_map = subparsers.add_parser("research-mind-map", help="Build a read-only topic map from stored records.")
    mind_map.add_argument("--topic", required=True)
    mind_map.add_argument("--max-items", type=int, default=5)
    evidence_bundle = subparsers.add_parser("evidence-bundle", help="Build a read-only evidence bundle from stored ScholarFlow records.")
    evidence_bundle.add_argument("--query", default="")
    evidence_bundle.add_argument("--limit", type=int, default=5)
    evidence_bundle.add_argument("--project-stage-id", default=None)
    subparsers.add_parser(
        "knowledge-health",
        help="Inspect the configured AI knowledge engine without exposing credentials.",
    )
    knowledge_search = subparsers.add_parser(
        "knowledge-search",
        help="Search the provenance-checked AI knowledge engine.",
    )
    knowledge_search.add_argument("--query", required=True)
    knowledge_search.add_argument("--mode", choices=("auto", "mix", "hybrid", "local", "global", "naive"), default="auto")
    knowledge_search.add_argument("--limit", type=int, default=5)
    knowledge_search.add_argument("--scope", default=None)
    knowledge_search.add_argument("--project-stage-id", default=None)
    knowledge_sources = subparsers.add_parser(
        "knowledge-sources",
        help="List governed sources currently tracked by the AI knowledge ingestion manifest.",
    )
    knowledge_sources.add_argument("--scope", default=None)
    knowledge_sources.add_argument("--limit", type=int, default=100)

    subparsers.add_parser("zotero-describe", help="Describe the host-configured Zotero backend without exposing credentials.")
    subparsers.add_parser("zotero-collections", help="List Zotero collections through the host-configured backend.")
    zotero_items = subparsers.add_parser("zotero-items", help="List Zotero items through the host-configured backend.")
    zotero_items.add_argument("--collection-key", default=None)
    zotero_items.add_argument("--limit", type=int, default=25)
    zotero_get = subparsers.add_parser("zotero-item-get", help="Read one Zotero item through the host-configured backend.")
    zotero_get.add_argument("--item-key", required=True)
    zotero_tag_propose = subparsers.add_parser("zotero-tag-propose", help="Propose adding tags to one Zotero item without writing it.")
    zotero_tag_propose.add_argument("--item-key", required=True)
    zotero_tag_propose.add_argument("--tags-json", required=True, help="JSON array of tags to add.")
    zotero_tag_get = subparsers.add_parser("zotero-tag-proposal-get", help="Get a stored governed Zotero tag proposal.")
    zotero_tag_get.add_argument("--proposal-id", required=True)
    zotero_tag_apply = subparsers.add_parser("zotero-tag-apply", help="Apply a confirmed governed Zotero tag proposal.")
    _add_apply_arguments(zotero_tag_apply)

    inbox_search = subparsers.add_parser("gateway-inbox-search", help="List external messages queued for Pi.")
    inbox_search.add_argument("--status", default="pending")
    inbox_search.add_argument("--limit", type=int, default=20)
    inbox_get = subparsers.add_parser("gateway-message-get", help="Get one queued external message.")
    inbox_get.add_argument("--message-id", required=True)
    inbox_ack = subparsers.add_parser("gateway-message-ack", help="Acknowledge a Pi-handled external message.")
    inbox_ack.add_argument("--message-id", required=True)
    inbox_ack.add_argument("--outcome", choices=("processed", "dismissed", "failed"), default="processed")
    inbox_ack.add_argument("--note", default=None)
    inbox_ack.add_argument("--confirmed", action="store_true")
    inbox_ack.add_argument("--confirmed-by", default=None)
    inbox_ack.add_argument("--confirmed-at", default=None)

    intake_create = subparsers.add_parser("intake-create", help="Create a deterministic ScholarFlow intake item from raw input.")
    intake_create.add_argument("--raw-input", required=True)
    intake_create.add_argument("--source", default="chat")
    intake_get = subparsers.add_parser("intake-get", help="Get one deterministic ScholarFlow intake item.")
    intake_get.add_argument("--intake-id", required=True)
    intake_anchor = subparsers.add_parser("intake-anchor", help="Resolve anchors for one ScholarFlow intake item.")
    intake_anchor.add_argument("--intake-id", required=True)
    intake_prepare = subparsers.add_parser("intake-prepare", help="Prepare deterministic route and proposal scaffolds for one intake item.")
    intake_prepare.add_argument("--intake-id", required=True)
    intake_prepare.add_argument("--route", required=True, choices=("task_only", "task_and_experiment", "paper_intake"))
    intake_prepare.add_argument("--stage-id", required=True)
    intake_prepare.add_argument("--related-task-id", action="append", default=[])
    intake_prepare.add_argument("--related-experiment-id", action="append", default=[])
    intake_prepare.add_argument("--related-paper-id", action="append", default=[])
    intake_propose = subparsers.add_parser("intake-propose", help="Prepare and emit deterministic governed proposals for one intake item.")
    intake_propose.add_argument("--intake-id", required=True)

    project_search = subparsers.add_parser("project-search", help="Search ScholarFlow project stages.")
    project_search.add_argument("--query", default="", help="Case-insensitive terms matched across project-stage fields.")
    project_search.add_argument("--status", default=None, help="Optional ProjectStageStatus value.")
    project_search.add_argument("--limit", type=int, default=20, help="Maximum number of results, from 1 to 100.")
    project_search.add_argument("--offset", type=int, default=0, help="Skip this many matches before returning results.")

    project_get = subparsers.add_parser("project-get", help="Get one ScholarFlow project stage.")
    project_get.add_argument("--stage-id", required=True)
    _add_mutation_arguments(subparsers, "project", "stage-id", "stage_id")
    project_create = subparsers.add_parser("project-propose-create", help="Create a governed new-project proposal.")
    project_create.add_argument("--stage-id", required=True)
    project_create.add_argument("--changes-json", required=True)
    project_apply_create = subparsers.add_parser("project-apply-create", help="Apply a confirmed new-project proposal.")
    _add_apply_arguments(project_apply_create)

    task_search = subparsers.add_parser("task-search", help="Search ScholarFlow tasks.")
    _add_search_arguments(task_search, with_project_stage=True)
    task_get = subparsers.add_parser("task-get", help="Get one ScholarFlow task.")
    task_get.add_argument("--task-id", required=True)

    task_propose = subparsers.add_parser("task-propose-update", help="Create a governed task update proposal.")
    task_propose.add_argument("--task-id", required=True)
    task_propose.add_argument("--changes-json", required=True)
    task_create = subparsers.add_parser("task-propose-create", help="Create a governed new-task proposal.")
    task_create.add_argument("--task-id", required=True)
    task_create.add_argument("--changes-json", required=True)
    task_proposal_get = subparsers.add_parser("task-proposal-get", help="Get a stored task update proposal.")
    task_proposal_get.add_argument("--proposal-id", required=True)
    task_apply = subparsers.add_parser("task-apply-update", help="Apply a confirmed task update proposal.")
    task_apply.add_argument("--proposal-id", required=True)
    task_apply.add_argument("--confirmed", action="store_true")
    task_apply.add_argument("--confirmed-by", default=None)
    task_apply.add_argument("--confirmed-at", default=None)

    paper_search = subparsers.add_parser("paper-search", help="Search ScholarFlow papers.")
    _add_search_arguments(paper_search)
    paper_get = subparsers.add_parser("paper-get", help="Get one ScholarFlow paper.")
    paper_get.add_argument("--paper-id", required=True)

    experiment_search = subparsers.add_parser("experiment-search", help="Search ScholarFlow experiments.")
    _add_search_arguments(experiment_search, with_project_stage=True)
    experiment_get = subparsers.add_parser("experiment-get", help="Get one ScholarFlow experiment.")
    experiment_get.add_argument("--experiment-id", required=True)
    experiment_observe = subparsers.add_parser("experiment-observe", help="Inspect approved logs and checkpoints stored on an experiment.")
    experiment_observe.add_argument("--experiment-id", required=True)
    experiment_observe.add_argument("--include-tail", action="store_true")
    experiment_observe.add_argument("--tail-lines", type=int, default=50)
    _add_mutation_arguments(subparsers, "experiment", "experiment-id", "experiment_id", operation="log")

    _add_mutation_arguments(subparsers, "paper", "paper-id", "paper_id", operation="intake")

    artifact_search = subparsers.add_parser("artifact-search", help="Search durable research artifacts.")
    _add_search_arguments(artifact_search, with_project_stage=True)
    artifact_search.add_argument("--kind", default=None)
    artifact_get = subparsers.add_parser("artifact-get", help="Get one durable research artifact.")
    artifact_get.add_argument("--artifact-id", required=True)
    _add_mutation_arguments(subparsers, "artifact", "artifact-id", "artifact_id", operation="publish")

    manuscript_search = subparsers.add_parser("manuscript-search", help="Search governed local LaTeX manuscripts.")
    manuscript_search.add_argument("--project-stage-id", default=None)
    manuscript_get = subparsers.add_parser("manuscript-get", help="Read one governed local LaTeX manuscript.")
    manuscript_get.add_argument("--manuscript-id", required=True)
    manuscript_section = subparsers.add_parser("manuscript-read-section", help="Read one anchored LaTeX manuscript section.")
    manuscript_section.add_argument("--manuscript-id", required=True)
    manuscript_section.add_argument("--section", required=True)
    manuscript_validate = subparsers.add_parser("manuscript-validate", help="Validate one local LaTeX manuscript and report compiler availability.")
    manuscript_validate.add_argument("--manuscript-id", required=True)
    manuscript_compile = subparsers.add_parser("manuscript-compile", help="Compile one accepted local LaTeX manuscript revision to a hash-scoped PDF.")
    manuscript_compile.add_argument("--manuscript-id", required=True)
    manuscript_patch = subparsers.add_parser("manuscript-propose-patch", help="Propose an evidence-linked anchored LaTeX section patch.")
    manuscript_patch.add_argument("--manuscript-id", required=True)
    manuscript_patch.add_argument("--section", required=True)
    manuscript_patch.add_argument("--replacement-latex", required=True)
    manuscript_patch.add_argument("--evidence-ref", action="append", default=[])
    manuscript_patch.add_argument("--base-file-hash", required=True)
    manuscript_proposal_get = subparsers.add_parser("manuscript-proposal-get", help="Get one manuscript proposal.")
    manuscript_proposal_get.add_argument("--proposal-id", required=True)
    manuscript_apply = subparsers.add_parser("manuscript-apply-patch", help="Apply a confirmed manuscript proposal.")
    _add_apply_arguments(manuscript_apply)

    relation_search = subparsers.add_parser("relation-search", help="Search confirmed research knowledge relations.")
    relation_search.add_argument("--query", default="")
    relation_search.add_argument("--predicate", default=None)
    relation_search.add_argument("--subject-ref", default=None)
    relation_search.add_argument("--object-ref", default=None)
    relation_search.add_argument("--limit", type=int, default=20)
    relation_get = subparsers.add_parser("relation-get", help="Get one research knowledge relation.")
    relation_get.add_argument("--relation-id", required=True)
    _add_mutation_arguments(subparsers, "relation", "relation-id", "relation_id", operation="upsert")

    candidate_search = subparsers.add_parser(
        "candidate-search",
        help="Search machine-extracted candidate knowledge separately from confirmed truth.",
    )
    candidate_search.add_argument("--query", default=None)
    candidate_search.add_argument(
        "--status",
        choices=("pending", "promoted", "rejected"),
        default=None,
    )
    candidate_search.add_argument(
        "--kind",
        choices=("entity", "claim", "edge"),
        default=None,
    )
    candidate_search.add_argument("--source-ref", default=None)
    candidate_search.add_argument("--limit", type=int, default=100)
    candidate_get = subparsers.add_parser(
        "candidate-get",
        help="Get one evidence-bound knowledge candidate.",
    )
    candidate_get.add_argument("--candidate-id", required=True)
    candidate_propose = subparsers.add_parser(
        "candidate-propose-review",
        help="Propose promotion or rejection of one candidate.",
    )
    candidate_propose.add_argument("--candidate-id", required=True)
    candidate_propose.add_argument(
        "--decision",
        choices=("promote", "reject"),
        required=True,
    )
    candidate_propose.add_argument("--note", default="")
    candidate_proposal_get = subparsers.add_parser(
        "candidate-proposal-get",
        help="Get one stored candidate review proposal.",
    )
    candidate_proposal_get.add_argument("--proposal-id", required=True)
    candidate_apply = subparsers.add_parser(
        "candidate-apply-review",
        help="Apply a confirmed candidate promotion or rejection.",
    )
    _add_apply_arguments(candidate_apply)

    kb_propose = subparsers.add_parser("kb-propose-write", help="Propose a governed object-to-vault write.")
    kb_propose.add_argument("--object-type", required=True)
    kb_propose.add_argument("--object-id", required=True)
    kb_propose.add_argument("--content-kind", required=True)
    kb_propose.add_argument("--options-json", default="{}")
    kb_get = subparsers.add_parser("kb-proposal-get", help="Get a stored KB write proposal.")
    kb_get.add_argument("--proposal-id", required=True)
    kb_apply = subparsers.add_parser("kb-apply-write", help="Apply a confirmed KB write proposal.")
    _add_apply_arguments(kb_apply)
    return parser


def execute_command(args: argparse.Namespace) -> dict[str, Any]:
    store = ScholarFlowStore(Path(args.data_dir))
    if args.command == "health":
        return _success(
            "bridge.health",
            {
                "bridge": "scholarflow-pi",
                "storage": "json",
                "data_dir": str(store.root_dir.resolve()),
            },
        )
    if args.command == "bridge-describe":
        return _success("bridge.describe", _describe_bridge_operations())
    if args.command == "quality-status":
        return _success(
            "quality.status",
            quality_status(store, repository_root=Path(__file__).resolve().parents[1]),
        )
    if args.command == "runtime-host-status":
        return _success("runtime.host.status", get_pi_host_status(store.root_dir))
    queries = ScholarFlowQueryService(store)
    intake = IntakeService(store)
    mutations = TaskMutationService(store)
    research_mutations = ResearchMutationService(store)
    candidate_knowledge = CandidateKnowledgeService(store)
    manuscripts = ManuscriptService(store)
    if args.command in {"zotero-tag-propose", "zotero-tag-proposal-get", "zotero-tag-apply"}:
        client = build_zotero_client()
        zotero_mutations = ZoteroMutationService(store.root_dir, client=client)
        if args.command == "zotero-tag-propose":
            return _success(
                "zotero.item.tags.propose",
                zotero_mutations.propose_add_tags(
                    args.item_key,
                    _parse_tags(args.tags_json),
                    request_id=args.request_id,
                ),
            )
        if args.command == "zotero-tag-proposal-get":
            return _success("zotero.item.tags.proposal.get", zotero_mutations.get_proposal(args.proposal_id))
        return _success(
            "zotero.item.tags.apply",
            zotero_mutations.apply(
                args.proposal_id,
                confirmation=_confirmation(args),
                request_id=args.request_id,
            ),
        )
    elif args.command.startswith("zotero-"):
        client = build_zotero_client()
        papers = PaperService(store, zotero_client=client)
        if args.command == "zotero-describe":
            payload = _success("zotero.describe", describe_zotero_client(client))
        elif args.command == "zotero-collections":
            payload = _success("zotero.collections", papers.list_zotero_collections().to_dict())
        elif args.command == "zotero-items":
            payload = _success("zotero.items", papers.list_zotero_items(collection_key=args.collection_key, limit=max(1, min(args.limit, 100))).to_dict())
        else:
            payload = _success("zotero.item.get", client.get_item(args.item_key).to_dict())
    elif args.command == "research-perspectives":
        payload = _success("research.perspectives", list_research_perspectives())
    elif args.command == "research-mind-map":
        payload = _success(
            "research.mind_map",
            {"topic": args.topic, "markdown": build_topic_mind_map(store, topic=args.topic, max_items=args.max_items)},
        )
    elif args.command == "evidence-bundle":
        payload = _success(
            "evidence.bundle",
            prepare_evidence_bundle(
                store,
                query=args.query,
                limit=args.limit,
                project_stage_id=args.project_stage_id,
            ).to_dict(),
        )
    elif args.command == "knowledge-health":
        payload = _success(
            "knowledge.health",
            build_knowledge_engine(store.root_dir).health(),
        )
    elif args.command == "knowledge-search":
        payload = _success(
            "knowledge.search",
            prepare_knowledge_bundle(
                build_knowledge_engine(store.root_dir),
                query=args.query,
                mode=args.mode,
                limit=args.limit,
                scope=args.scope,
                project_stage_id=args.project_stage_id,
            ).to_dict(),
        )
    elif args.command == "knowledge-sources":
        payload = _success(
            "knowledge.sources",
            summarize_ingestion_manifest(
                KnowledgeIngestionManifest(store.root_dir),
                scope=args.scope,
                limit=args.limit,
            ),
        )
    elif args.command == "gateway-inbox-search":
        payload = _success("gateway.inbox.search", list_gateway_inbox(store.root_dir, status=args.status, limit=args.limit))
    elif args.command == "gateway-message-get":
        payload = _success("gateway.message.get", get_gateway_message(store.root_dir, message_id=args.message_id))
    elif args.command == "gateway-message-ack":
        return _success(
            "gateway.message.ack",
            acknowledge_gateway_message(
                store.root_dir,
                message_id=args.message_id,
                outcome=args.outcome,
                note=args.note,
                confirmed=args.confirmed,
                confirmed_by=args.confirmed_by,
            ),
        )
    elif args.command == "intake-create":
        payload = _success(
            "intake.create",
            intake.create_item(raw_input=args.raw_input, source=args.source),
        )
    elif args.command == "intake-get":
        payload = _success("intake.get", intake.get_item(args.intake_id))
    elif args.command == "intake-anchor":
        payload = _success("intake.anchor", intake.anchor_item(args.intake_id))
    elif args.command == "intake-prepare":
        payload = _success(
            "intake.prepare",
            intake.prepare_item(
                args.intake_id,
                route=args.route,
                stage_id=args.stage_id,
                related_task_ids=args.related_task_id,
                related_experiment_ids=args.related_experiment_id,
                related_paper_ids=args.related_paper_id,
            ),
        )
    elif args.command == "intake-propose":
        payload = _success("intake.propose", intake.propose_item(args.intake_id, request_id=args.request_id))
    elif args.command == "project-search":
        payload = _success(
            "project.search",
            queries.search_project_stages(
                query=args.query,
                status=args.status,
                limit=args.limit,
                offset=args.offset,
            ),
        )
    elif args.command == "project-get":
        payload = _success("project.get", queries.get_project_stage(args.stage_id))
    elif args.command == "project-propose-update":
        return _success("project.update.propose", research_mutations.propose_project_update(args.stage_id, _parse_changes(args.changes_json), request_id=args.request_id))
    elif args.command == "project-propose-create":
        return _success("project.create.propose", research_mutations.propose_project_create(args.stage_id, _parse_changes(args.changes_json), request_id=args.request_id))
    elif args.command == "project-proposal-get":
        return _success("project.update.proposal.get", research_mutations.get_proposal(args.proposal_id))
    elif args.command == "project-apply-update":
        return _success_with_project_knowledge(
            "project.update.apply",
            research_mutations.apply(args.proposal_id, confirmation=_confirmation(args), request_id=args.request_id),
            store,
        )
    elif args.command == "project-apply-create":
        return _success_with_project_knowledge(
            "project.create.apply",
            research_mutations.apply(args.proposal_id, confirmation=_confirmation(args), request_id=args.request_id),
            store,
        )
    elif args.command == "task-search":
        payload = _success(
            "task.search",
            queries.search_tasks(
                query=args.query,
                status=args.status,
                project_stage_id=args.project_stage_id,
                limit=args.limit,
            ),
        )
    elif args.command == "task-get":
        payload = _success("task.get", queries.get_task(args.task_id))
    elif args.command == "task-propose-update":
        payload = _success(
            "task.update.propose",
            mutations.propose_update(
                args.task_id,
                _parse_changes(args.changes_json),
                request_id=args.request_id,
            ),
        )
        return payload
    elif args.command == "task-propose-create":
        return _success("task.create.propose", mutations.propose_create(
            args.task_id, _parse_changes(args.changes_json), request_id=args.request_id,
        ))
    elif args.command == "task-proposal-get":
        payload = _success("task.update.proposal.get", mutations.get_proposal(args.proposal_id))
        return payload
    elif args.command == "task-apply-update":
        confirmation = {
            "proposal_id": args.proposal_id,
            "confirmed": args.confirmed,
            "confirmed_by": args.confirmed_by,
            "confirmed_at": args.confirmed_at,
            "pi_tool_call_id": args.request_id,
        }
        payload = _success_with_project_knowledge(
            "task.update.apply",
            mutations.apply_update(
                args.proposal_id,
                confirmation=confirmation,
                request_id=args.request_id,
            ),
            store,
        )
        return payload
    elif args.command == "paper-search":
        payload = _success(
            "paper.search",
            queries.search_papers(query=args.query, status=args.status, limit=args.limit),
        )
    elif args.command == "paper-get":
        payload = _success("paper.get", queries.get_paper(args.paper_id))
    elif args.command == "paper-propose-intake":
        return _success("paper.intake.propose", research_mutations.propose_paper_intake(args.paper_id, _parse_changes(args.changes_json), request_id=args.request_id))
    elif args.command == "paper-proposal-get":
        return _success("paper.intake.proposal.get", research_mutations.get_proposal(args.proposal_id))
    elif args.command == "paper-apply-intake":
        return _success_with_project_knowledge(
            "paper.intake.apply",
            research_mutations.apply(args.proposal_id, confirmation=_confirmation(args), request_id=args.request_id),
            store,
        )
    elif args.command == "experiment-search":
        payload = _success(
            "experiment.search",
            queries.search_experiments(
                query=args.query,
                status=args.status,
                project_stage_id=args.project_stage_id,
                limit=args.limit,
            ),
        )
    elif args.command == "experiment-get":
        payload = _success("experiment.get", queries.get_experiment(args.experiment_id))
    elif args.command == "experiment-observe":
        payload = _success(
            "experiment.observe",
            observe_experiment(
                store,
                args.experiment_id,
                include_tail=args.include_tail,
                tail_lines=args.tail_lines,
                request_id=args.request_id,
            ),
        )
    elif args.command == "experiment-propose-log":
        return _success("experiment.log.propose", research_mutations.propose_experiment_log(args.experiment_id, _parse_changes(args.changes_json), request_id=args.request_id))
    elif args.command == "experiment-proposal-get":
        return _success("experiment.log.proposal.get", research_mutations.get_proposal(args.proposal_id))
    elif args.command == "experiment-apply-log":
        return _success_with_project_knowledge(
            "experiment.log.apply",
            research_mutations.apply(args.proposal_id, confirmation=_confirmation(args), request_id=args.request_id),
            store,
        )
    elif args.command == "artifact-search":
        payload = _success(
            "research_artifact.search",
            queries.search_research_artifacts(
                query=args.query,
                status=args.status,
                kind=args.kind,
                project_stage_id=args.project_stage_id,
                limit=args.limit,
            ),
        )
    elif args.command == "artifact-get":
        payload = _success("research_artifact.get", queries.get_research_artifact(args.artifact_id))
    elif args.command == "artifact-propose-publish":
        return _success("research_artifact.publish.propose", research_mutations.propose_research_artifact(args.artifact_id, _parse_changes(args.changes_json), request_id=args.request_id))
    elif args.command == "artifact-proposal-get":
        return _success("research_artifact.publish.proposal.get", research_mutations.get_proposal(args.proposal_id))
    elif args.command == "artifact-apply-publish":
        return _success_with_project_knowledge(
            "research_artifact.publish.apply",
            research_mutations.apply(args.proposal_id, confirmation=_confirmation(args), request_id=args.request_id),
            store,
        )
    elif args.command == "manuscript-search":
        payload = _success("manuscript.search", manuscripts.list(project_stage_id=args.project_stage_id))
    elif args.command == "manuscript-get":
        payload = _success("manuscript.get", manuscripts.get(args.manuscript_id))
    elif args.command == "manuscript-read-section":
        payload = _success("manuscript.section.get", manuscripts.read_section(args.manuscript_id, args.section))
    elif args.command == "manuscript-validate":
        payload = _success("manuscript.validate", manuscripts.validate(args.manuscript_id))
    elif args.command == "manuscript-compile":
        payload = _success("manuscript.compile", manuscripts.compile(args.manuscript_id))
    elif args.command == "manuscript-propose-patch":
        return _success(
            "manuscript.patch.propose",
            manuscripts.propose_patch(
                args.manuscript_id,
                section=args.section,
                replacement_latex=args.replacement_latex,
                evidence_refs=args.evidence_ref,
                base_file_hash=args.base_file_hash,
                request_id=args.request_id,
            ),
        )
    elif args.command == "manuscript-proposal-get":
        return _success("manuscript.proposal.get", manuscripts.get_proposal(args.proposal_id))
    elif args.command == "manuscript-apply-patch":
        return _success("manuscript.patch.apply", manuscripts.apply(args.proposal_id, confirmation=_confirmation(args), request_id=args.request_id))
    elif args.command == "relation-search":
        payload = _success(
            "knowledge_relation.search",
            queries.search_knowledge_relations(
                query=args.query,
                predicate=args.predicate,
                subject_ref=args.subject_ref,
                object_ref=args.object_ref,
                limit=args.limit,
            ),
        )
    elif args.command == "relation-get":
        payload = _success("knowledge_relation.get", queries.get_knowledge_relation(args.relation_id))
    elif args.command == "relation-propose-upsert":
        return _success("knowledge_relation.upsert.propose", research_mutations.propose_knowledge_relation(args.relation_id, _parse_changes(args.changes_json), request_id=args.request_id))
    elif args.command == "relation-proposal-get":
        return _success("knowledge_relation.upsert.proposal.get", research_mutations.get_proposal(args.proposal_id))
    elif args.command == "relation-apply-upsert":
        return _success_with_project_knowledge(
            "knowledge_relation.upsert.apply",
            research_mutations.apply(args.proposal_id, confirmation=_confirmation(args), request_id=args.request_id),
            store,
        )
    elif args.command == "candidate-search":
        payload = _success(
            "knowledge_candidate.search",
            candidate_knowledge.list(
                query=args.query,
                status=args.status,
                kind=args.kind,
                source_ref=args.source_ref,
                limit=args.limit,
            ),
        )
    elif args.command == "candidate-get":
        payload = _success(
            "knowledge_candidate.get",
            candidate_knowledge.get(args.candidate_id),
        )
    elif args.command == "candidate-propose-review":
        return _success(
            "knowledge_candidate.review.propose",
            candidate_knowledge.propose_review(
                args.candidate_id,
                decision=args.decision,
                note=args.note,
                request_id=args.request_id,
            ),
        )
    elif args.command == "candidate-proposal-get":
        return _success(
            "knowledge_candidate.review.proposal.get",
            candidate_knowledge.get_review_proposal(args.proposal_id),
        )
    elif args.command == "candidate-apply-review":
        return _success_with_project_knowledge(
            "knowledge_candidate.review.apply",
            candidate_knowledge.apply_review(
                args.proposal_id,
                confirmation=_confirmation(args),
                request_id=args.request_id,
            ),
            store,
        )
    elif args.command.startswith("kb-"):
        if not args.vault_dir:
            raise MutationError("configuration_error", "SCHOLARFLOW_VAULT_DIR is required for KB operations")
        kb = KBMutationService(store, Path(args.vault_dir))
        if args.command == "kb-propose-write":
            options = _parse_changes(args.options_json)
            return _success("kb.write.propose", kb.propose_object_write(object_type=args.object_type, object_id=args.object_id, content_kind=args.content_kind, request_id=args.request_id, **options))
        if args.command == "kb-proposal-get":
            return _success("kb.write.proposal.get", kb.get_proposal(args.proposal_id))
        return _success("kb.write.apply", kb.apply(args.proposal_id, confirmation=_confirmation(args), request_id=args.request_id))
    else:
        raise DomainQueryError("unsupported_operation", f"unsupported bridge command: {args.command}")

    event = append_event(
        store.root_dir,
        event_type="pi.domain.read",
        payload={
            "request_id": args.request_id,
            "operation": payload["operation"],
            "result_count": _result_count(payload["data"]),
        },
    )
    payload["meta"] = {"audit_event_id": event["event_id"]}
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        payload = execute_command(args)
        exit_code = 0
    except (DomainQueryError, MutationError) as exc:
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "ok": False,
            "operation": str(getattr(args, "command", "unknown")).replace("-", "."),
            "error": {
                "code": exc.code,
                "type": type(exc).__name__,
                "message": str(exc),
                "details": exc.details,
            },
        }
        exit_code = 1
    except Exception as exc:  # The bridge must always return a machine-readable failure.
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "ok": False,
            "operation": str(getattr(args, "command", "unknown")).replace("-", "."),
            "error": {
                "code": "internal_error",
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
        exit_code = 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


def _success(operation: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "ok": True,
        "operation": operation,
        "data": data,
    }


def _success_with_project_knowledge(
    operation: str,
    data: dict[str, Any],
    store: ScholarFlowStore,
) -> dict[str, Any]:
    """Persist derived project snapshots after any confirmed Pi-domain write.

    The bridge is a short-lived process, so it only creates the durable queue
    entry here. A resident Workbench worker or an explicit retry performs
    LightRAG ingestion. Snapshot failures never roll back the confirmed domain
    mutation.
    """

    result = dict(data)
    try:
        service = ProjectKnowledgeService(store, background=False)
        scheduled = service.schedule(trigger=f"bridge_applied:{operation}")
        result["project_knowledge"] = {
            "automatic": True,
            "scheduled": scheduled.get("scheduled", []),
            "counts": scheduled.get("counts", {}),
            "status": "queued",
        }
    except Exception as exc:
        append_event(
            store.root_dir,
            event_type="knowledge.project.schedule.failed",
            payload={"operation": operation, "error": str(exc)[:1_000]},
        )
        result["project_knowledge"] = {
            "automatic": True,
            "scheduled": [],
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}"[:1_000],
        }
    return _success(operation, result)


def _describe_bridge_operations() -> dict[str, Any]:
    parser = build_parser()
    subparsers_action = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    operations: list[dict[str, Any]] = []
    for command, command_parser in sorted(subparsers_action.choices.items()):
        parameters = []
        for action in command_parser._actions:
            if action.dest == "help":
                continue
            parameters.append(
                {
                    "name": action.dest,
                    "flags": list(action.option_strings),
                    "required": bool(getattr(action, "required", False)),
                    "choices": list(action.choices) if action.choices is not None else None,
                    "repeated": isinstance(action, argparse._AppendAction),
                }
            )
        effect = "confirmed_write" if "apply" in command or command == "gateway-message-ack" else "proposal" if "propose" in command else "read"
        operations.append(
            {
                "command": command,
                "description": command_parser.description or command_parser.prog,
                "effect": effect,
                "parameters": parameters,
            }
        )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "transport": "json-over-stdio",
        "operation_count": len(operations),
        "operations": operations,
    }


def _add_search_arguments(parser: argparse.ArgumentParser, *, with_project_stage: bool = False) -> None:
    parser.add_argument("--query", default="")
    parser.add_argument("--status", default=None)
    parser.add_argument("--limit", type=int, default=20)
    if with_project_stage:
        parser.add_argument("--project-stage-id", default=None)


def _result_count(data: dict[str, Any]) -> int:
    if "count" in data:
        return int(data["count"])
    return 1


def _parse_changes(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MutationError("invalid_argument", "changes-json must contain valid JSON") from exc
    if not isinstance(payload, dict):
        raise MutationError("invalid_argument", "changes-json must contain a JSON object")
    return payload


def _parse_tags(value: str) -> list[str]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MutationError("invalid_argument", "tags-json must contain a valid JSON array") from exc
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise MutationError("invalid_argument", "tags-json must contain a JSON array of strings")
    return payload


def _add_mutation_arguments(subparsers: Any, object_name: str, id_flag: str, id_dest: str, *, operation: str = "update") -> None:
    propose = subparsers.add_parser(f"{object_name}-propose-{operation}")
    propose.add_argument(f"--{id_flag}", dest=id_dest, required=True)
    propose.add_argument("--changes-json", required=True)
    get_parser = subparsers.add_parser(f"{object_name}-proposal-get")
    get_parser.add_argument("--proposal-id", required=True)
    apply_parser = subparsers.add_parser(f"{object_name}-apply-{operation}")
    _add_apply_arguments(apply_parser)


def _add_apply_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--proposal-id", required=True)
    parser.add_argument("--confirmed", action="store_true")
    parser.add_argument("--confirmed-by", default=None)
    parser.add_argument("--confirmed-at", default=None)


def _confirmation(args: argparse.Namespace) -> dict[str, Any]:
    return {"proposal_id": args.proposal_id, "confirmed": args.confirmed, "confirmed_by": args.confirmed_by, "confirmed_at": args.confirmed_at, "pi_tool_call_id": args.request_id}


if __name__ == "__main__":
    raise SystemExit(main())
