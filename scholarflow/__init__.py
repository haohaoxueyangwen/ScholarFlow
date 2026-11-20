"""ScholarFlow deterministic domain and Pi-native control-plane API."""

from __future__ import annotations

from .confirmation_store import (
    approve_pairing,
    list_pairings,
    request_pairing,
)
from .domain_queries import DomainQueryError, ScholarFlowQueryService
from .enums import (
    ExperimentStatus,
    KnowledgeRelationType,
    PaperStatus,
    ProjectStageStatus,
    ResearchArtifactKind,
    ResearchArtifactStatus,
    RiskItemStatus,
    TaskStatus,
)
from .event_bus import append_event, list_events
from .exporters import export_paper_markdown
from .gateway import (
    acknowledge_gateway_message,
    approve_gateway_pairing,
    enqueue_workflow_request,
    gateway_status,
    get_gateway_message,
    list_gateway_inbox,
    poll_gateway_inbox_once,
    receive_gateway_input,
    start_gateway,
    stop_gateway,
)
from .intake import IntakeService
from .kb_constitution import plan_kb_write, write_kb_note
from .knowledge_engine import (
    LIGHTRAG_PINNED_VERSION,
    DisabledKnowledgeEngine,
    KnowledgeEngineConfig,
    KnowledgeSource,
    KnowledgeSourceMap,
    LightRAGClient,
    build_knowledge_engine,
    prepare_knowledge_bundle,
)
from .knowledge_candidates import CandidateKnowledgeService
from .contracts import KBWriteRequest
from .evidence import EvidenceBundle, EvidenceSource, READ_ONLY_RULE, prepare_evidence_bundle
from .models import DailySummary, Experiment, IntakeItem, KnowledgeRelation, Paper, ProjectStage, ResearchArtifact, RiskItem, Task
from .memory_audit import audit_memory_content, discover_memory_files
from .obsidian_sync import sync_object_to_vault
from .paper_service import PaperService
from .quality import audit_ecosystem_manifest, audit_knowledge_engine_manifest, audit_pi_resources, detect_stale_experiments, quality_status, workflow_health_summary
from .pi_rpc_host import PiRpcConfig, PiRpcHostError, PiRpcSupervisor, acknowledge_pi_rpc_escalation, pi_rpc_status
from .research_workflow import build_topic_mind_map, list_research_perspectives, write_topic_mind_map
from .retrieval import (
    EvidenceRetriever,
    HashingEmbeddingProvider,
    HybridEvidenceRetriever,
    InMemoryEmbeddingIndex,
    LexicalEvidenceRetriever,
    RetrievalCandidate,
    VectorHit,
)
from .retrieval_evaluation import RetrievalCase, evaluate_retriever
from .runtime_supervisor import (
    get_pi_host_status,
    pause_runtime_supervisor,
    resume_runtime_supervisor,
    run_runtime_supervisor_loop,
    run_runtime_supervisor_once,
    runtime_doctor,
    runtime_health_summary,
    runtime_supervisor_status,
    runtime_trend_report,
    start_runtime_supervisor,
    stop_runtime_supervisor,
)
from .scheduler import list_automation_jobs, run_due_jobs, schedule_default_jobs
from .scientific_document import (
    GrobidScientificPdfParser,
    NativeScientificPdfParser,
    ScientificBlock,
    ScientificCitation,
    ScientificDocument,
    ScientificPdfParserRouter,
    TesseractScientificPdfParser,
    ScientificReference,
    build_scientific_pdf_parser,
    parse_scientific_pdf,
)
from .storage import JsonEntityStore, ScholarFlowStore
from .vault_automation import append_daily_digest, maintain_vault_views, resolve_journal_path, run_kb_backfill
from .zotero_client import NullZoteroClient, ZoteroClientResult


__all__ = [
    "DomainQueryError",
    "ScholarFlowQueryService",
    "ScholarFlowStore",
    "JsonEntityStore",
    "DailySummary",
    "EvidenceBundle",
    "EvidenceSource",
    "EvidenceRetriever",
    "HashingEmbeddingProvider",
    "HybridEvidenceRetriever",
    "InMemoryEmbeddingIndex",
    "LexicalEvidenceRetriever",
    "Experiment",
    "IntakeItem",
    "KnowledgeRelation",
    "KnowledgeEngineConfig",
    "CandidateKnowledgeService",
    "KnowledgeSource",
    "KnowledgeSourceMap",
    "LightRAGClient",
    "DisabledKnowledgeEngine",
    "LIGHTRAG_PINNED_VERSION",
    "KBWriteRequest",
    "NullZoteroClient",
    "Paper",
    "PaperService",
    "PiRpcConfig",
    "PiRpcHostError",
    "PiRpcSupervisor",
    "ProjectStage",
    "ResearchArtifact",
    "RetrievalCandidate",
    "RetrievalCase",
    "RiskItem",
    "ScientificBlock",
    "ScientificCitation",
    "ScientificDocument",
    "ScientificPdfParserRouter",
    "TesseractScientificPdfParser",
    "ScientificReference",
    "NativeScientificPdfParser",
    "GrobidScientificPdfParser",
    "Task",
    "ExperimentStatus",
    "KnowledgeRelationType",
    "PaperStatus",
    "ProjectStageStatus",
    "ResearchArtifactKind",
    "ResearchArtifactStatus",
    "RiskItemStatus",
    "TaskStatus",
    "VectorHit",
    "evaluate_retriever",
    "ZoteroClientResult",
    "acknowledge_gateway_message",
    "approve_gateway_pairing",
    "approve_pairing",
    "append_event",
    "append_daily_digest",
    "audit_pi_resources",
    "audit_ecosystem_manifest",
    "audit_knowledge_engine_manifest",
    "audit_memory_content",
    "build_topic_mind_map",
    "build_scientific_pdf_parser",
    "build_knowledge_engine",
    "enqueue_workflow_request",
    "export_paper_markdown",
    "detect_stale_experiments",
    "discover_memory_files",
    "gateway_status",
    "get_pi_host_status",
    "acknowledge_pi_rpc_escalation",
    "get_gateway_message",
    "IntakeService",
    "list_events",
    "list_automation_jobs",
    "list_gateway_inbox",
    "list_pairings",
    "list_research_perspectives",
    "maintain_vault_views",
    "pause_runtime_supervisor",
    "parse_scientific_pdf",
    "plan_kb_write",
    "quality_status",
    "READ_ONLY_RULE",
    "poll_gateway_inbox_once",
    "prepare_evidence_bundle",
    "prepare_knowledge_bundle",
    "pi_rpc_status",
    "receive_gateway_input",
    "request_pairing",
    "resolve_journal_path",
    "resume_runtime_supervisor",
    "run_due_jobs",
    "run_kb_backfill",
    "run_runtime_supervisor_loop",
    "run_runtime_supervisor_once",
    "runtime_doctor",
    "runtime_health_summary",
    "runtime_supervisor_status",
    "runtime_trend_report",
    "schedule_default_jobs",
    "start_gateway",
    "start_runtime_supervisor",
    "stop_gateway",
    "stop_runtime_supervisor",
    "sync_object_to_vault",
    "write_kb_note",
    "write_topic_mind_map",
    "workflow_health_summary",
]
