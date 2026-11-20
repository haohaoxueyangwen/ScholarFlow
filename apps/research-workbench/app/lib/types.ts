export type Item = Record<string, unknown>;

export type ConnectionState = "loading" | "connected" | "demo" | "offline";

export type Dashboard = {
  source: string;
  generated_at: string;
  summary: {
    active_projects: number;
    open_tasks: number;
    papers: number;
    running_experiments: number;
    pending_candidates: number;
    pending_proposals: number;
  };
  projects: Item[];
  focus_tasks: Item[];
  recent_papers: Item[];
  experiments: Item[];
  artifacts: Item[];
  relations: Item[];
  candidates: Item[];
  proposals: Item[];
  proposal_receipts: Item[];
  runtime: Item;
};

export type ProjectWorkspaceData = {
  schema_version: number;
  generated_at: string;
  read_only: boolean;
  project: Item;
  summary: {
    task_count: number;
    open_task_count: number;
    blocked_task_count: number;
    paper_count: number;
    experiment_count: number;
    running_experiment_count: number;
    artifact_count: number;
    draft_count: number;
    pending_proposal_count: number;
  };
  framing: {
    title?: unknown;
    goal?: unknown;
    risks: unknown[];
    next_actions: unknown[];
    evidence_refs: unknown[];
  };
  tasks: Item[];
  papers: Item[];
  experiments: Item[];
  artifacts: Item[];
  evidence: Item & { items?: Item[]; evidence_refs?: unknown[] };
  pending_proposals: Item[];
  history: Item[];
};

export type PaperReaderData = {
  schema_version: number;
  read_only: boolean;
  available: boolean;
  reason?: string;
  paper: Item;
  source?: Item;
  sources: Item[];
  pdf?: {
    available: boolean;
    reason?: string;
    source?: string;
    size_bytes?: number;
  };
  document?: {
    schema_version?: number;
    document_id?: string;
    title?: string;
    authors: unknown[];
    content_hash?: string;
    page_count: number;
    parser: Item;
    metadata: Item;
    warnings: unknown[];
    block_count: number;
    blocks: Item[];
    references: Item[];
    citations: Item[];
    toc: Item[];
    offset: number;
    limit: number;
    has_more: boolean;
  };
};

export type LiteratureResult = {
  provider: "openalex" | "crossref" | string;
  result_id: string;
  provider_url: string;
  openalex_id?: string;
  openalex_url?: string;
  title: string;
  authors: string[];
  year?: number | null;
  venue?: string;
  doi?: string;
  type?: string;
  cited_by_count: number;
  abstract: string;
  open_access: { is_oa: boolean; status?: string };
  primary_url: string;
  pdf_url?: string;
  in_library: boolean;
};

export type LiteratureSearchData = {
  pagination?: { provider?: string; page: number; page_size?: number; total_matches?: number | null; has_more: boolean; complete_snapshot: boolean; filter_scope: string; author_matching?: string };
  schema_version: number;
  generated_at: string;
  read_only: boolean;
  provider: string;
  query: string;
  count: number;
  items: LiteratureResult[];
};

export type ReaderImport = {
  schema_version: number;
  import_id: string;
  filename: string;
  size_bytes: number;
  content_hash: string;
  created_at: string;
  updated_at: string;
  status: "parsed" | "staged" | "parsed_stale";
  parse_error?: string;
  title: string;
  authors: string[];
  page_count: number;
  reference_count: number;
  origin?: Record<string, string>;
  origins?: Array<Record<string, string>>;
  document_ref?: string;
  project_stage_ids?: string[];
  previous_parse_available?: boolean;
};

export type PdfTextAnchor = {
  kind: "pdf_text_range_v1";
  document_fingerprint: string;
  page: number;
  start: { item_index: number; offset: number };
  end: { item_index: number; offset: number };
  rects: Array<{ x: number; y: number; width: number; height: number }>;
};

export type ReaderAnnotation = {
  anchor?: PdfTextAnchor | null;
  annotation_id: string;
  target_ref: string;
  page: number;
  quote: string;
  note: string;
  color: "yellow" | "mint" | "blue" | "rose";
  created_at: string;
  updated_at: string;
  user_authored: true;
  authoritative: false;
};

