import assert from "node:assert/strict";
import test from "node:test";

import {
  RESEARCH_WORKER_MAX_COUNT,
  RESEARCH_WORKER_MAX_TOOL_CALLS,
  RESEARCH_WORKER_MAX_TURNS,
  RESEARCH_WORKER_DEFAULT_SEARCH_PROVIDER,
  RESEARCH_WORKER_MIN_COUNT,
  buildResearchWorkerPrompt,
  constrainResearchWorkerToolInput,
  extractEvidenceRefs,
  extractSuccessfulToolEvidenceRefs,
  filterResearchWorkerTools,
  isResearchWorkerAllowedTool,
  normalizeResearchFacets,
  capOpenResearchFacets,
  parseResearchWorkerBundle,
  recoverWorkerBundleFromToolEvidence,
  summarizeResearchWorkerRuns,
} from "../extensions/research-worker-contract.js";

test("research worker tool boundary permits evidence reads and blocks every mutation surface", () => {
  assert.equal(isResearchWorkerAllowedTool("web_search"), true);
  assert.equal(isResearchWorkerAllowedTool("sf_research_search"), true);
  assert.equal(isResearchWorkerAllowedTool("sf_knowledge_search"), true);
  for (const forbidden of [
    "sf_artifact_propose_publish",
    "sf_project_apply_update",
    "sf_zotero_tag_apply",
    "sf_kb_apply_write",
    "sf_inbox_ack",
    "mcp",
    "mcpScript",
    "bash",
    "write",
    "memory",
    "sf_lane_enter",
  ]) {
    assert.equal(isResearchWorkerAllowedTool(forbidden), false, forbidden);
  }
  assert.deepEqual(
    filterResearchWorkerTools(["sf_knowledge_search", "write", "web_search", "sf_knowledge_search"]),
    ["sf_knowledge_search", "web_search"],
  );
});

test("research worker search input is deterministic and headless", () => {
  assert.equal(RESEARCH_WORKER_DEFAULT_SEARCH_PROVIDER, "duckduckgo");
  const input = { provider: "exa", workflow: "summary-review", queries: ["a", "b", "c", "d", "e"] };
  constrainResearchWorkerToolInput("web_search", input);
  assert.deepEqual(input, { provider: "duckduckgo", workflow: "none", queries: ["a", "b", "c", "d"] });
});

test("research facets enforce two to four unique, bounded independent tasks", () => {
  assert.equal(RESEARCH_WORKER_MIN_COUNT, 2);
  assert.equal(RESEARCH_WORKER_MAX_COUNT, 4);
  assert.equal(RESEARCH_WORKER_MAX_TOOL_CALLS, 3);
  assert.equal(RESEARCH_WORKER_MAX_TURNS, 5);
  const facets = normalizeResearchFacets([
    { name: "Primary papers", focus: "Peer-reviewed evidence", queries: ["query one"] },
    { name: "Implementations", focus: "Maintained reference implementations" },
  ]);
  assert.equal(facets.length, 2);
  assert.throws(() => normalizeResearchFacets([facets[0]]), /requires 2-4/);
  assert.throws(() => normalizeResearchFacets([facets[0], { ...facets[0] }]), /unique/);
});

test("research worker prompt carries the process boundary and structured evidence contract", () => {
  const [facet] = normalizeResearchFacets([
    { name: "Primary papers", focus: "Peer-reviewed evidence" },
    { name: "Implementations", focus: "Reference code" },
  ]);
  const prompt = buildResearchWorkerPrompt({
    question: "What architecture is supported?",
    facet,
    projectStageId: "stage-fixture",
    maxSources: 5,
  });
  assert.match(prompt, /strictly read-only/i);
  assert.match(prompt, /stage-fixture/);
  assert.match(prompt, /final message MUST be one JSON object/);
  assert.match(prompt, /first and mandatory call is sf_research_search/);
  assert.match(prompt, /fixed three-call budget/);
});

test("open research caps to two facets unless the question names three or four scopes", () => {
  const three = [
    { name: "Primary papers", focus: "Peer-reviewed evidence" },
    { name: "Implementations", focus: "Reference code" },
    { name: "Failures", focus: "Failure reports" },
  ];
  assert.equal(capOpenResearchFacets(three, "Investigate durable context for research agents.").length, 2);
  assert.equal(
    capOpenResearchFacets(three, "Use three independent scopes: papers, implementations, and failures.").length,
    3,
  );
});

