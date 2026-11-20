function orderedSubsequence(actual, expected) {
  let cursor = 0;
  for (const item of actual) {
    if (item === expected[cursor]) cursor += 1;
  }
  return cursor === expected.length;
}

function includesNormalized(haystack, needle) {
  return String(haystack).toLowerCase().includes(String(needle).toLowerCase());
}

function argumentRuleMatches(calls, rule) {
  const candidates = calls.filter((call) => call.name === rule.tool);
  return candidates.some((call) => Object.entries(rule.contains || {}).every(
    ([key, value]) => includesNormalized(JSON.stringify(call.args?.[key]), value),
  ));
}

function resultRuleMatches(calls, rule) {
  const candidates = calls.filter((call) => call.name === rule.tool);
  return candidates.some((call) => {
    const value = String(rule.path || "").split(".").filter(Boolean).reduce(
      (current, key) => current && typeof current === "object" ? current[key] : undefined,
      call.result_metrics,
    );
    if (Object.hasOwn(rule, "equals") && value !== rule.equals) return false;
    if (Object.hasOwn(rule, "gte") && !(Number(value) >= Number(rule.gte))) return false;
    if (Object.hasOwn(rule, "lte") && !(Number(value) <= Number(rule.lte))) return false;
    return value !== undefined;
  });
}

function answerContainsExpectations(fixture) {
  return [...(fixture.answer_contains_all || []), ...(fixture.answer_contains || [])];
}

function criticalChecksForFixture(fixture, checks) {
  const defaults = ["forbidden_tools", "arguments", "answer_forbidden", "no_tool_errors"];
  const configured = fixture.release_gate?.critical_checks || defaults;
  return configured.filter((name) => Object.hasOwn(checks, name));
}

const INFRASTRUCTURE_ERROR_PATTERNS = [
  /no api key/i,
  /log in to a provider/i,
  /authentication (?:failed|required|error)/i,
  /invalid api key/i,
  /invalid bearer token/i,
  /\b401\b.*(?:auth|token|credential)/i,
  /connection error/i,
  /network error/i,
  /fetch failed/i,
  /econn(?:refused|reset|aborted)/i,
  /enotfound/i,
  /etimedout/i,
  /rate[ -]?limit/i,
  /quota/i,
  /service unavailable/i,
  /gateway timeout/i,
];

export function isInfrastructureError(value) {
  const text = Array.isArray(value) ? value.join("\n") : String(value || "");
  return INFRASTRUCTURE_ERROR_PATTERNS.some((pattern) => pattern.test(text));
}

export function evaluationStatus({
  failureCount = 0,
  infrastructureBlocked = false,
  releaseMode = false,
  live = false,
  releaseGateStatus = "pass",
} = {}) {
  if (failureCount > 0) return "fail";
  if (infrastructureBlocked) return "infrastructure_blocked";
  if (releaseMode && live && releaseGateStatus !== "pass") return "fail";
  return "pass";
}

export function scoreBehavior(fixture, trace) {
  const calledTools = [...new Set(trace.calls.map((call) => call.name))];
  const expectedAny = fixture.live_expected_any || [];
  const requiredAll = fixture.live_required_all || [];
  const requiredAny = fixture.live_required_any || [];
  const forbidden = fixture.forbidden_tools || [];
  const sequence = fixture.trajectory_sequence || [];
  const answer = trace.final_answer || "";
  const answerContains = answerContainsExpectations(fixture);
  const checks = {
    expected_any: expectedAny.length === 0 || expectedAny.some((name) => calledTools.includes(name)),
    required_all: requiredAll.every((name) => calledTools.includes(name)),
    required_any: requiredAny.length === 0 || requiredAny.some((name) => calledTools.includes(name)),
    forbidden_tools: forbidden.every((name) => !calledTools.includes(name)),
    trajectory_sequence: sequence.length === 0 || orderedSubsequence(trace.calls.map((call) => call.name), sequence),
    max_tool_calls: !fixture.max_tool_calls || trace.calls.length <= fixture.max_tool_calls,
    arguments: (fixture.argument_rules || []).every((rule) => argumentRuleMatches(trace.calls, rule)),
    results: (fixture.result_rules || []).every((rule) => resultRuleMatches(trace.calls, rule)),
    answer_contains: answerContains.every((text) => includesNormalized(answer, text)),
    answer_forbidden: (fixture.answer_forbidden || []).every((text) => !includesNormalized(answer, text)),
    no_tool_errors: trace.calls.every((call) => !call.is_error),
  };
  const critical = criticalChecksForFixture(fixture, checks);
  const score = Object.values(checks).filter(Boolean).length / Object.keys(checks).length;
  const criticalFailures = critical.filter((name) => !checks[name]);
  const advisoryFailures = Object.keys(checks).filter((name) => !checks[name] && !critical.includes(name));
  const releaseMinScore = Number(fixture.release_gate?.min_score ?? 0.9);
  const passed = fixture.release_gate
    ? criticalFailures.length === 0 && score >= releaseMinScore
    : Object.values(checks).every(Boolean);
  return {
    status: passed ? "pass" : "fail",
    score: Number(score.toFixed(3)),
    checks,
    critical_checks: critical,
    critical_failures: criticalFailures,
    advisory_failures: advisoryFailures,
    critical_failure: criticalFailures.length > 0,
    release_blocking_failure: criticalFailures.length > 0,
    called_tools: calledTools,
    forbidden_called: forbidden.filter((name) => calledTools.includes(name)),
  };
}

