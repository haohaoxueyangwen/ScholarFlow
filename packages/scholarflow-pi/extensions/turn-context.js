/**
 * Per-turn verified project context (P2).
 *
 * Parse order:
 * 1) UI-selected refs + user-text stage IDs
 * 2) ScholarFlow existence verification (via tool results / explicit verify)
 * 3–4) bounded search candidates; clarify when ambiguous
 * 5) bind the exact ID into tool execution
 *
 * Never silently repair unknown IDs by adding a `stage-` prefix on get/bind paths.
 */

/** @typedef {"unresolved"|"claimed"|"verified"|"ambiguous"|"conflict"|"missing"} TurnContextStatus */
/** @typedef {"ui_selection"|"user_text"|"tool_verified"|"search_candidate"} ContextSource */
/** @typedef {"pass"|"filled"|"conflict"|"blocked"|"rejected"} BindAction */

/**
 * @typedef {object} StageClaim
 * @property {string} stageId
 * @property {ContextSource} source
 * @property {boolean} verified
 */

/**
 * @typedef {object} TurnContextAuditEntry
 * @property {string} toolName
 * @property {Record<string, unknown>} originalParams
 * @property {Record<string, unknown>} executedParams
 * @property {BindAction} action
 * @property {string} reason
 * @property {{ status: TurnContextStatus, verifiedStageId: string|null, claimedStageIds: string[] }} projectContext
 */

/**
 * @typedef {object} ResolvedTurnContext
 * @property {string} sessionScope
 * @property {StageClaim[]} userSpecified
 * @property {string[]} claimedStageIds
 * @property {string[]} verifiedStageIds
 * @property {string|null} verifiedStageId write-target only; null when multi-read or unverified
 * @property {string[]} allowedOperations
 * @property {TurnContextStatus} status
 * @property {string|null} conflictReason
 * @property {TurnContextAuditEntry[]} audit
 * @property {string[]} searchCandidates
 * @property {string[]} uiSelectedStageIds
 * @property {string[]} userTextStageIds
 */

export const STAGE_ID_PATTERN = /^stage-[a-z0-9][a-z0-9._-]*$/i;

/** Tools that read or mutate under an existing project stage id. */
export const STAGE_BOUND_TOOLS = Object.freeze({
  sf_project_get: "stage_id",
  sf_project_propose_update: "stage_id",
  sf_task_search: "project_stage_id",
  sf_task_propose_create: "project_stage_id",
  sf_task_propose_update: "project_stage_id",
  sf_evidence_bundle: "project_stage_id",
  sf_artifact_search: "project_stage_id",
  sf_artifact_propose_publish: "project_stage_id",
  sf_manuscript_search: "project_stage_id",
  sf_knowledge_search: "project_stage_id",
  sf_experiment_search: "project_stage_id",
  sf_experiment_propose_log: "project_stage_id",
});

/** Task creation must not hang under an unresolved project. */
export const PROJECT_REQUIRED_TOOLS = new Set([
  "sf_task_propose_create",
]);

const USER_REQUEST_MARKER = "\n\nUser request:\n";
const COORDINATION_MARKER = "\n\nBounded coordination checkpoints";
const CONTEXT_HEADER = "ScholarFlow workbench context";

/**
 * @returns {ResolvedTurnContext}
 */
export function createEmptyTurnContext() {
  return {
    sessionScope: "turn",
    userSpecified: [],
    claimedStageIds: [],
    verifiedStageIds: [],
    verifiedStageId: null,
    allowedOperations: [],
    status: "unresolved",
    conflictReason: null,
    audit: [],
    searchCandidates: [],
    uiSelectedStageIds: [],
    userTextStageIds: [],
  };
}

/**
 * Split workbench preamble from the user request. Coordination / route blocks
 * are not treated as user-specified object sources (T2.6).
 *
 * @param {string} rawPrompt
 * @returns {{ contextBlock: string, userText: string }}
 */
export function splitWorkbenchPrompt(rawPrompt) {
  const prompt = String(rawPrompt ?? "");
  const markerIndex = prompt.indexOf(USER_REQUEST_MARKER);
  if (markerIndex < 0) {
    return { contextBlock: "", userText: prompt };
  }
  let contextBlock = prompt.slice(0, markerIndex);
  const coordIndex = contextBlock.indexOf(COORDINATION_MARKER);
  if (coordIndex >= 0) contextBlock = contextBlock.slice(0, coordIndex);
  const routeIndex = contextBlock.search(/\n\nSCHOLARFLOW |\n\nRequired first tool call:/);
  if (routeIndex >= 0) contextBlock = contextBlock.slice(0, routeIndex);
  return {
    contextBlock,
    userText: prompt.slice(markerIndex + USER_REQUEST_MARKER.length),
  };
}

