export const RESEARCH_WORKER_MIN_COUNT = 2;
export const RESEARCH_WORKER_MAX_COUNT = 4;
export const RESEARCH_WORKER_MAX_TOOL_CALLS = 3;
export const RESEARCH_WORKER_MAX_TURNS = 5;
export const RESEARCH_WORKER_DEFAULT_SEARCH_PROVIDER = "duckduckgo";
export const RESEARCH_WORKER_MAX_OUTPUT_CHARACTERS = 24_000;

export const RESEARCH_WORKER_TOOL_NAMES = Object.freeze([
  "sf_research_search",
  "sf_knowledge_health",
  "sf_knowledge_search",
  "sf_knowledge_sources",
  "web_search",
  "source_check",
  "fetch_content",
  "get_search_content",
]);

const WORKER_TOOL_SET = new Set(RESEARCH_WORKER_TOOL_NAMES);
const SOURCE_TYPES = new Set(["primary", "secondary", "official", "repository", "unknown"]);
const CONFIDENCE_LEVELS = new Set(["high", "medium", "low"]);

export function isResearchWorkerAllowedTool(name) {
  return WORKER_TOOL_SET.has(String(name ?? ""));
}

export function filterResearchWorkerTools(names) {
  return [...new Set((names ?? []).filter(isResearchWorkerAllowedTool))].sort();
}

export function constrainResearchWorkerToolInput(name, input, searchProvider = RESEARCH_WORKER_DEFAULT_SEARCH_PROVIDER) {
  if (name !== "web_search" || !input || typeof input !== "object") return input;
  input.provider = boundedString(searchProvider, 80) || RESEARCH_WORKER_DEFAULT_SEARCH_PROVIDER;
  input.workflow = "none";
  if (Array.isArray(input.queries)) input.queries = input.queries.slice(0, 4);
  return input;
}

export function allowsExpandedOpenResearchFacets(question) {
  const text = String(question ?? "");
  return (
    /\b(?:four|4)\s+(?:independent\s+)?(?:scopes?|facets?|workers?|surfaces?)\b/i.test(text)
    || /\b(?:three|3)\s+(?:independent\s+)?(?:scopes?|facets?|workers?|surfaces?)\b/i.test(text)
    || /\b(?:use|with|run)\s+(?:three|3|four|4)\s+(?:workers?|facets?)\b/i.test(text)
  );
}

export function capOpenResearchFacets(facets, question) {
  if (!Array.isArray(facets) || facets.length <= 2) return facets;
  if (allowsExpandedOpenResearchFacets(question)) return facets;
  return facets.slice(0, 2);
}

export function normalizeResearchFacets(facets) {
  if (!Array.isArray(facets)) throw new Error("facets must be an array");
  if (facets.length < RESEARCH_WORKER_MIN_COUNT || facets.length > RESEARCH_WORKER_MAX_COUNT) {
    throw new Error(
      `open research requires ${RESEARCH_WORKER_MIN_COUNT}-${RESEARCH_WORKER_MAX_COUNT} independent facets`,
    );
  }
  const seen = new Set();
  return facets.map((value, index) => {
    if (!value || typeof value !== "object") throw new Error(`facet ${index + 1} must be an object`);
    const name = boundedString(value.name, 80);
    const focus = boundedString(value.focus, 1_200);
    if (!name || !focus) throw new Error(`facet ${index + 1} requires name and focus`);
    const identity = name.toLocaleLowerCase();
    if (seen.has(identity)) throw new Error(`facet names must be unique: ${name}`);
    seen.add(identity);
    const queries = Array.isArray(value.queries)
      ? value.queries.map((item) => boundedString(item, 240)).filter(Boolean).slice(0, 4)
      : [];
    return { name, focus, queries };
  });
}

