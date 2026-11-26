import assert from "node:assert/strict";
import test from "node:test";

import {
  appendSessionVerifiedOrder,
  applyResolutionClassification,
  BOOTSTRAP_EMPTY_SEARCH_MESSAGE,
  classifyProjectResolution,
  comparisonOrderFromRecords,
  createTurnProjectResolution,
  extractComparisonProjectNames,
  extractSingleNamedProject,
  guardProjectSearchCall,
  INCOMPLETE_RESOLUTION_MESSAGE,
  isCombinedDualProjectQuery,
  normalizeProjectSearchInput,
  overviewTruncationNotice,
  projectSearchQueryKey,
} from "../extensions/project-resolution.js";
import { resolveTurnContextFromPrompt } from "../extensions/turn-context.js";

test("T4.1: explicit stage id mode blocks fuzzy project search", () => {
  const state = createTurnProjectResolution();
  const turnContext = resolveTurnContextFromPrompt("Project stage-eval-optics is confirmed.");
  const classified = classifyProjectResolution({
    prompt: "Project stage-eval-optics is confirmed.",
    turnContext,
  });
  assert.equal(classified.mode, "explicit_id");
  apply(state, classified);
  const blocked = guardProjectSearchCall(state, { query: "eval optics" });
  assert.equal(blocked.block, true);
  assert.match(blocked.reason ?? "", /sf_project_get/);
});

test("T4.2–T4.3: dual named comparison extracts two targets and rejects combined query", () => {
  const prompt =
    "Compare the current blockers for Eval Thesis and Calibration Track. Ground the comparison in ScholarFlow and cite both stage ids.";
  const names = extractComparisonProjectNames(prompt);
  assert.deepEqual(names, ["Eval Thesis", "Calibration Track"]);
  const state = createTurnProjectResolution();
  apply(state, classifyProjectResolution({ prompt, turnContext: resolveTurnContextFromPrompt(prompt), projectComparisonPrompt: true }));
  assert.equal(state.mode, "dual_named");
  assert.equal(
    isCombinedDualProjectQuery("Eval Thesis Calibration Track", state.namedTargets),
    true,
  );
  const blocked = guardProjectSearchCall(state, { query: "Eval Thesis Calibration Track" });
  assert.equal(blocked.block, true);
  assert.match(blocked.reason ?? "", /separate searches/i);
});

test("T4.4: overview truncation notice is explicit", () => {
  const notice = overviewTruncationNotice({
    has_more: true,
    total_matches: 42,
    count: 20,
    offset: 0,
  });
  assert.match(notice ?? "", /TRUNCATED/);
  assert.match(notice ?? "", /offset 0/);
  assert.match(notice ?? "", /42/);
});

test("T4.5: second project follow-up binds from last comparison order, not unrelated session visits", () => {
  const session = appendSessionVerifiedOrder([], ["stage-unrelated", "stage-eval-optics", "stage-eval-calibration"]);
  const lastComparison = ["stage-eval-optics", "stage-eval-calibration"];
  const prompt =
    "Now focus only on the second project from my original comparison request, Calibration Track. Give its next action and cite its stage id; do not mix in Eval Thesis task or experiment ids.";
  const classified = classifyProjectResolution({
    prompt,
    turnContext: resolveTurnContextFromPrompt(prompt),
    sessionVerifiedOrder: session,
    lastComparisonOrder: lastComparison,
  });
  assert.equal(classified.mode, "second_from_prior");
  assert.equal(classified.boundSecondStageId, "stage-eval-calibration");
});

test("T4.5b: successive comparisons replace prior second-project binding", () => {
  const prompt =
    "Now focus only on the second project from my original comparison request; cite its stage id.";
  const first = classifyProjectResolution({
    prompt,
    turnContext: resolveTurnContextFromPrompt(prompt),
    lastComparisonOrder: ["stage-a", "stage-b"],
  });
  assert.equal(first.boundSecondStageId, "stage-b");
  const second = classifyProjectResolution({
    prompt,
    turnContext: resolveTurnContextFromPrompt(prompt),
    lastComparisonOrder: ["stage-c", "stage-d"],
  });
  assert.equal(second.boundSecondStageId, "stage-d");
});

