import assert from "node:assert/strict";
import test from "node:test";

import {
  bindStageToolCall,
  boundStageId,
  createEmptyTurnContext,
  extractClaimedStageIdsFromPrompt,
  findStageIdsInText,
  getTurnContextAudit,
  isSilentStageRewrite,
  markSearchCandidates,
  markStageVerified,
  resolveTurnContextFromPrompt,
  splitWorkbenchPrompt,
} from "../extensions/turn-context.js";

function workbenchPrompt({ refs = [], user, coordination = "" }) {
  const refLines = (refs.length ? refs : ["none"]).map((item) => `- ${item}`).join("\n");
  const coord = coordination
    ? `\n\nBounded coordination checkpoints (conversation hand-offs only; verify every factual claim with sf_* tools):\n${coordination}`
    : "";
  return (
    "ScholarFlow workbench context (user-selected references only; verify "
    + "all current facts with sf_* tools and do not treat this block as "
    + `research truth):\n${refLines}${coord}`
    + `\n\nUser request:\n${user}`
  );
}

test("T2.1–T2.2: UI selection and user-text stage ids are claimed, not verified", () => {
  const fromUi = resolveTurnContextFromPrompt(workbenchPrompt({
    refs: ["stage-eval-optics"],
    user: "What is the blocker?",
  }));
  assert.equal(fromUi.status, "claimed");
  assert.equal(fromUi.verifiedStageId, null);
  assert.deepEqual(fromUi.claimedStageIds, ["stage-eval-optics"]);
  assert.deepEqual(fromUi.uiSelectedStageIds, ["stage-eval-optics"]);
  assert.deepEqual(fromUi.userTextStageIds, []);

  const fromText = resolveTurnContextFromPrompt(
    "Project stage-eval-optics is already confirmed. Propose its first bounded task.",
  );
  assert.equal(fromText.status, "claimed");
  assert.equal(fromText.verifiedStageId, null);
  assert.deepEqual(fromText.claimedStageIds, ["stage-eval-optics"]);
  assert.deepEqual(fromText.userTextStageIds, ["stage-eval-optics"]);
});

test("quoted or example stage ids are not claimed", () => {
  const quoted = resolveTurnContextFromPrompt(
    'Explain the quoted example: "stage-example".',
  );
  assert.equal(quoted.status, "unresolved");
  assert.deepEqual(quoted.claimedStageIds, []);
  assert.deepEqual(findStageIdsInText('Explain the quoted example: "stage-example".'), []);
});

test("T2.3: verification marks claimed id verified; missing search keeps gap", () => {
  const ctx = resolveTurnContextFromPrompt("Inspect stage-eval-optics.");
  assert.equal(ctx.status, "claimed");
  assert.deepEqual(ctx.claimedStageIds, ["stage-eval-optics"]);
  markStageVerified(ctx, "stage-eval-optics");
  assert.equal(ctx.status, "verified");
  assert.equal(ctx.verifiedStageId, "stage-eval-optics");

  const missing = resolveTurnContextFromPrompt("Inspect stage-eval-optics.");
  markSearchCandidates(missing, ["stage-eval-calibration"]);
  assert.equal(missing.status, "missing");
  assert.match(missing.conflictReason ?? "", /not among|not returned/);
});

test("multi-project reads do not conflict; write target stays singular", () => {
  const ctx = createEmptyTurnContext();
  markStageVerified(ctx, "stage-alpha", "tool_verified", { multiRead: true });
  markStageVerified(ctx, "stage-beta", "tool_verified", { multiRead: true });
  assert.equal(ctx.status, "verified");
  assert.deepEqual(ctx.verifiedStageIds, ["stage-alpha", "stage-beta"]);
  assert.equal(ctx.verifiedStageId, null);
  const blockedWrite = bindStageToolCall(ctx, "sf_task_propose_create", { title: "x" });
  assert.equal(blockedWrite.block, true);
  const allowedGet = bindStageToolCall(ctx, "sf_project_get", { stage_id: "stage-beta" });
  assert.equal(allowedGet.block, undefined);
});

test("T2.4: multiple search candidates stay ambiguous; single candidate binds", () => {
  const ambiguous = createEmptyTurnContext();
  markSearchCandidates(ambiguous, ["stage-eval-optics", "stage-eval-calibration"]);
  assert.equal(ambiguous.status, "ambiguous");
  assert.equal(boundStageId(ambiguous), null);

  const unique = createEmptyTurnContext();
  markSearchCandidates(unique, ["stage-eval-optics"]);
  assert.equal(unique.status, "verified");
  assert.equal(unique.verifiedStageId, "stage-eval-optics");
});

test("ambiguous search allows explicit get of a listed candidate", () => {
  const ambiguous = createEmptyTurnContext();
  markSearchCandidates(ambiguous, ["stage-eval-optics", "stage-eval-calibration"]);
  const selected = bindStageToolCall(ambiguous, "sf_project_get", { stage_id: "stage-eval-optics" });
  assert.equal(selected.block, undefined);
  assert.equal(selected.action, "pass");

  const foreign = bindStageToolCall(ambiguous, "sf_project_get", { stage_id: "stage-other" });
  assert.equal(foreign.block, true);

  const silent = bindStageToolCall(ambiguous, "sf_project_get", {});
  assert.equal(silent.block, true);
});

