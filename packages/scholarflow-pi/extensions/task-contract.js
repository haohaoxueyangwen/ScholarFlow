/**
 * Pi task contract four-exits v1 (P4.5).
 * Single manifest drives route text, tool allowlists, guard names, and fixture exits.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/** @typedef {import('./routing.js').classifyScholarFlowPrompt extends (...args: any) => infer R ? R : never} ScholarFlowRoute */

let cachedManifest;
let cachedBootstrapOutcomes;
let cachedInformationDuties;

function readJson(relativePath) {
  return JSON.parse(readFileSync(resolve(packageRoot, relativePath), "utf8"));
}

export function loadFourExitsManifest() {
  if (!cachedManifest) {
    cachedManifest = readJson("quality/pi-task-contract-four-exits-v1.json");
  }
  return cachedManifest;
}

export function loadBootstrapOutcomeContract() {
  if (!cachedBootstrapOutcomes) {
    cachedBootstrapOutcomes = readJson("quality/project-create-outcomes-v4.json");
  }
  return cachedBootstrapOutcomes;
}

export function loadInformationDutiesContract() {
  if (!cachedInformationDuties) {
    cachedInformationDuties = readJson("quality/information-duties-v1.json");
  }
  return cachedInformationDuties;
}

/**
 * @param {string} skillId
 */
export function skillContractBlock(skillId) {
  const manifest = loadFourExitsManifest();
  const block = manifest.skills?.[skillId];
  if (!block) {
    throw new Error(`Missing four-exits skill block: ${skillId}`);
  }
  return block;
}

/**
 * T4.5.1 — flow instructions from linked outcome contracts (not hand-maintained copies).
 * When resolutionMode is supplied, project_context instructions match the P4 parser mode.
 *
 * @param {"project_bootstrap"|"project_context"} routeKey
 * @param {{ readOnly?: boolean, resolutionMode?: string }} [options]
 */
export function routeDirectiveForSkill(routeKey, options = {}) {
  const manifest = loadFourExitsManifest();
  if (routeKey === "project_bootstrap") {
    const bootstrap = loadBootstrapOutcomeContract();
    const codes = bootstrap.outcomes.map((item) => item.code).join(", ");
    const summaries = bootstrap.outcomes
      .map((item) => `${item.code} (${item.summary})`)
      .join("; ");
    return (
      "SCHOLARFLOW PROJECT BOOTSTRAP ROUTE: First call sf_lane_enter with lane=\"project\". "
      + "Call sf_paper_search once with an empty query to list the bounded stored-paper set, "
      + "then call sf_project_search once with the proposed project name or topic "
      + "(never an empty overview query). "
      + `Choose exactly one ${bootstrap.contract_id} result: ${codes}. `
      + `Outcomes: ${summaries}. `
      + "Similar names or shared papers are clues only—compare goal, object, deliverables, and scope. "
      + "When multiple candidates appear, call sf_project_get with one exact listed stage_id to decide "
      + "reuse_existing versus create_proposal. "
      + "Even when an existing stage id is named as a non-duplication reference, still run one named "
      + "sf_project_search for duplicate detection before create. "
      + "For sf_project_propose_create, evidence_refs must be only stored paper refs "
      + "(paper:<paper_id> or paper-<paper_id>); never stage/task/experiment ids. "
      + "If the user asked to prepare a create proposal for a new stage, emit sf_project_propose_create after the duplicate check unless they explicitly asked to reuse an equivalent stage. "
      + "The create tool normalizes new stage ids to stage-*. Stop before apply; "
      + "never create a task against an uncommitted project."
    );
  }
  if (routeKey === "project_context") {
    const duties = loadInformationDutiesContract();
    const allowlist = duties.project_status_allowlist.join(", ");
    const readOnly = options.readOnly === true;
    const mode = options.resolutionMode ?? "unresolved";
    const modeFlow = {
      explicit_id:
        "An explicit stage id is already claimed: call sf_project_get with that exact id to verify it; do not run a fuzzy sf_project_search.",
      single_named:
        "Resolve the named project with one bounded sf_project_search, confirm a unique stage_id, then call sf_project_get with only that exact id.",
      dual_named:
        "This is a dual-project comparison: run separate bounded sf_project_search calls for each named project (never one combined query), then sf_project_get for each exact returned stage_id.",
      global_overview:
        "This is a project overview: use bounded paginated sf_project_search, report truncation when has_more is true, and do not invent missing pages.",
      second_from_prior:
        "The second project from the prior comparison is already claimed: call sf_project_get with that exact stage id; do not run another fuzzy search.",
      unresolved:
        "Call sf_project_search with the named project/stage terms (or a bounded overview when no name is given), then sf_project_get with only an exact returned stage_id.",
    }[mode] ?? (
      "Call sf_project_search with the named project/stage terms, then sf_project_get with only an exact returned stage_id."
    );
    if (readOnly) {
      return (
        "SCHOLARFLOW PROJECT CONTEXT ROUTE: This is a read-only current-state question, not a request to plan or mutate. "
        + `First enter the project lane. ${modeFlow} `
        + `Bounded read tools for this workload (${duties.contract_id}): ${allowlist}. `
        + "Do not call planning, evidence-bundle, artifact-proposal, task/project proposal, apply, or memory tools; "
        + "do not invent a plan or next action."
      );
    }
    return (
      "SCHOLARFLOW PROJECT CONTEXT ROUTE: Ground the current answer in ScholarFlow. "
      + `${modeFlow} `
      + `Primary read surface (${duties.contract_id}): ${allowlist}. `
      + "Never guess a stage id or retry guessed variants. Do not mutate anything."
    );
  }
  const known = Object.values(manifest.skills).map((item) => item.flow_route_key).join(", ");
  throw new Error(`Unknown flow route key "${routeKey}"; expected one of: ${known}`);
}