export function assistantText(events) {
  const texts = events
    .filter((event) => event.type === "message_end" && event.message?.role === "assistant")
    .flatMap((event) => event.message.content || [])
    .filter((content) => content.type === "text")
    .map((content) => content.text);
  return texts.at(-1) || "";
}

function executedArtifactSearchArguments(event, ending) {
  if (event.toolName !== "sf_artifact_search" || !ending || ending.isError) return undefined;
  const details = ending.result?.details;
  if (details?.operation !== "research_artifact.search") return undefined;
  const args = details.execution_arguments;
  if (!args || typeof args !== "object" || Array.isArray(args)) return undefined;
  const strings = ["query", "status", "kind", "project_stage_id"];
  if (Object.keys(args).some((key) => key !== "limit" && !strings.includes(key))) return undefined;
  if (strings.some((key) => Object.hasOwn(args, key) && typeof args[key] !== "string")) return undefined;
  if (!Number.isInteger(args.limit) || args.limit < 1) return undefined;
  return { ...args };
}

const INTENTIONAL_POLICY_BLOCK_PATTERNS = [
  /The experiment lane already performed its one broad search/i,
  /Open research must call sf_research_delegate exactly once/i,
  /Project resolution is incomplete for this turn/i,
  /This turn already has an explicit stage id/i,
  /This turn already resolved the second project from the prior comparison/i,
  /Project targets for this turn are already resolved/i,
  /Dual-project requests must use separate searches/i,
  /This turn already ran the same bounded project search/i,
  /Bootstrap duplicate checks require a named project or topic query/i,
  /Bounded search returned multiple stage candidates/i,
  /Cannot create a task under a claimed-but-unverified project stage/i,
  /Cannot create a task under an unresolved project stage/i,
];

export function isIntentionalPolicyBlock(errorText) {
  const text = String(errorText || "");
  if (!text) return false;
  return INTENTIONAL_POLICY_BLOCK_PATTERNS.some((pattern) => pattern.test(text));
}

export function toolTrace(events) {
  const endings = new Map(
    events.filter((event) => event.type === "tool_execution_end")
      .map((event) => [event.toolCallId, event]),
  );
  return events.filter((event) => event.type === "tool_execution_start").flatMap((event) => {
    const ending = endings.get(event.toolCallId);
    const errorText = ending?.isError
      ? (ending.result?.content || []).filter((item) => item.type === "text").map((item) => item.text).join("\n")
      : undefined;
    // Intentional ScholarFlow policy redirects stay in the raw Pi log, but do
    // not score as domain tool errors or completed tool calls (T5.4 root cause).
    if (isIntentionalPolicyBlock(errorText)) return [];
    // Hermes Memory currently represents an empty store as an error result.
    // For retrieval evaluation this is a valid empty read, not a broken tool.
    const expectedEmptyRead = event.toolName === "memory_search"
      && errorText?.includes("No memories in extended store yet");
    const delegateDetails = event.toolName === "sf_research_delegate" && ending?.result?.details
      ? ending.result.details
      : undefined;
    const resultMetrics = delegateDetails ? {
      read_only_enforced: delegateDetails.read_only_enforced === true,
      worker_count: Number(delegateDetails.worker_count || 0),
      successful_worker_count: Number(delegateDetails.successful_worker_count || 0),
      failed_worker_count: Number(delegateDetails.failed_worker_count || 0),
      unique_source_count: Number(delegateDetails.unique_source_count || 0),
      finding_count: Number(delegateDetails.finding_count || 0),
      child_tool_error_count: Number(delegateDetails.child_tool_error_count || 0),
      elapsed_ms: Number(delegateDetails.elapsed_ms || 0),
      aggregate_usage: {
        totalTokens: Number(delegateDetails.aggregate_usage?.totalTokens || 0),
        cost: Number(delegateDetails.aggregate_usage?.cost || 0),
      },
    } : undefined;
    const workerOutcomes = Array.isArray(delegateDetails?.workers)
      ? delegateDetails.workers.slice(0, 4).map((worker, index) => {
        const status = /^[a-z][a-z0-9_]{0,47}$/.test(String(worker?.status)) ? String(worker.status) : "unknown";
        const count = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
        return {
          worker_index: index + 1,
          status,
          exit_code: typeof worker?.exit_code === "number" && Number.isFinite(worker.exit_code) ? worker.exit_code : null,
          tool_call_count: count(worker?.tool_call_count),
          tool_error_count: count(worker?.tool_error_count),
          source_count: Array.isArray(worker?.bundle?.sources) ? worker.bundle.sources.length : 0,
          finding_count: Array.isArray(worker?.bundle?.findings) ? worker.bundle.findings.length : 0,
          // Raw child errors, facet text and evidence may contain source or
          // credential content. Preserve the explicit outcome category only.
          error_category: status === "ok" ? null : status,
        };
      }) : undefined;
    const executedArguments = executedArtifactSearchArguments(event, ending);
    return {
      id: event.toolCallId,
      name: event.toolName,
      args: executedArguments || event.args || {},
      ...(executedArguments ? { model_args: event.args || {}, args_origin: "tool_execution" } : {}),
      is_error: Boolean(ending?.isError) && !expectedEmptyRead,
      ...(resultMetrics ? { result_metrics: resultMetrics } : {}),
      ...(workerOutcomes ? { worker_outcomes: workerOutcomes } : {}),
      ...(errorText ? { error: errorText.slice(0, 1000) } : {}),
    };
  });
}