/**
 * @param {string} value
 * @returns {string|null}
 */
export function normalizeStageRef(value) {
  const raw = String(value ?? "").trim();
  if (!raw || raw === "none") return null;
  const typed = raw.match(/^(?:stage|project_stage|project):(.+)$/i);
  const candidate = (typed ? typed[1] : raw).trim();
  if (!STAGE_ID_PATTERN.test(candidate)) return null;
  return candidate.toLowerCase();
}

/**
 * Collect stage ids from free text, ignoring quotes, code, and example framing.
 *
 * @param {string} text
 * @returns {string[]}
 */
export function findStageIdsInText(text) {
  const scrubbed = String(text ?? "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`[^`]*`/g, " ")
    .replace(/"[^"]*"/g, " ")
    .replace(/'[^']*'/g, " ")
    .replace(/「[^」]*」/g, " ")
    .replace(/\b(?:example|e\.g\.|eg\.|for example|quoted example|比如|例如|示例)[:：]?\s*/gi, " ");
  const matches = scrubbed.match(/\bstage-[a-z0-9][a-z0-9._-]*\b/gi) ?? [];
  const seen = new Set();
  /** @type {string[]} */
  const ids = [];
  for (const match of matches) {
    const id = match.toLowerCase();
    if (!seen.has(id)) {
      seen.add(id);
      ids.push(id);
    }
  }
  return ids;
}

/**
 * UI-selected references from the workbench context list only (T2.2).
 *
 * @param {string} contextBlock
 * @returns {string[]}
 */
export function extractWorkbenchSelectedStageIds(contextBlock) {
  if (!String(contextBlock ?? "").includes(CONTEXT_HEADER)) return [];
  const seen = new Set();
  /** @type {string[]} */
  const ids = [];
  for (const line of String(contextBlock).split(/\r?\n/)) {
    const bullet = line.match(/^\s*-\s+(.+?)\s*$/);
    if (!bullet) continue;
    const id = normalizeStageRef(bullet[1]);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    ids.push(id);
  }
  return ids;
}

/**
 * @param {string} rawPrompt
 * @returns {{ uiSelectedStageIds: string[], userTextStageIds: string[], claimedStageIds: string[], conflictReason: string|null }}
 */
export function extractClaimedStageIdsFromPrompt(rawPrompt) {
  const { contextBlock, userText } = splitWorkbenchPrompt(rawPrompt);
  const uiSelectedStageIds = extractWorkbenchSelectedStageIds(contextBlock);
  const userTextStageIds = findStageIdsInText(userText);
  const claimed = [...new Set([...uiSelectedStageIds, ...userTextStageIds])];
  let conflictReason = null;
  if (
    uiSelectedStageIds.length === 1 &&
    userTextStageIds.length === 1 &&
    uiSelectedStageIds[0] !== userTextStageIds[0]
  ) {
    conflictReason =
      `UI-selected stage ${uiSelectedStageIds[0]} conflicts with user-specified stage ${userTextStageIds[0]}; clarify before binding.`;
  } else if (claimed.length > 1) {
    conflictReason =
      `Multiple explicit stage ids in this turn (${claimed.join(", ")}); clarify before binding.`;
  }
  return { uiSelectedStageIds, userTextStageIds, claimedStageIds: claimed, conflictReason };
}

/**
 * Step 1 (+ conflict detection for T2.7). Does not invent IDs from paper/tool text.
 *
 * @param {string} rawPrompt
 * @param {{ sessionScope?: string, allowedOperations?: string[] }} [options]
 * @returns {ResolvedTurnContext}
 */
export function resolveTurnContextFromPrompt(rawPrompt, options = {}) {
  const ctx = createEmptyTurnContext();
  ctx.sessionScope = options.sessionScope ?? "turn";
  ctx.allowedOperations = options.allowedOperations ? [...options.allowedOperations] : [];
  const extracted = extractClaimedStageIdsFromPrompt(rawPrompt);
  ctx.uiSelectedStageIds = extracted.uiSelectedStageIds;
  ctx.userTextStageIds = extracted.userTextStageIds;
  for (const stageId of extracted.uiSelectedStageIds) {
    ctx.userSpecified.push({ stageId, source: "ui_selection", verified: false });
  }
  for (const stageId of extracted.userTextStageIds) {
    if (ctx.userSpecified.some((item) => item.stageId === stageId)) continue;
    ctx.userSpecified.push({ stageId, source: "user_text", verified: false });
  }
  if (extracted.conflictReason) {
    ctx.status = "conflict";
    ctx.conflictReason = extracted.conflictReason;
    return ctx;
  }
  if (extracted.claimedStageIds.length === 1) {
    ctx.status = "claimed";
    ctx.claimedStageIds = [...extracted.claimedStageIds];
    // Claimed ≠ verified: keep verifiedStageId null until ScholarFlow confirms.
    return ctx;
  }
  if (extracted.claimedStageIds.length > 1) {
    ctx.claimedStageIds = [...extracted.claimedStageIds];
  }
  ctx.status = "unresolved";
  return ctx;
}

/**
 * Step 2: record a ScholarFlow-confirmed stage in the read set.
 * Does not invent a write-target conflict when multiple projects are read.
 * Set options.writeTarget to bind the singular write target.
 *
 * @param {ResolvedTurnContext} ctx
 * @param {string} stageId
 * @param {ContextSource} [source]
 * @param {{ writeTarget?: boolean }} [options]
 * @returns {ResolvedTurnContext}
 */
export function markStageVerified(ctx, stageId, source = "tool_verified", options = {}) {
  const id = normalizeStageRef(stageId) ?? String(stageId ?? "").trim().toLowerCase();
  if (!id || !STAGE_ID_PATTERN.test(id)) return ctx;
  if (ctx.status === "conflict") return ctx;
  const existing = ctx.userSpecified.find((item) => item.stageId === id);
  if (existing) existing.verified = true;
  else ctx.userSpecified.push({ stageId: id, source, verified: true });
  if (!ctx.verifiedStageIds.includes(id)) ctx.verifiedStageIds.push(id);
  if (ctx.claimedStageIds.includes(id) || ctx.status === "claimed") {
    if (!ctx.claimedStageIds.includes(id) && ctx.claimedStageIds.length === 0) {
      ctx.claimedStageIds = [id];
    }
  }

  const asWriteTarget = options.writeTarget === true
    || (ctx.verifiedStageIds.length === 1 && ctx.claimedStageIds.length <= 1 && !options.multiRead);

  if (options.writeTarget === true) {
    if (ctx.verifiedStageId && ctx.verifiedStageId !== id) {
      ctx.status = "conflict";
      ctx.conflictReason =
        `Write target ${id} conflicts with already bound write target ${ctx.verifiedStageId}.`;
      return ctx;
    }
    ctx.verifiedStageId = id;
  } else if (asWriteTarget) {
    ctx.verifiedStageId = id;
  } else if (ctx.verifiedStageId && ctx.verifiedStageId !== id) {
    // Multi-project read scope: keep both verified ids; clear singular write target.
    ctx.verifiedStageId = null;
  }

  ctx.status = "verified";
  ctx.conflictReason = null;
  return ctx;
}

/**
 * Steps 3–4: record bounded search candidates; bind only when unique.
 *
 * @param {ResolvedTurnContext} ctx
 * @param {string[]} stageIds
 * @returns {ResolvedTurnContext}
 */
export function markSearchCandidates(ctx, stageIds) {
  if (ctx.status === "conflict") return ctx;
  const ids = [...new Set(
    (stageIds ?? [])
      .map((value) => normalizeStageRef(value) ?? String(value ?? "").trim().toLowerCase())
      .filter((value) => STAGE_ID_PATTERN.test(value)),
  )];
  ctx.searchCandidates = ids;
  if (ctx.status === "claimed" && ctx.claimedStageIds.length === 1) {
    const claimed = ctx.claimedStageIds[0];
    if (ids.includes(claimed)) {
      return markStageVerified(ctx, claimed, "search_candidate", { writeTarget: true });
    }
    ctx.status = "missing";
    ctx.conflictReason = ids.length === 0
      ? `Claimed stage ${claimed} was not returned by the bounded search.`
      : `Claimed stage ${claimed} was not among search candidates (${ids.join(", ")}).`;
    return ctx;
  }
  if (ctx.status === "verified" && ctx.verifiedStageIds.length > 0) {
    // Preserve multi-read verification; do not collapse to a single write target here.
    return ctx;
  }
  if (ids.length === 1) {
    return markStageVerified(ctx, ids[0], "search_candidate", { writeTarget: true });
  }
  if (ids.length > 1) {
    ctx.status = "ambiguous";
    ctx.conflictReason =
      `Bounded search returned multiple stage candidates (${ids.join(", ")}); clarify before binding.`;
    ctx.verifiedStageId = null;
  } else if (ctx.status === "unresolved") {
    ctx.status = "missing";
    ctx.conflictReason = "Bounded search returned no project stage candidates.";
  }
  return ctx;
}

/**
 * True when `modelId` looks like a truncated / unprefixed rewrite of `boundId`
 * (e.g. eval-optics ← stage-eval-optics). Never treat this as a repair target.
 *
 * @param {string} modelId
 * @param {string} boundId
 */
export function isSilentStageRewrite(modelId, boundId) {
  const model = String(modelId ?? "").trim().toLowerCase();
  const bound = String(boundId ?? "").trim().toLowerCase();
  if (!model || !bound || model === bound) return false;
  if (bound === `stage-${model}`) return true;
  if (model.startsWith("stage-") && bound.startsWith("stage-") && bound.endsWith(model.slice("stage-".length))) {
    return true;
  }
  if (!model.startsWith("stage-") && bound.endsWith(`-${model}`)) return true;
  if (!model.startsWith("stage-") && bound.endsWith(model)) return true;
  return false;
}

/**
 * @param {ResolvedTurnContext} ctx
 * @returns {string|null}
 */
export function boundStageId(ctx) {
  if (!ctx) return null;
  if (ctx.status === "conflict" || ctx.status === "ambiguous" || ctx.status === "missing") {
    return null;
  }
  // Only a verified write-target may auto-fill. Claimed ids require an exact get first.
  if (ctx.status === "verified" && ctx.verifiedStageId) {
    return ctx.verifiedStageId;
  }
  return null;
}

/**
 * @param {ResolvedTurnContext} ctx
 * @param {string} toolName
 * @param {Record<string, unknown>} input
 * @returns {{ block?: boolean, reason?: string, action: BindAction, paramKey?: string }}
 */
export function bindStageToolCall(ctx, toolName, input) {
  const paramKey = STAGE_BOUND_TOOLS[toolName];
  if (!paramKey || !input || typeof input !== "object") {
    return { action: "pass" };
  }

  const originalValue = typeof input[paramKey] === "string" ? input[paramKey].trim() : "";
  const originalParams = { ...input };
  const projectContext = {
    status: ctx.status,
    verifiedStageId: ctx.verifiedStageId,
    claimedStageIds: ctx.claimedStageIds.length
      ? [...ctx.claimedStageIds]
      : ctx.userSpecified.map((item) => item.stageId),
    verifiedStageIds: [...(ctx.verifiedStageIds ?? [])],
  };

  /** @param {BindAction} action @param {string} reason @param {boolean} [block] */
  const finish = (action, reason, block = false) => {
    ctx.audit.push({
      toolName,
      originalParams,
      executedParams: { ...input },
      action,
      reason,
      projectContext: {
        ...projectContext,
        status: ctx.status,
        verifiedStageId: ctx.verifiedStageId,
        verifiedStageIds: [...(ctx.verifiedStageIds ?? [])],
      },
    });
    return block ? { block: true, reason, action, paramKey } : { action, paramKey };
  };

  // Task create requires a verified write-target, not a mere claim.
  if (
    PROJECT_REQUIRED_TOOLS.has(toolName)
    && (
      ctx.status === "conflict"
      || ctx.status === "ambiguous"
      || ctx.status === "missing"
      || ctx.status === "claimed"
      || (ctx.status === "verified" && !ctx.verifiedStageId)
    )
  ) {
    return finish(
      "blocked",
      ctx.conflictReason
        ?? (ctx.status === "claimed"
          ? "Cannot create a task under a claimed-but-unverified project stage; call sf_project_get first."
          : "Cannot create a task under an unresolved project stage; resolve one exact stage id first."),
      true,
    );
  }

  if (ctx.status === "conflict" || ctx.status === "ambiguous") {
    if (originalValue) {
      const modelId = originalValue.toLowerCase();
      const bound = ctx.verifiedStageId;
      if (bound && modelId === bound) {
        return finish("pass", "Model supplied the already-resolved stage id.");
      }
      if (
        ctx.verifiedStageIds?.includes(modelId)
        && (toolName === "sf_project_get" || toolName === "sf_project_propose_update")
      ) {
        return finish("pass", "Model supplied a verified read-scope stage id.");
      }
      // Ambiguous search results: allow an explicit get of one listed candidate
      // so the model can disambiguate; refuse silent fill and non-candidate ids.
      if (
        ctx.status === "ambiguous"
        && toolName === "sf_project_get"
        && Array.isArray(ctx.searchCandidates)
        && ctx.searchCandidates.includes(modelId)
      ) {
        return finish(
          "pass",
          `Model selected search candidate ${modelId} to resolve ambiguity.`,
        );
      }
      return finish(
        "conflict",
        ctx.conflictReason ?? "Turn project context is ambiguous or conflicting; refuse silent stage binding.",
        true,
      );
    }
    return finish(
      "blocked",
      ctx.conflictReason ?? "Turn project context is ambiguous or conflicting; refuse silent stage fill.",
      true,
    );
  }

  // Claimed: allow exact get of the claimed id; do not auto-fill write tools.
  if (ctx.status === "claimed") {
    const claimed = ctx.claimedStageIds[0] ?? null;
    if (toolName === "sf_project_get") {
      if (!originalValue && claimed) {
        input[paramKey] = claimed;
        return finish("filled", `Filled stage_id from claimed turn context (${claimed}) for verification read.`);
      }
      if (originalValue && claimed && originalValue.toLowerCase() === claimed) {
        return finish("pass", "Model stage id matches claimed turn context.");
      }
      if (originalValue && claimed && isSilentStageRewrite(originalValue.toLowerCase(), claimed)) {
        return finish(
          "conflict",
          `Model stage id "${originalValue}" is a truncated/unprefixed rewrite of claimed "${claimed}"; refusing silent stage- repair (T2.8 / T2.10).`,
          true,
        );
      }
      if (originalValue && claimed && originalValue.toLowerCase() !== claimed) {
        return finish(
          "conflict",
          `Model stage_id="${originalValue}" conflicts with claimed stage "${claimed}"; refusing silent overwrite.`,
          true,
        );
      }
    }
    return finish("pass", "Claimed context does not auto-bind write tools; verification read required.");
  }

  const bound = boundStageId(ctx);
  if (!originalValue) {
    if (bound && ctx.status === "verified") {
      input[paramKey] = bound;
      return finish(
        "filled",
        `Filled ${paramKey} from verified turn context (${bound}).`,
      );
    }
    return finish("pass", "No stage param supplied; optional for this tool.");
  }

  const modelId = originalValue.toLowerCase();
  if (bound) {
    if (modelId === bound) {
      return finish("pass", "Model stage id matches turn context.");
    }
    if (isSilentStageRewrite(modelId, bound)) {
      return finish(
        "conflict",
        `Model stage id "${originalValue}" is a truncated/unprefixed rewrite of bound "${bound}"; refusing silent stage- repair (T2.8 / T2.10).`,
        true,
      );
    }
    // Multi-read: allow get of another verified stage without treating it as write conflict.
    if (
      toolName === "sf_project_get"
      && ctx.verifiedStageIds?.includes(modelId)
    ) {
      return finish("pass", "Model stage id is within verified read scope.");
    }
    if (
      toolName === "sf_project_get"
      && !ctx.verifiedStageId
      && ctx.verifiedStageIds?.length
    ) {
      return finish("pass", "Multi-read scope has no singular write target; forwarding get unchanged.");
    }
    return finish(
      "conflict",
      `Model ${paramKey}="${originalValue}" conflicts with turn-bound stage "${bound}"; refusing silent overwrite.`,
      true,
    );
  }

  // No bound write-target: still refuse inventing stage- prefixes on get/bind paths.
  if (
    toolName === "sf_project_get" &&
    modelId &&
    !STAGE_ID_PATTERN.test(modelId) &&
    STAGE_ID_PATTERN.test(`stage-${modelId.replace(/^proj-/, "")}`)
  ) {
    return finish(
      "rejected",
      `Refusing to silently prefix stage- onto unknown id "${originalValue}" for ${toolName}.`,
      true,
    );
  }

  return finish("pass", "No turn-bound stage; forwarding model stage id unchanged.");
}

/**
 * @param {ResolvedTurnContext} ctx
 * @returns {TurnContextAuditEntry[]}
 */
export function getTurnContextAudit(ctx) {
  return [...(ctx?.audit ?? [])];
}
