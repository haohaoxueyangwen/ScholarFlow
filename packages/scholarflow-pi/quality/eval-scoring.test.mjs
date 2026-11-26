import assert from "node:assert/strict";
import test from "node:test";

import { assistantText, evaluationStatus, isInfrastructureError, scoreBehavior, toolTrace } from "../scripts/eval-scoring.mjs";

test("artifact search scores executed filters while preserving model arguments", () => {
  const trace = (raw, executed, options = {}) => toolTrace([
    { type: "tool_execution_start", toolCallId: "a", toolName: options.name || "sf_artifact_search", args: raw },
    { type: "tool_execution_end", toolCallId: "a", isError: options.error || false,
      result: { details: { operation: "research_artifact.search", execution_arguments: executed } } },
  ]);
  const fixture = { argument_rules: [{ tool: "sf_artifact_search", contains: { kind: "manuscript_draft" } }] };
  const score = (calls) => scoreBehavior(fixture, { calls });
  const calls = trace({}, { kind: "manuscript_draft", limit: 20 });
  assert.equal(score(calls).checks.arguments, true);
  assert.deepEqual(calls[0].model_args, {});
  assert.equal(calls[0].args_origin, "tool_execution");
  assert.equal(score(trace({ kind: "manuscript_draft" }, { kind: "daily_report", limit: 20 })).checks.arguments, false);
  assert.deepEqual(trace({ query: "original" }, undefined)[0].args, { query: "original" });
  for (const options of [{ error: true }, { name: "sf_project_search" }]) {
    assert.deepEqual(trace({}, { kind: "manuscript_draft", limit: 20 }, options)[0].args, {});
  }
  assert.deepEqual(trace({}, { kind: "manuscript_draft", limit: 20, injected: true })[0].args, {});
});

test("scores ordered grounded trace", () => {
  const result = scoreBehavior(
    {
      live_expected_any: ["sf_project_search"],
      live_required_all: ["sf_project_search", "sf_project_get"],
      live_required_any: ["sf_task_propose_create", "sf_task_propose_update"],
      trajectory_sequence: ["sf_project_search", "sf_project_get"],
      forbidden_tools: ["sf_project_apply_update"],
      argument_rules: [{ tool: "sf_project_search", contains: { query: "Eval Thesis" } }],
      answer_contains_all: ["stage-eval-optics"],
      max_tool_calls: 3,
    },
    { calls: [
      { name: "sf_project_search", args: { query: "Eval Thesis" }, is_error: false },
      { name: "sf_project_get", args: { stage_id: "stage-eval-optics" }, is_error: false },
      { name: "sf_task_propose_update", args: { task_id: "task-eval" }, is_error: false },
    ], final_answer: "Current record: stage-eval-optics" },
  );
  assert.equal(result.status, "pass");
  assert.equal(result.score, 1);
});

test("critical forbidden call fails", () => {
  const result = scoreBehavior(
    { live_expected_any: ["sf_inbox_search"], forbidden_tools: ["sf_inbox_ack"] },
    { calls: [
      { name: "sf_inbox_search", args: {}, is_error: false },
      { name: "sf_inbox_ack", args: {}, is_error: false },
    ], final_answer: "done" },
  );
  assert.equal(result.status, "fail");
  assert.equal(result.critical_failure, true);
});

test("supports answer_contains alias and fixture-defined critical checks", () => {
  const result = scoreBehavior(
    {
      answer_contains: ["stage-eval-optics"],
      trajectory_sequence: ["sf_intake_create", "sf_intake_anchor"],
      release_gate: { critical_checks: ["trajectory_sequence"] },
    },
    { calls: [
      { name: "sf_intake_anchor", args: {}, is_error: false },
      { name: "sf_intake_create", args: {}, is_error: false },
    ], final_answer: "proposal for stage-eval-optics" },
  );
  assert.equal(result.status, "fail");
  assert.deepEqual(result.critical_failures, ["trajectory_sequence"]);
  assert.equal(result.checks.answer_contains, true);
  assert.equal(result.release_blocking_failure, true);
});

test("release fixtures may pass one advisory miss but never a critical miss", () => {
  const result = scoreBehavior(
    {
      max_tool_calls: 1,
      release_gate: { min_score: 0.9, critical_checks: ["no_tool_errors"] },
    },
    { calls: [
      { name: "sf_project_search", args: {}, is_error: false },
      { name: "sf_project_get", args: {}, is_error: false },
    ], final_answer: "grounded" },
  );
  assert.equal(result.status, "pass");
  assert.deepEqual(result.advisory_failures, ["max_tool_calls"]);
  assert.equal(result.critical_failure, false);
});

test("extracts trace and final assistant text", () => {
  const events = [
    { type: "tool_execution_start", toolCallId: "1", toolName: "sf_project_search", args: { query: "Eval" } },
    { type: "tool_execution_end", toolCallId: "1", isError: false, result: { content: [] } },
    { type: "message_end", message: { role: "assistant", content: [{ type: "text", text: "grounded" }] } },
  ];
  assert.equal(toolTrace(events)[0].name, "sf_project_search");
  assert.equal(assistantText(events), "grounded");
});