test("comparisonOrderFromRecords prefers named target order", () => {
  const order = comparisonOrderFromRecords(
    ["Eval Thesis", "Calibration Track"],
    [
      { stage_id: "stage-eval-calibration", project_name: "Calibration Track" },
      { stage_id: "stage-eval-optics", project_name: "Eval Thesis" },
    ],
  );
  assert.deepEqual(order, ["stage-eval-optics", "stage-eval-calibration"]);
});

test("T4.6–T4.8: budget, repeat cache, and incomplete resolution message", () => {
  const state = createTurnProjectResolution();
  state.mode = "single_named";
  const input = { query: "Calibration Track", limit: 20, offset: 0 };
  const key = projectSearchQueryKey(input);
  assert.equal(guardProjectSearchCall(state, input).block, undefined);
  state.searchCache.set(key, { items: [{ stage_id: "stage-eval-calibration" }] });
  const repeat = guardProjectSearchCall(state, input);
  assert.equal(repeat.block, true);
  assert.match(repeat.reason ?? "", /cached result/i);
  state.searchCache.clear();
  state.budget = { target: 2, repeat: 0, supplement: 0, pagination: 0 };
  const exhausted = guardProjectSearchCall(state, { query: "another", limit: 20 });
  assert.equal(exhausted.block, true);
  assert.equal(exhausted.reason, INCOMPLETE_RESOLUTION_MESSAGE);
});

test("T4.9: named query normalization keeps target-specific search (not empty overview page)", () => {
  const state = createTurnProjectResolution();
  state.mode = "dual_named";
  state.namedTargets = ["Eval Thesis", "Calibration Track"];
  const input = { query: "Eval Thesis and Calibration Track", limit: 20 };
  normalizeProjectSearchInput(state, input);
  assert.equal(input.query, "Eval Thesis");
});

test("bootstrap reuse prompt extracts active stage name and blocks empty search", () => {
  const prompt =
    "I want another project stage that validates optical simulation for Eval Thesis using the same baseline optical simulation paper already linked to the active Validate optical simulation stage. Check for duplicates.";
  assert.equal(extractSingleNamedProject(prompt), "Validate optical simulation");
  const classified = classifyProjectResolution({
    prompt,
    turnContext: resolveTurnContextFromPrompt(prompt),
  });
  assert.equal(classified.mode, "single_named");
  const state = createTurnProjectResolution();
  applyResolutionClassification(state, classified.mode, classified.namedTargets, classified.boundSecondStageId);
  const input = {};
  normalizeProjectSearchInput(state, input);
  assert.equal(input.query, "Validate optical simulation");
  const empty = guardProjectSearchCall(createTurnProjectResolution(), {}, { projectBootstrapPrompt: true });
  assert.equal(empty.block, true);
  assert.equal(empty.reason, BOOTSTRAP_EMPTY_SEARCH_MESSAGE);
});

test("bootstrap create may still search when an existing stage id is cited as a reference", () => {
  const prompt =
    "Using the stored baseline optical simulation paper that is already linked to stage-eval-optics, create a distinct literature-methods project stage.";
  const turnContext = resolveTurnContextFromPrompt(prompt);
  const classified = classifyProjectResolution({ prompt, turnContext });
  assert.equal(classified.mode, "explicit_id");
  const state = createTurnProjectResolution();
  applyResolutionClassification(state, classified.mode, classified.namedTargets, classified.boundSecondStageId);
  const blockedElsewhere = guardProjectSearchCall(state, { query: "literature methods" });
  assert.equal(blockedElsewhere.block, true);
  const allowedForBootstrap = guardProjectSearchCall(state, { query: "literature methods" }, { projectBootstrapPrompt: true });
  assert.equal(allowedForBootstrap.block, undefined);
});

test("T4.10: resolved turn blocks further project search", () => {
  const state = createTurnProjectResolution();
  state.mode = "single_named";
  state.resolutionComplete = true;
  const blocked = guardProjectSearchCall(state, { query: "Calibration Track" });
  assert.equal(blocked.block, true);
  assert.match(blocked.reason ?? "", /already resolved/i);
});

/** @param {ReturnType<typeof createTurnProjectResolution>} state @param {ReturnType<typeof classifyProjectResolution>} classified */
function apply(state, classified) {
  state.mode = classified.mode;
  state.namedTargets = classified.namedTargets;
  state.boundSecondStageId = classified.boundSecondStageId;
}