test("research worker bundle parser keeps only claims linked to returned sources", () => {
  const parsed = parseResearchWorkerBundle(
    "```json\n" + JSON.stringify({
      schema_version: 1,
      facet: "Primary papers",
      summary: "Two primary sources agree.",
      findings: [
        { claim: "Grounded", evidence_refs: ["doi:10.1000/one"], confidence: "high" },
        { claim: "Ungrounded", evidence_refs: ["https://missing.test"], confidence: "high" },
      ],
      sources: [{ ref: "doi:10.1000/one", title: "One", doi: "10.1000/one", source_type: "primary" }],
      conflicts: [],
      gaps: [],
      search_notes: [],
    }) + "\n```",
    "Primary papers",
  );
  assert.equal(parsed.status, "ok");
  assert.equal(parsed.bundle.findings.length, 1);
  assert.equal(parsed.bundle.findings[0].claim, "Grounded");
});

test("research worker bundle parser rejects source refs absent from tool evidence", () => {
  const value = JSON.stringify({
    facet: "Primary papers",
    summary: "One grounded and one invented source.",
    findings: [
      { claim: "Grounded", evidence_refs: ["doi:10.1000/one"] },
      { claim: "Invented", evidence_refs: ["doi:10.1000/two"] },
    ],
    sources: [
      { ref: "doi:10.1000/one", title: "One" },
      { ref: "doi:10.1000/two", title: "Two" },
    ],
  });
  const refs = extractEvidenceRefs({ details: { source_refs: ["doi:10.1000/one"] } });
  const parsed = parseResearchWorkerBundle(value, "Primary papers", refs);
  assert.equal(parsed.bundle.sources.length, 1);
  assert.equal(parsed.bundle.findings.length, 1);
  assert.equal(parsed.bundle.findings[0].claim, "Grounded");
});

test("failed worker tools cannot contribute evidence refs", () => {
  const result = { details: { source_refs: ["doi:10.1000/error-only"] } };
  assert.deepEqual(extractSuccessfulToolEvidenceRefs({ isError: true, result }), []);
  assert.deepEqual(
    extractSuccessfulToolEvidenceRefs({ isError: false, result }),
    ["doi:10.1000/error-only"],
  );
});

test("recovers a grounded bundle from successful tool refs when worker JSON is invalid", () => {
  const recovered = recoverWorkerBundleFromToolEvidence(
    "Primary papers",
    ["https://doi.org/10.1000/one", "doi:10.1000/one"],
    "worker did not return a JSON object",
  );
  assert.ok(recovered);
  assert.equal(recovered.facet, "Primary papers");
  assert.equal(recovered.findings.length, 1);
  assert.equal(recovered.sources[0].ref, "doi:10.1000/one");
});

test("research worker bundle parser enforces the declared per-worker source cap", () => {
  const sources = Array.from({ length: 6 }, (_, index) => ({
    ref: `doi:10.1000/${index}`,
    title: `Source ${index}`,
  }));
  const parsed = parseResearchWorkerBundle(JSON.stringify({
    facet: "Bounded facet",
    summary: "Bounded evidence.",
    findings: sources.map((source) => ({ claim: source.title, evidence_refs: [source.ref] })),
    sources,
  }), "Bounded facet", null, 3);
  assert.equal(parsed.bundle.sources.length, 3);
  assert.equal(parsed.bundle.findings.length, 3);
});

test("research worker summary exposes coverage and delegated cost without merging contexts", () => {
  const usage = { input: 100, output: 20, cacheRead: 0, cacheWrite: 0, reasoning: 0, totalTokens: 120, cost: 0.001, turns: 1 };
  const summary = summarizeResearchWorkerRuns([
    { status: "ok", usage, bundle: { findings: [{ claim: "a" }], sources: [{ ref: "doi:a" }] } },
    { status: "ok", usage, bundle: { findings: [{ claim: "b" }], sources: [{ ref: "doi:b" }, { ref: "doi:a" }] } },
    { status: "process_error", usage: {}, bundle: null },
  ]);
  assert.equal(summary.worker_count, 3);
  assert.equal(summary.successful_worker_count, 2);
  assert.equal(summary.unique_source_count, 2);
  assert.equal(summary.aggregate_usage.totalTokens, 240);
  assert.equal(summary.read_only_enforced, true);
});
