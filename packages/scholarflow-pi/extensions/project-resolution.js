/**
 * Multi-project resolution and bounded search budget (P4).
 *
 * Modes: explicit_id, single_named, dual_named, global_overview, second_from_prior.
 * Session-scoped verified order is for follow-up turns only; per-turn search cache
 * does not survive across turns.
 */

import { findStageIdsInText, STAGE_ID_PATTERN } from "./turn-context.js";

/** @typedef {"explicit_id"|"single_named"|"dual_named"|"global_overview"|"second_from_prior"|"unresolved"} ResolutionMode */
/** @typedef {"target"|"repeat"|"supplement"|"pagination"} BudgetKind */

export const PROJECT_SEARCH_MAX_PER_TURN = 2;
export const PROJECT_OVERVIEW_PAGE_SIZE = 20;
export const INCOMPLETE_RESOLUTION_MESSAGE =
  "Project resolution is incomplete for this turn; reuse returned records or ask for a narrower identifier. Do not claim that the project does not exist.";

/**
 * @typedef {object} TurnProjectResolution
 * @property {ResolutionMode} mode
 * @property {string[]} namedTargets
 * @property {boolean} resolutionComplete
 * @property {Set<string>} resolvedStageIds
 * @property {Map<string, object>} searchCache
 * @property {{ target: number, repeat: number, supplement: number, pagination: number }} budget
 * @property {object[]} audit
 * @property {string|null} boundSecondStageId
 */

/**
 * @returns {TurnProjectResolution}
 */
export function createTurnProjectResolution() {
  return {
    mode: "unresolved",
    namedTargets: [],
    resolutionComplete: false,
    resolvedStageIds: new Set(),
    searchCache: new Map(),
    budget: { target: 0, repeat: 0, supplement: 0, pagination: 0 },
    audit: [],
    boundSecondStageId: null,
  };
}

/**
 * @param {string} prompt
 * @returns {boolean}
 */
export function isSecondProjectFollowupPrompt(prompt) {
  return (
    /\b(?:focus only|second project|original comparison|do not mix|cite (?:its|the) stage id)\b/i.test(prompt) ||
    /(?:只关注|第二个项目|原来的对比|不要混入|引用(?:它|该|这个)阶段 ?ID)/u.test(prompt)
  );
}

/**
 * @param {string} prompt
 * @returns {string[]}
 */
export function extractComparisonProjectNames(prompt) {
  const text = String(prompt ?? "");
  const patterns = [
    /\bcompare\b[^.!?\n]{0,40}(?:the\s+)?(?:current\s+)?blockers?\s+for\s+(.+?)\s+and\s+(.+?)(?:\.|,|\s+from|\s+ground|\s+cite|\s+only|$)/i,
    /\bcompare\b[^.!?\n]{0,80}(.+?)\s+and\s+(.+?)(?:\.|,|\s+from|\s+ground|\s+cite|\s+only|$)/i,
    /(?:比较|对比).{0,30}(?:阻塞项?|状态).{0,20}(.+?)(?:与|和)(.+?)(?:的|当前|阻塞|\.|,|$)/u,
  ];
  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (!match) continue;
    const left = cleanProjectName(match[1]);
    const right = cleanProjectName(match[2]);
    if (left && right && left.toLowerCase() !== right.toLowerCase()) {
      return [left, right];
    }
  }
  return [];
}

/**
 * @param {string} value
 * @returns {string}
 */