export type CapabilityReport = {
  generated_at: string;
  summary: {
    pi_version?: string;
    skill_count: number;
    fixture_count: number;
    tool_count?: number;
    extension_count: number;
    latest_live_status: string;
  };
  skills: string[];
  extensions: Item[];
  groups: Array<{
    group_id: string;
    title: string;
    status: string;
    items: string[];
  }>;
  validation: {
    status: string;
    provider?: string;
    model?: string;
    selection_verified: boolean;
    metrics: Item;
    report_name?: string;
    updated_at?: string;
  };
};

export type AgentSession = {
  session_id: string;
  status: string;
  alive?: boolean;
  resumable?: boolean;
  updated_at?: string;
  name?: string;
  scope?: "global" | "project" | "task";
  project_id?: string | null;
  parent_session_id?: string | null;
  title?: string;
  objective?: string;
  latest_checkpoint?: AgentCheckpoint | null;
  resume_cursor?: AgentResumeCursor;
  latest_context_manifest?: AgentContextManifest | null;
  latest_observation?: AgentObservation | null;
};

export type AgentObservation = {
  schema_version: number;
  observation_id: string;
  session_id: string;
  request_id: string;
  created_at: string;
  status: "pass" | "warn" | "fail";
  outcome: {
    completion: "completed" | "failed" | "unfinished";
    final_answer: {
      present: boolean;
      character_count: number;
      estimated_tokens: number;
    };
    final_state: {
      status: string;
      changed_files?: string[];
    };
  };
  trajectory: {
    tool_call_count: number;
    tool_error_count: number;
    confirmation_approved: number;
    confirmed_apply_count: number;
    retrieval_call_count: number;
    compaction_count: number;
    delegated_worker_count?: number;
    delegated_worker_success_count?: number;
    delegated_worker_failure_count?: number;
    delegated_unique_source_count?: number;
  };
  cost: {
    latency_ms?: number | null;
    total_tokens: number;
    reported_cost_total: number;
    delegated_workers?: {
      worker_count: number;
      successful_worker_count: number;
      failed_worker_count: number;
      unique_source_count: number;
      usage: { totalTokens?: number; cost?: number };
    };
  };
  signals: Array<{
    code: string;
    severity: "warning" | "error";
    message: string;
  }>;
};

export type AgentResumeCursor = {
  schema_version: number;
  session_id: string;
  sequence: number;
  event_id?: string | null;
  recorded_at?: string | null;
};

export type AgentContextManifest = {
  schema_version: number;
  manifest_id: string;
  session_id: string;
  request_id: string;
  policy_version: string;
  created_at: string;
  budget: {
    estimator: string;
    context_window_tokens: number;
    compaction_trigger_tokens: number;
    response_reserve_tokens: number;
    keep_recent_tokens: number;
    estimate_scope: "new_prompt_only";
    estimated_new_prompt_tokens: number;
  };
  route?: {
    policy_version: string;
    workload: "factual_query" | "sequential_decision" | "project_review" | "cross_project_synthesis" | "open_research";
    confidence: number;
    working_set_target_tokens: number;
    checkpoint_policy: {
      max_items: number;
      max_characters: number;
    };
  };
  sources: Item[];
  excluded_sources?: Item[];
};

export type AgentCheckpoint = {
  checkpoint_id: string;
  session_id: string;
  scope: "global" | "project" | "task";
  project_id?: string | null;
  title: string;
  summary: string;
  decisions?: string[];
  open_questions?: string[];
  next_actions?: string[];
  evidence_refs?: string[];
  automatic?: boolean;
  created_at: string;
};

export type AgentEvent = Item & {
  schema_version: number;
  event_id: string;
  session_id: string;
  type: string;
  sequence: number;
  recorded_at: string;
  source: "workbench" | "pi";
};

export type ConversationItem = {
  sequence?: number;
  id: string;
  kind: "user" | "assistant" | "tool" | "error";
  text: string;
  detail?: string;
  pending?: boolean;
};

export type ConversationPage = { items: ConversationItem[]; has_more: boolean; before_sequence: number; through_sequence: number; total: number };

export type ContextSelection = {
  ref: string;
  label: string;
  kind: string;
};

export type ResourceConfig = {
  route: string;
  idField: string;
  titleField: string;
  contextType: string;
  eyebrow: string;
  title: string;
  description: string;
};