/**
 * T4.5.2 — tool surface from manifest (+ optional runtime extras).
 *
 * @param {string} skillId
 * @param {{ includeProposals?: boolean }} [options]
 */
export function toolSurfaceForSkill(skillId, options = {}) {
  const block = skillContractBlock(skillId);
  const surface = block.tool_surface ?? {};
  const names = new Set([
    ...(surface.reads ?? []),
    ...(options.includeProposals ? (surface.proposals ?? []) : []),
  ]);
  return [...names];
}

/**
 * T4.5.3 — guard identifiers expected to enforce the contract at execution time.
 *
 * @param {string} skillId
 */
export function executionGuardsForSkill(skillId) {
  return [...(skillContractBlock(skillId).execution_guards ?? [])];
}

/**
 * T4.5.4 — legal exits for fixtures and scoring.
 *
 * @param {string} skillId
 */
export function legalExitsForSkill(skillId) {
  return [...(skillContractBlock(skillId).legal_exits ?? [])];
}

/**
 * @param {{ skill?: string, contract_outcome?: string, id?: string }} fixture
 */
export function assertFixtureMatchesContract(fixture) {
  if (!fixture?.skill) return { ok: true, skipped: true };
  const manifest = loadFourExitsManifest();
  const block = manifest.skills?.[fixture.skill];
  if (!block) return { ok: true, skipped: true };
  if (fixture.contract_outcome) {
    const legal = new Set(block.legal_exits);
    if (!legal.has(fixture.contract_outcome)) {
      return {
        ok: false,
        reason: `Fixture ${fixture.id ?? "(unknown)"} contract_outcome "${fixture.contract_outcome}" `
          + `is not in legal_exits for ${fixture.skill}: ${[...legal].join(", ")}`,
      };
    }
  }
  return { ok: true };
}

/**
 * @param {string} skillId
 * @param {string[]} activeTools
 */
export function assertActiveToolsMatchSkillSurface(skillId, activeTools, options = {}) {
  const allowed = new Set(toolSurfaceForSkill(skillId, options));
  const unexpected = activeTools.filter(
    (name) => name.startsWith("sf_") && !allowed.has(name),
  );
  if (unexpected.length) {
    return {
      ok: false,
      reason: `Active tools [${unexpected.join(", ")}] outside ${skillId} contract surface [${[...allowed].join(", ")}]`,
    };
  }
  return { ok: true };
}