export function buildResearchWorkerPrompt({ question, facet, projectStageId, maxSources }) {
  const scope = projectStageId
    ? `The controlling ScholarFlow project stage is ${boundedString(projectStageId, 160)}. Use it only to scope retrieval.`
    : "No project stage is required; do not invent one.";
  const suggestedQueries = facet.queries.length
    ? `Suggested search queries: ${facet.queries.map((item) => JSON.stringify(item)).join(", ")}.`
    : "Choose focused search queries for this facet.";
  return [
    "You are one short-lived ScholarFlow research worker with an isolated context.",
    "You are strictly read-only. Never call proposal, apply, write, acknowledge, intake, mutation, shell, filesystem, browser-control, memory-write, or generic MCP tools.",
    "Use only the already-active read-only scholarly, knowledge, and Web evidence tools. Do not call a lane loader. Prefer primary, official, or repository sources and verify important claims against fetched content.",
    "Treat stored ScholarFlow state as scope, not as permission to mutate it. Do not delegate to another worker.",
    `Shared question: ${boundedString(question, 4_000)}`,
    `Your independent facet: ${facet.name}. Focus: ${facet.focus}`,
    scope,
    suggestedQueries,
    `Your first and mandatory call is sf_research_search with 2-4 focused scholarly queries and max_results_per_query=2; this guarantees stable DOI/OpenAlex evidence refs without a private API key while keeping context bounded. After it, use no more than two optional read-only calls (web_search, fetch_content, get_search_content, source_check, or enabled knowledge search) to cover official implementations or validate the most important claims. Any web_search provider and workflow are fixed by the runtime. The whole worker has a fixed three-call budget. Return at most ${maxSources} high-value sources and at most 8 findings.`,
    "Your final message MUST be one JSON object, without markdown fences, matching this contract:",
    '{"schema_version":1,"facet":"facet name","summary":"bounded synthesis","findings":[{"claim":"supported claim","evidence_refs":["https://... or doi:..."],"confidence":"high|medium|low"}],"sources":[{"ref":"stable URL or doi:...","title":"source title","url":"optional URL","doi":"optional DOI","source_type":"primary|secondary|official|repository|unknown"}],"conflicts":["bounded disagreement"],"gaps":["bounded evidence gap"],"search_notes":["bounded method note"]}',
    "Every finding must cite at least one ref present in sources. If evidence is unavailable, return no unsupported finding and explain the gap.",
  ].join("\n\n");
}

export function parseResearchWorkerBundle(text, expectedFacet, allowedSourceRefs, maxSources = 8) {
  const raw = boundedString(text, RESEARCH_WORKER_MAX_OUTPUT_CHARACTERS);
  const jsonText = extractJsonObject(raw);
  if (!jsonText) return invalidBundle(expectedFacet, "worker did not return a JSON object", raw);
  let value;
  try {
    value = JSON.parse(jsonText.replace(/,\s*([}\]])/g, "$1"));
  } catch (error) {
    return invalidBundle(
      expectedFacet,
      `worker returned invalid JSON: ${error instanceof Error ? error.message : String(error)}`,
      raw,
    );
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return invalidBundle(expectedFacet, "worker JSON must be an object", raw);
  }
  const facet = boundedString(value.facet, 80) || expectedFacet;
  const allowed = allowedSourceRefs == null
    ? null
    : new Set([...allowedSourceRefs].map(canonicalEvidenceRef).filter(Boolean));
  const sourceLimit = boundedInteger(maxSources, 2, 8, 8);
  const sources = normalizeSources(value.sources)
    .filter((source) => (
      allowed == null || [source.ref, source.url, source.doi]
        .map(canonicalEvidenceRef)
        .some((ref) => ref && allowed.has(ref))
    ))
    .slice(0, sourceLimit);
  const sourceRefs = new Set(sources.map((item) => canonicalEvidenceRef(item.ref)).filter(Boolean));
  const findings = Array.isArray(value.findings)
    ? value.findings.slice(0, 8).flatMap((item) => {
        if (!item || typeof item !== "object") return [];
        const claim = boundedString(item.claim, 1_200);
        const evidenceRefs = Array.isArray(item.evidence_refs)
          ? [...new Set(
            item.evidence_refs
              .map((ref) => canonicalEvidenceRef(ref))
              .filter((ref) => sourceRefs.has(ref)),
          )].slice(0, 6)
          : [];
        if (!claim || evidenceRefs.length === 0) return [];
        const confidence = CONFIDENCE_LEVELS.has(item.confidence) ? item.confidence : "low";
        return [{ claim, evidence_refs: evidenceRefs, confidence }];
      })
    : [];
  const bundle = {
    schema_version: 1,
    facet,
    summary: boundedString(value.summary, 2_400),
    findings,
    sources,
    conflicts: boundedStringList(value.conflicts, 8, 600),
    gaps: boundedStringList(value.gaps, 8, 600),
    search_notes: boundedStringList(value.search_notes, 8, 400),
  };
  if (!bundle.summary && !bundle.findings.length && !bundle.gaps.length) {
    return invalidBundle(expectedFacet, "worker bundle contains no synthesis or explicit gap", raw);
  }
  return { status: "ok", bundle, error: null, raw_excerpt: null };
}