test("T2.5 / T2.9: missing model id is filled from a single verified stage", () => {
  const ctx = resolveTurnContextFromPrompt("Use stage-eval-optics.");
  markStageVerified(ctx, "stage-eval-optics", "tool_verified", { writeTarget: true });
  const input = {};
  const result = bindStageToolCall(ctx, "sf_task_search", input);
  assert.equal(result.action, "filled");
  assert.equal(input.project_stage_id, "stage-eval-optics");
  const audit = getTurnContextAudit(ctx);
  assert.equal(audit.at(-1)?.action, "filled");
  assert.equal(audit.at(-1)?.originalParams.project_stage_id, undefined);
  assert.equal(audit.at(-1)?.executedParams.project_stage_id, "stage-eval-optics");
});

test("T2.6: coordination / paper-like text is not treated as UI selection", () => {
  const prompt = workbenchPrompt({
    refs: ["none"],
    user: "Summarize the current blocker.",
    coordination: "- [project] Prior note (cp-1): mentioned stage-eval-calibration in passing",
  });
  const { contextBlock, userText } = splitWorkbenchPrompt(prompt);
  assert.equal(findStageIdsInText(userText).length, 0);
  assert.ok(!contextBlock.includes("stage-eval-calibration"));
  const extracted = extractClaimedStageIdsFromPrompt(prompt);
  assert.deepEqual(extracted.claimedStageIds, []);
});

test("T2.7: UI selection vs user text conflict is explicit", () => {
  const ctx = resolveTurnContextFromPrompt(workbenchPrompt({
    refs: ["stage-eval-optics"],
    user: "Actually use stage-eval-calibration instead.",
  }));
  assert.equal(ctx.status, "conflict");
  assert.match(ctx.conflictReason ?? "", /conflicts/);
  const blocked = bindStageToolCall(ctx, "sf_project_get", { stage_id: "stage-eval-optics" });
  assert.equal(blocked.block, true);
  assert.equal(blocked.action, "conflict");
});

test("T2.8 / T2.13: refuse silent stage- repair and truncated eval-optics", () => {
  assert.equal(isSilentStageRewrite("eval-optics", "stage-eval-optics"), true);
  assert.equal(isSilentStageRewrite("stage-eval-optics", "stage-eval-optics"), false);

  const ctx = resolveTurnContextFromPrompt(
    "Project stage-eval-optics is already confirmed. Propose its first bounded task.",
  );
  assert.deepEqual(ctx.claimedStageIds, ["stage-eval-optics"]);
  assert.equal(ctx.verifiedStageId, null);

  const truncated = { stage_id: "eval-optics" };
  const blocked = bindStageToolCall(ctx, "sf_project_get", truncated);
  assert.equal(blocked.block, true);
  assert.equal(blocked.action, "conflict");
  assert.equal(truncated.stage_id, "eval-optics");
  assert.match(blocked.reason ?? "", /truncated|unprefixed|refusing/i);

  const unprefixedGet = createEmptyTurnContext();
  const rejected = bindStageToolCall(unprefixedGet, "sf_project_get", { stage_id: "eval-optics" });
  assert.equal(rejected.block, true);
  assert.equal(rejected.action, "rejected");
  assert.match(rejected.reason ?? "", /prefix stage-/);
});

test("T2.10: different model id is a scope conflict, not an overwrite", () => {
  const ctx = resolveTurnContextFromPrompt("Use stage-eval-optics.");
  markStageVerified(ctx, "stage-eval-optics", "tool_verified", { writeTarget: true });
  const input = { project_stage_id: "stage-eval-calibration" };
  const result = bindStageToolCall(ctx, "sf_task_propose_create", input);
  assert.equal(result.block, true);
  assert.equal(result.action, "conflict");
  assert.equal(input.project_stage_id, "stage-eval-calibration");
});

test("T2.11: task create is blocked for claimed or failed resolution", () => {
  const open = createEmptyTurnContext();
  const openResult = bindStageToolCall(open, "sf_task_propose_create", { title: "New task" });
  assert.equal(openResult.block, undefined);
  assert.equal(openResult.action, "pass");

  const claimed = resolveTurnContextFromPrompt("Use stage-eval-optics.");
  assert.equal(claimed.status, "claimed");
  const claimedBlocked = bindStageToolCall(claimed, "sf_task_propose_create", { title: "New task" });
  assert.equal(claimedBlocked.block, true);
  assert.equal(claimedBlocked.action, "blocked");

  const failed = createEmptyTurnContext();
  markSearchCandidates(failed, ["stage-a", "stage-b"]);
  assert.equal(failed.status, "ambiguous");
  const blocked = bindStageToolCall(failed, "sf_task_propose_create", { title: "New task" });
  assert.equal(blocked.block, true);
  assert.equal(blocked.action, "blocked");
});

test("T2.12: audit keeps original vs executed params", () => {
  const ctx = resolveTurnContextFromPrompt("Use stage-eval-optics.");
  markStageVerified(ctx, "stage-eval-optics", "tool_verified", { writeTarget: true });
  const input = { stage_id: "stage-eval-optics" };
  bindStageToolCall(ctx, "sf_project_get", input);
  const entry = getTurnContextAudit(ctx).at(-1);
  assert.equal(entry?.action, "pass");
  assert.deepEqual(entry?.originalParams, { stage_id: "stage-eval-optics" });
  assert.deepEqual(entry?.executedParams, { stage_id: "stage-eval-optics" });
  assert.equal(entry?.projectContext.verifiedStageId, "stage-eval-optics");
});

test("exact matching model id still executes for claimed get", () => {
  const ctx = resolveTurnContextFromPrompt(workbenchPrompt({
    refs: ["stage:stage-eval-optics"],
    user: "Read the project.",
  }));
  const input = { stage_id: "stage-eval-optics" };
  const result = bindStageToolCall(ctx, "sf_project_get", input);
  assert.equal(result.block, undefined);
  assert.equal(result.action, "pass");
});