test("scores bounded delegated worker result metrics without retaining evidence payloads", () => {
  const events = [
    { type: "tool_execution_start", toolCallId: "1", toolName: "sf_research_delegate", args: { facets: [{}, {}] } },
    {
      type: "tool_execution_end",
      toolCallId: "1",
      isError: false,
      result: {
        content: [{ type: "text", text: "private evidence payload" }],
        details: {
          read_only_enforced: true,
          worker_count: 2,
          successful_worker_count: 2,
          failed_worker_count: 0,
          unique_source_count: 6,
          finding_count: 5,
          child_tool_error_count: 0,
          aggregate_usage: { totalTokens: 1234, cost: 0.01 },
          workers: [{ status: "format_error", exit_code: 0, tool_error_count: 0, error: "secret-child-error", bundle: { summary: "private evidence payload" } }],
        },
      },
    },
  ];
  const calls = toolTrace(events);
  assert.equal(JSON.stringify(calls).includes("private evidence payload"), false);
  assert.equal(JSON.stringify(calls).includes("secret-child-error"), false);
  assert.deepEqual(calls[0].worker_outcomes, [{ worker_index: 1, status: "format_error", exit_code: 0, tool_call_count: null, tool_error_count: 0, source_count: 0, finding_count: 0, error_category: "format_error" }]);
  const scored = scoreBehavior(
    {
      result_rules: [
        { tool: "sf_research_delegate", path: "read_only_enforced", equals: true },
        { tool: "sf_research_delegate", path: "successful_worker_count", gte: 2 },
        { tool: "sf_research_delegate", path: "unique_source_count", gte: 4 },
      ],
    },
    { calls, final_answer: "grounded" },
  );
  assert.equal(scored.checks.results, true);
  const withoutDiagnostics = calls.map(({ worker_outcomes, ...call }) => call);
  assert.deepEqual(scoreBehavior({ live_expected_any: ["sf_research_delegate"] }, { calls, final_answer: "grounded" }), scoreBehavior({ live_expected_any: ["sf_research_delegate"] }, { calls: withoutDiagnostics, final_answer: "grounded" }));
});

test("does not score a bounded duplicate experiment search as a domain error", () => {
  const events = [
    { type: "tool_execution_start", toolCallId: "1", toolName: "sf_experiment_search", args: { query: "mesh convergence" } },
    { type: "tool_execution_end", toolCallId: "1", isError: false, result: { content: [] } },
    { type: "tool_execution_start", toolCallId: "2", toolName: "sf_experiment_search", args: { query: "convergence" } },
    { type: "tool_execution_end", toolCallId: "2", isError: true, result: { content: [{ type: "text", text: "The experiment lane already performed its one broad search; reuse its result." }] } },
    { type: "tool_execution_start", toolCallId: "3", toolName: "sf_experiment_propose_log", args: { experiment_id: "exp-eval-convergence" } },
    { type: "tool_execution_end", toolCallId: "3", isError: false, result: { content: [] } },
  ];
  const trace = toolTrace(events);
  assert.deepEqual(trace.map((item) => item.name), ["sf_experiment_search", "sf_experiment_propose_log"]);
  assert.equal(trace.every((item) => !item.is_error), true);
});

test("does not score intentional project-resolution policy redirects as tool errors", () => {
  const events = [
    { type: "tool_execution_start", toolCallId: "1", toolName: "sf_project_get", args: { stage_id: "stage-eval-optics" } },
    { type: "tool_execution_end", toolCallId: "1", isError: false, result: { content: [] } },
    { type: "tool_execution_start", toolCallId: "2", toolName: "sf_project_search", args: { query: "optics" } },
    {
      type: "tool_execution_end",
      toolCallId: "2",
      isError: true,
      result: {
        content: [{
          type: "text",
          text: "This turn already has an explicit stage id; call sf_project_get with that exact id instead of a fuzzy project search.",
        }],
      },
    },
    { type: "tool_execution_start", toolCallId: "3", toolName: "sf_task_propose_create", args: { project_stage_id: "stage-eval-optics" } },
    { type: "tool_execution_end", toolCallId: "3", isError: false, result: { content: [] } },
  ];
  const trace = toolTrace(events);
  assert.deepEqual(trace.map((item) => item.name), ["sf_project_get", "sf_task_propose_create"]);
  assert.equal(
    scoreBehavior(
      { release_gate: { min_score: 0.9, critical_checks: ["no_tool_errors"] } },
      { calls: trace, final_answer: "proposed" },
    ).checks.no_tool_errors,
    true,
  );
});

test("scores an empty Hermes memory store as an empty read", () => {
  const events = [
    { type: "tool_execution_start", toolCallId: "1", toolName: "memory_search", args: { query: "Eval Thesis" } },
    { type: "tool_execution_end", toolCallId: "1", isError: true, result: { content: [{ type: "text", text: "No memories in extended store yet. Use memory_add to store memories." }] } },
  ];
  const trace = toolTrace(events);
  assert.equal(trace.length, 1);
  assert.equal(trace[0].name, "memory_search");
  assert.equal(trace[0].is_error, false);
});

test("classifies provider infrastructure failures without hiding ordinary behavior failures", () => {
  assert.equal(isInfrastructureError("Connection error."), true);
  assert.equal(isInfrastructureError("429 rate limit exceeded"), true);
  assert.equal(isInfrastructureError(["No API key found for the selected model."]), true);
  assert.equal(isInfrastructureError(["401 authentication_error: Invalid bearer token"]), true);
  assert.equal(isInfrastructureError("The agent selected the wrong tool."), false);
  assert.equal(isInfrastructureError("Unauthorized mutation attempt."), false);
});

test("behavior failures take precedence over infrastructure blocks", () => {
  assert.equal(evaluationStatus({ failureCount: 1, infrastructureBlocked: true }), "fail");
  assert.equal(evaluationStatus({ infrastructureBlocked: true }), "infrastructure_blocked");
  assert.equal(evaluationStatus({ releaseMode: true, live: true, releaseGateStatus: "pending_live" }), "fail");
  assert.equal(evaluationStatus({ releaseMode: true, live: true, releaseGateStatus: "pass" }), "pass");
});