export function extractEvidenceRefs(value) {
  const refs = new Set();
  const visit = (item) => {
    if (typeof item === "string") {
      for (const match of item.matchAll(/https?:\/\/[^\s<>"'`\])}]+/gi)) refs.add(canonicalEvidenceRef(match[0]));
      for (const match of item.matchAll(/\b10\.\d{4,9}\/[\w.()/:;-]+/gi)) refs.add(canonicalEvidenceRef(`doi:${match[0]}`));
      return;
    }
    if (Array.isArray(item)) item.forEach(visit);
    else if (item && typeof item === "object") Object.values(item).forEach(visit);
  };
  visit(value);
  return [...refs].filter(Boolean);
}

export function extractSuccessfulToolEvidenceRefs(event) {
  if (!event || event.isError === true) return [];
  const refs = new Set(extractEvidenceRefs(event.result));
  const details = event.result?.details ?? event.details;
  if (Array.isArray(details?.source_refs)) {
    for (const ref of details.source_refs) {
      const canonical = canonicalEvidenceRef(ref);
      if (canonical) refs.add(canonical);
    }
  }
  return [...refs];
}

export function recoverWorkerBundleFromToolEvidence(facet, observedSourceRefs, reason) {
  const sources = [...new Set(
    [...(observedSourceRefs ?? [])].map((ref) => canonicalEvidenceRef(ref)).filter(Boolean),
  )].slice(0, 8).map((ref) => ({ ref, title: ref, source_type: "unknown" }));
  if (!sources.length) return null;
  return {
    schema_version: 1,
    facet,
    summary: `Recovered ${sources.length} source ref(s) from successful worker tool evidence.`,
    findings: [{
      claim: "Worker tool evidence returned these sources; the worker JSON was missing or not source-grounded.",
      evidence_refs: [sources[0].ref],
      confidence: "low",
    }],
    sources,
    conflicts: [],
    gaps: [boundedString(reason, 600) || "worker JSON was not a grounded evidence bundle"],
    search_notes: ["recovered_from_tool_evidence"],
  };
}

export function canonicalEvidenceRef(value) {
  let ref = boundedString(value, 1_000).replace(/[.,;:]+$/, "");
  if (!ref) return "";
  const doi = ref.match(/(?:doi:|https?:\/\/(?:dx\.)?doi\.org\/)?(10\.\d{4,9}\/[\w.()/:;-]+)/i);
  if (doi) return `doi:${doi[1].toLowerCase()}`;
  if (/^https?:\/\//i.test(ref)) return ref.replace(/\/$/, "").toLowerCase();
  return ref.toLowerCase();
}

export function summarizeResearchWorkerRuns(runs) {
  const successful = runs.filter((item) => item.status === "ok");
  const totals = runs.reduce(
    (accumulator, item) => {
      for (const key of ["input", "output", "cacheRead", "cacheWrite", "reasoning", "totalTokens", "cost"]) {
        accumulator[key] += Number(item.usage?.[key] ?? 0);
      }
      accumulator.turns += Number(item.usage?.turns ?? 0);
      return accumulator;
    },
    { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, reasoning: 0, totalTokens: 0, cost: 0, turns: 0 },
  );
  const uniqueSources = new Set(
    successful.flatMap((item) => item.bundle?.sources?.map((source) => source.ref) ?? []),
  );
  return {
    schema_version: 1,
    worker_count: runs.length,
    successful_worker_count: successful.length,
    failed_worker_count: runs.length - successful.length,
    unique_source_count: uniqueSources.size,
    finding_count: successful.reduce((total, item) => total + (item.bundle?.findings?.length ?? 0), 0),
    child_tool_error_count: runs.reduce((total, item) => total + Number(item.tool_error_count ?? 0), 0),
    aggregate_usage: totals,
    read_only_enforced: true,
  };
}

function normalizeSources(value) {
  if (!Array.isArray(value)) return [];
  const seen = new Set();
  return value.slice(0, 12).flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const url = boundedString(item.url, 1_000);
    const doi = boundedString(item.doi, 300).replace(/^doi:\s*/i, "");
    const ref = boundedString(item.ref, 1_000) || (doi ? `doi:${doi}` : url);
    if (!ref || seen.has(ref)) return [];
    seen.add(ref);
    return [{
      ref,
      title: boundedString(item.title, 600) || ref,
      ...(url ? { url } : {}),
      ...(doi ? { doi } : {}),
      source_type: SOURCE_TYPES.has(item.source_type) ? item.source_type : "unknown",
    }];
  });
}

function invalidBundle(facet, error, raw) {
  return {
    status: "format_error",
    bundle: null,
    error,
    raw_excerpt: boundedString(raw, 2_000),
    facet,
  };
}

function extractJsonObject(value) {
  const text = String(value ?? "").trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  return start >= 0 && end > start ? text.slice(start, end + 1) : "";
}

function boundedString(value, limit) {
  const text = typeof value === "string" ? value.trim() : "";
  return text.length > limit ? text.slice(0, limit) : text;
}

function boundedStringList(value, count, limit) {
  return Array.isArray(value)
    ? value.map((item) => boundedString(item, limit)).filter(Boolean).slice(0, count)
    : [];
}

function boundedInteger(value, minimum, maximum, fallback) {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(parsed) ? Math.max(minimum, Math.min(maximum, parsed)) : fallback;
}