function cleanProjectName(value) {
  return String(value ?? "")
    .replace(/\b(?:from|stored|records?|ground(?:ed)?|scholarflow|cite|only|current|blockers?)\b/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * @param {string} prompt
 * @returns {boolean}
 */
export function isGlobalProjectOverviewPrompt(prompt) {
  const text = String(prompt ?? "").toLowerCase();
  return (
    /\b(?:all|every|overview|inventory|list)\b[^.!?\n]{0,40}\b(?:project|stage)s?\b/.test(text) ||
    /(?:全部|所有|概览|清单|列出).{0,20}(?:项目|阶段)/u.test(text)
  );
}

/**
 * @param {string} prompt
 * @returns {string|null}
 */
export function extractSingleNamedProject(prompt) {
  const dual = extractComparisonProjectNames(prompt);
  if (dual.length === 2) return null;
  const text = String(prompt ?? "");
  const patterns = [
    // Prefer an already-named active stage when checking duplicates / reuse.
    /\bactive\s+([A-Z][A-Za-z0-9][A-Za-z0-9 .-]{1,80}?)\s+stage\b/i,
    /\blinked to (?:the )?(?:active\s+)?([A-Z][A-Za-z0-9][A-Za-z0-9 .-]{1,80}?)\s+stage\b/i,
    /\b(?:for|on|about|regarding)\s+(?:project\s+)?([A-Z][A-Za-z0-9 .-]{2,60}?)(?:\.|,|\s+give|\s+what|\s+current|$)/,
    /\b(?:status|blocker|progress|next action)\s+(?:of\s+)?(?:project\s+)?([A-Z][A-Za-z0-9 .-]{2,60}?)(?:\.|,|$)/i,
    /(?:项目|阶段)\s*[「"']?([^「"'\n。！？]{2,40})[」"']?/u,
  ];
  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (!match) continue;
    const name = cleanProjectName(match[1]);
    if (name && !STAGE_ID_PATTERN.test(name)) return name;
  }
  return null;
}

export const BOOTSTRAP_EMPTY_SEARCH_MESSAGE =
  "Bootstrap duplicate checks require a named project or topic query; do not run an empty overview sf_project_search.";

/**
 * @param {object} options
 * @param {string} options.prompt
 * @param {{ verifiedStageId?: string|null, claimedStageIds?: string[], userTextStageIds?: string[], uiSelectedStageIds?: string[], status?: string }} [options.turnContext]
 * @param {string[]} [options.sessionVerifiedOrder]
 * @param {string[]} [options.lastComparisonOrder]
 * @param {boolean} [options.projectComparisonPrompt]
 * @returns {{ mode: ResolutionMode, namedTargets: string[], boundSecondStageId: string|null }}
 */
export function classifyProjectResolution({
  prompt,
  turnContext,
  sessionVerifiedOrder = [],
  lastComparisonOrder = [],
  projectComparisonPrompt = false,
}) {
  const followup = isSecondProjectFollowupPrompt(prompt);
  const comparisonOrder = lastComparisonOrder.length >= 2
    ? lastComparisonOrder
    : sessionVerifiedOrder;
  if (followup && comparisonOrder.length >= 2) {
    return {
      mode: "second_from_prior",
      namedTargets: [],
      boundSecondStageId: comparisonOrder[1],
    };
  }
  const explicitIds = [...new Set([
    ...(turnContext?.claimedStageIds ?? []),
    ...(turnContext?.uiSelectedStageIds ?? []),
    ...(turnContext?.userTextStageIds ?? []),
  ])].filter((id) => STAGE_ID_PATTERN.test(id));
  if (explicitIds.length === 1 && !projectComparisonPrompt && !followup) {
    return { mode: "explicit_id", namedTargets: [], boundSecondStageId: null };
  }
  if (isGlobalProjectOverviewPrompt(prompt)) {
    return { mode: "global_overview", namedTargets: [], boundSecondStageId: null };
  }
  const dual = extractComparisonProjectNames(prompt);
  if (dual.length === 2 || (projectComparisonPrompt && dual.length === 2)) {
    return { mode: "dual_named", namedTargets: dual, boundSecondStageId: null };
  }
  if (projectComparisonPrompt) {
    return { mode: "dual_named", namedTargets: dual, boundSecondStageId: null };
  }
  const single = extractSingleNamedProject(prompt);
  if (single) {
    return { mode: "single_named", namedTargets: [single], boundSecondStageId: null };
  }
  return { mode: "unresolved", namedTargets: [], boundSecondStageId: null };
}

/**
 * @param {string} query
 * @param {string[]} namedTargets
 * @returns {boolean}
 */
export function isCombinedDualProjectQuery(query, namedTargets) {
  if (!query || namedTargets.length < 2) return false;
  const normalized = query.toLocaleLowerCase();
  const hits = namedTargets.filter((target) => normalized.includes(target.toLocaleLowerCase()));
  return hits.length >= 2 || (/\band\b|&|与|和/.test(normalized) && hits.length >= 1 && normalized.split(/\band\b|&|与|和/u).length >= 2);
}

/**
 * @param {TurnProjectResolution} state
 * @param {ResolutionMode} mode
 * @param {string[]} namedTargets
 * @param {string|null} boundSecondStageId
 */
export function applyResolutionClassification(state, mode, namedTargets, boundSecondStageId) {
  state.mode = mode;
  state.namedTargets = [...namedTargets];
  state.boundSecondStageId = boundSecondStageId;
}

/**
 * @param {string[]} sessionOrder
 * @param {string[]} stageIds
 * @returns {string[]}
 */
export function appendSessionVerifiedOrder(sessionOrder, stageIds) {
  const next = [...sessionOrder];
  for (const raw of stageIds) {
    const id = String(raw ?? "").trim().toLowerCase();
    if (!id || !STAGE_ID_PATTERN.test(id) || next.includes(id)) continue;
    next.push(id);
  }
  return next;
}

/**
 * Build comparison order from dual-named targets and search/get records.
 * Prefers project_name / title match against namedTargets; falls back to
 * first two distinct stage ids in encounter order only when names cannot match.
 *
 * @param {string[]} namedTargets
 * @param {Array<{ stage_id?: string, project_name?: string, title?: string }>} records
 * @returns {string[]}
 */
export function comparisonOrderFromRecords(namedTargets, records) {
  const targets = (namedTargets ?? []).map((name) => String(name).trim()).filter(Boolean);
  if (targets.length < 2) return [];
  /** @type {(string|null)[]} */
  const ordered = targets.map(() => null);
  for (const record of records ?? []) {
    const stageId = String(record?.stage_id ?? "").trim().toLowerCase();
    if (!stageId || !STAGE_ID_PATTERN.test(stageId)) continue;
    const haystack = `${record?.project_name ?? ""} ${record?.title ?? ""}`.toLocaleLowerCase();
    for (let index = 0; index < targets.length; index += 1) {
      if (ordered[index]) continue;
      if (haystack.includes(targets[index].toLocaleLowerCase())) {
        ordered[index] = stageId;
        break;
      }
    }
  }
  if (ordered.every(Boolean)) return /** @type {string[]} */ (ordered);
  const fallback = [];
  for (const record of records ?? []) {
    const stageId = String(record?.stage_id ?? "").trim().toLowerCase();
    if (!stageId || !STAGE_ID_PATTERN.test(stageId) || fallback.includes(stageId)) continue;
    fallback.push(stageId);
    if (fallback.length >= 2) break;
  }
  return fallback.length >= 2 ? fallback : [];
}

/**
 * @param {object|null|undefined} data
 * @returns {string[]}
 */
export function stageIdsFromSearchData(data) {
  if (!data || typeof data !== "object") return [];
  const items = /** @type {{ items?: unknown[] }} */ (data).items;
  if (!Array.isArray(items)) return [];
  return items
    .filter((item) => item && typeof item === "object")
    .map((item) => /** @type {{ stage_id?: unknown }} */ (item).stage_id)
    .filter((value) => typeof value === "string" && STAGE_ID_PATTERN.test(value))
    .map((value) => value.toLowerCase());
}

/**
 * @param {TurnProjectResolution} state
 * @param {string} queryKey
 * @param {object} data
 */
export function cacheProjectSearchResult(state, queryKey, data) {
  state.searchCache.set(queryKey, data);
  for (const id of stageIdsFromSearchData(data)) state.resolvedStageIds.add(id);
}

/**
 * @param {TurnProjectResolution} state
 * @param {string[]} stageIds
 */
export function markProjectResolutionComplete(state, stageIds) {
  for (const id of stageIds) {
    if (STAGE_ID_PATTERN.test(id)) state.resolvedStageIds.add(id.toLowerCase());
  }
  if (state.mode === "explicit_id" && state.resolvedStageIds.size >= 1) {
    state.resolutionComplete = true;
  }
  if (state.mode === "single_named" && state.resolvedStageIds.size >= 1) {
    state.resolutionComplete = true;
  }
  if (state.mode === "dual_named" && state.resolvedStageIds.size >= 2) {
    state.resolutionComplete = true;
  }
  if (state.mode === "second_from_prior" && state.boundSecondStageId) {
    state.resolutionComplete = true;
  }
}

/**
 * @param {TurnProjectResolution} state
 * @returns {number}
 */
function totalPrimaryBudgetUsed(state) {
  const b = state.budget;
  return b.target + b.repeat + b.supplement;
}

/**
 * @param {TurnProjectResolution} state
 * @param {BudgetKind} kind
 */
function consumeBudget(state, kind) {
  state.budget[kind] += 1;
}

/**
 * @param {Record<string, unknown>} input
 * @returns {string}
 */
export function projectSearchQueryKey(input) {
  const query = typeof input.query === "string" ? input.query.trim().toLocaleLowerCase() : "";
  const offset = typeof input.offset === "number" ? input.offset : 0;
  const limit = typeof input.limit === "number" ? input.limit : PROJECT_OVERVIEW_PAGE_SIZE;
  return `${query}|offset:${offset}|limit:${limit}`;
}

/**
 * @param {TurnProjectResolution} state
 * @param {Record<string, unknown>} input
 * @param {{ projectComparisonPrompt?: boolean, projectBootstrapPrompt?: boolean }} [options]
 * @returns {{ block?: boolean, reason?: string, kind?: BudgetKind }}
 */
export function guardProjectSearchCall(state, input, options = {}) {
  if (state.resolutionComplete) {
    return {
      block: true,
      reason: "Project targets for this turn are already resolved; reuse returned records instead of issuing another project search.",
      kind: "repeat",
    };
  }
  if (state.mode === "explicit_id" && !options.projectBootstrapPrompt) {
    return {
      block: true,
      reason: "This turn already has an explicit stage id; call sf_project_get with that exact id instead of a fuzzy project search.",
      kind: "target",
    };
  }
  if (state.mode === "second_from_prior" && state.boundSecondStageId) {
    return {
      block: true,
      reason: "This turn already resolved the second project from the prior comparison; call sf_project_get with that exact id instead of a fuzzy project search.",
      kind: "target",
    };
  }
  const query = typeof input.query === "string" ? input.query.trim() : "";
  const offset = typeof input.offset === "number" ? input.offset : 0;
  if (options.projectBootstrapPrompt && !query && offset === 0) {
    return {
      block: true,
      reason: BOOTSTRAP_EMPTY_SEARCH_MESSAGE,
      kind: "target",
    };
  }
  if (state.mode === "dual_named" && isCombinedDualProjectQuery(query, state.namedTargets)) {
    return {
      block: true,
      reason: "Dual-project requests must use separate searches for each named project; do not combine both names into one query.",
      kind: "target",
    };
  }
  const queryKey = projectSearchQueryKey(input);
  if (state.searchCache.has(queryKey)) {
    consumeBudget(state, "repeat");
    return {
      block: true,
      reason: "This turn already ran the same bounded project search; reuse the cached result instead of repeating the query.",
      kind: "repeat",
    };
  }
  const kind = offset > 0 ? "pagination" : "target";
  if (kind === "pagination") {
    if (state.budget.pagination >= 1) {
      return { block: true, reason: INCOMPLETE_RESOLUTION_MESSAGE, kind: "pagination" };
    }
    consumeBudget(state, "pagination");
    return { kind: "pagination" };
  }
  if (totalPrimaryBudgetUsed(state) >= PROJECT_SEARCH_MAX_PER_TURN) {
    return { block: true, reason: INCOMPLETE_RESOLUTION_MESSAGE, kind: "supplement" };
  }
  consumeBudget(state, kind);
  return { kind };
}

/**
 * @param {TurnProjectResolution} state
 * @param {Record<string, unknown>} input
 */
export function normalizeProjectSearchInput(state, input) {
  delete input.status;
  const query = typeof input.query === "string" ? input.query.trim() : "";
  if (state.mode === "dual_named" && isCombinedDualProjectQuery(query, state.namedTargets)) {
    delete input.query;
  }
  if (state.mode === "global_overview") {
    if (!input.limit) input.limit = PROJECT_OVERVIEW_PAGE_SIZE;
    if (typeof input.offset !== "number") input.offset = 0;
    if (input.query === "") delete input.query;
    return;
  }
  if (state.mode === "dual_named" && state.namedTargets.length === 2 && !input.query) {
    const used = [...state.searchCache.keys()].join(" ");
    const nextTarget = state.namedTargets.find((target) => !used.includes(target.toLocaleLowerCase()));
    if (nextTarget) input.query = nextTarget;
  }
  // Named single-target / extracted bootstrap names: fill an empty query once.
  if (!input.query && state.namedTargets.length === 1) {
    input.query = state.namedTargets[0];
  }
}

/**
 * @param {object} data
 * @returns {string|null}
 */
export function overviewTruncationNotice(data) {
  if (!data || typeof data !== "object") return null;
  const record = /** @type {{ has_more?: boolean, total_matches?: number, count?: number, offset?: number }} */ (data);
  if (!record.has_more) return null;
  const total = typeof record.total_matches === "number" ? record.total_matches : "?";
  const offset = typeof record.offset === "number" ? record.offset : 0;
  const count = typeof record.count === "number" ? record.count : 0;
  return (
    `PROJECT OVERVIEW TRUNCATED: showing ${count} project stage(s) starting at offset ${offset} ` +
    `of ${total} total matches. Additional pages require another bounded project search with offset=${offset + count}.`
  );
}

/**
 * @param {TurnProjectResolution} state
 * @returns {object[]}
 */
export function getProjectResolutionAudit(state) {
  return [...(state?.audit ?? [])];
}
