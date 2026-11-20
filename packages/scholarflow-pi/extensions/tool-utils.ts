import { Type } from "typebox";

export function queryParameter(description: string) {
  return Type.Optional(Type.String({ description }));
}

export function statusParameter(values: string) {
  return Type.Optional(Type.String({ description: `Optional status: ${values}.` }));
}

export function limitParameter() {
  return Type.Optional(Type.Integer({ description: "Maximum number of results.", minimum: 1, maximum: 100, default: 20 }));
}

export function relationPredicateParameter() {
  return Type.Union([
    Type.Literal("supports"), Type.Literal("contradicts"), Type.Literal("depends_on"),
    Type.Literal("derived_from"), Type.Literal("related_to"),
  ], { description: "Governed evidence relation predicate." });
}

export function addSearchArgs(
  command: string,
  params: { query?: string; status?: string; project_stage_id?: string; limit?: number; offset?: number },
): string[] {
  const args = [command, "--limit", String(params.limit ?? 20)];
  if (params.query?.trim()) args.push("--query", params.query.trim());
  if (params.status?.trim()) args.push("--status", params.status.trim());
  if (params.project_stage_id?.trim()) args.push("--project-stage-id", params.project_stage_id.trim());
  if (command === "project-search" && typeof params.offset === "number" && params.offset > 0) {
    args.push("--offset", String(params.offset));
  }
  return args;
}

export function definedObject(value: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(value).filter(([, item]) => item !== undefined));
}

export function omitBlankOptionalStrings(value: Record<string, unknown>, keys: string[]): Record<string, unknown> {
  return Object.fromEntries(Object.entries(definedObject(value)).filter(([key, item]) => (
    !keys.includes(key) || typeof item !== "string" || item.trim().length > 0
  )));
}

const LANE_PREFIXES: Record<string, string[]> = {
  paper: ["sf_paper_", "sf_project_", "sf_task_", "sf_experiment_"],
  intake: ["sf_intake_"],
  literature: [
    "sf_zotero_", "sf_paper_", "sf_project_", "sf_task_", "sf_experiment_",
    "sf_evidence_", "sf_knowledge_", "sf_artifact_", "sf_relation_", "sf_candidate_",
  ],
  writing: ["sf_manuscript_"],
  experiment: ["sf_experiment_", "sf_evidence_"],
  analysis: ["sf_experiment_", "sf_evidence_", "sf_artifact_", "sf_relation_", "sf_candidate_"],
  planning: ["sf_project_", "sf_task_", "sf_evidence_", "sf_knowledge_", "sf_artifact_", "sf_relation_", "sf_candidate_", "sf_research_"],
  review: ["sf_project_", "sf_task_", "sf_experiment_", "sf_paper_", "sf_evidence_", "sf_knowledge_", "sf_artifact_", "sf_relation_", "sf_candidate_"],
  automation: ["sf_inbox_", "sf_runtime_", "sf_quality_"],
};

const LANE_EXACT_TOOLS: Record<string, string[]> = {
  project: [
    "sf_project_search",
    "sf_project_get",
    "sf_paper_search",
    "sf_paper_get",
    "sf_project_propose_create",
    "sf_task_search",
    "sf_task_propose_create",
  ],
  experiment: ["sf_project_get"],
  analysis: ["sf_project_get", "sf_task_get", "sf_paper_get"],
  // Keep the optional paper-reference write available without exposing its
  // apply counterpart. The proposal remains subject to Pi confirmation.
  paper: ["sf_kb_propose_write", "sf_relation_search"],
  writing: [
    "sf_project_search",
    "sf_evidence_bundle",
    "sf_artifact_search",
    "sf_artifact_propose_publish",
  ],
  literature: ["sf_research_delegate"],
};

export function toolsForLane(lane: string, allNames: string[]): string[] {
  if (lane === "all") return [...allNames].sort();
  const prefixes = LANE_PREFIXES[lane] ?? [];
  const exact = LANE_EXACT_TOOLS[lane] ?? [];
  return allNames
    .filter((name) => name === "sf_lane_enter" || exact.includes(name) || prefixes.some((prefix) => name.startsWith(prefix)))
    .sort();
}
