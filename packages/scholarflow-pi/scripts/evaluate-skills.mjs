import { spawn, spawnSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { assistantText, evaluationStatus, isInfrastructureError, scoreBehavior, toolTrace } from "./eval-scoring.mjs";
import { assertNodeRuntimeAtLeast } from "./assert-node-runtime.mjs";
import { offlineContractResourceArgs } from "./evaluation-resources.mjs";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const packageRoot = resolve(scriptDir, "..");
const repositoryRoot = resolve(packageRoot, "..", "..");
const minimumNode = JSON.parse(readFileSync(resolve(packageRoot, "compatibility", "runtime-policy.json"), "utf8")).node.minimum;
assertNodeRuntimeAtLeast(process.versions.node, minimumNode, "evaluation controller");
if (process.env.SCHOLARFLOW_NODE?.trim()) {
  const childVersion = spawnSync(process.env.SCHOLARFLOW_NODE.trim(), ["--version"], { encoding: "utf8", timeout: 5000 });
  if (childVersion.error || childVersion.status !== 0) throw new Error("Cannot inspect the configured evaluation Pi child Node.js runtime");
  assertNodeRuntimeAtLeast(childVersion.stdout.trim(), minimumNode, "evaluation Pi child");
}
const evaluationCwd = process.env.SCHOLARFLOW_EVAL_CWD?.trim() || repositoryRoot;
const fixturePath = resolve(packageRoot, "quality", "skill-fixtures.json");
const live = process.argv.includes("--live");
const release = process.argv.includes("--release");
const onlyArg = process.argv.find((item) => item.startsWith("--skill="));
const onlySkill = onlyArg?.slice("--skill=".length);
const fixtureArg = process.argv.find((item) => item.startsWith("--fixture="));
const onlyFixture = fixtureArg?.slice("--fixture=".length);
const suiteArg = process.argv.find((item) => item.startsWith("--suite="));
const onlySuite = suiteArg?.slice("--suite=".length);
const timeoutArg = process.argv.find((item) => item.startsWith("--timeout-ms="));
const timeoutMs = Number(timeoutArg?.slice("--timeout-ms=".length) || (live ? 180_000 : 20_000));
const repeatArg = process.argv.find((item) => item.startsWith("--repeat="));
const repeat = Math.max(1, Number(repeatArg?.slice("--repeat=".length) || 1));
const reportArg = process.argv.find((item) => item.startsWith("--report="));
const reportPath = reportArg ? resolve(repositoryRoot, reportArg.slice("--report=".length)) : undefined;
const evalRoot = live ? mkdtempSync(resolve(tmpdir(), "scholarflow-pi-eval-")) : undefined;
const evalDataDir = evalRoot ? resolve(evalRoot, "data") : undefined;
const evalRunDir = evalRoot ? resolve(evalRoot, "runs") : undefined;
const evalVaultDir = evalRoot ? resolve(evalRoot, "vault") : undefined;
const evalAgentDir = evalRoot ? resolve(evalRoot, "agent") : undefined;

function isEvaluationPermissionRequest(event) {
  return live && event?.method === "select" &&
    String(event?.title || "").startsWith("Permission Required");
}

function prepareEvaluationAgentDir() {
  if (!evalRoot || !evalAgentDir) return;
  mkdirSync(evalAgentDir, { recursive: true });

  // Pi normally writes settings/trust locks next to the user's auth file. A
  // live release gate must be safe to run from CI and from a restricted
  // desktop worker, so give every run its own writable agent directory.
  const configuredAgentDir = process.env.SCHOLARFLOW_EVAL_AGENT_DIR?.trim()
    || process.env.PI_CODING_AGENT_DIR?.trim()
    || join(homedir(), ".pi", "agent");
  for (const file of ["auth.json", "models.json"]) {
    const source = resolve(configuredAgentDir, file);
    if (existsSync(source)) copyFileSync(source, resolve(evalAgentDir, file));
  }
  process.env.PI_CODING_AGENT_DIR = evalAgentDir;
}

function seedEvalStore({ reset = false } = {}) {
  if (!evalRoot || !evalDataDir || !evalRunDir || !evalVaultDir) return;
  if (reset) rmSync(evalDataDir, { recursive: true, force: true });
  for (const relative of ["wiki/reference", "wiki/syntheses", "wiki/entities", "wiki/specs", "10_Projects"]) {
    mkdirSync(resolve(evalVaultDir, relative), { recursive: true });
  }
  const seeded = spawnSync(
    process.env.SCHOLARFLOW_PYTHON || "python",
    [resolve(scriptDir, "seed-eval-store.py"), "--data-dir", evalDataDir, "--run-dir", evalRunDir],
    { cwd: repositoryRoot, encoding: "utf8" },
  );
  if (seeded.status !== 0) throw new Error(`Could not seed eval store: ${seeded.stderr || seeded.stdout}`);
}

if (evalRoot) {
  // Synthetic fixtures have no approved external index: never inherit the
  // desktop user's live LightRAG connection into the seed or Pi child.
  process.env.SCHOLARFLOW_KNOWLEDGE_ENGINE = "disabled";
  for (const name of Object.keys(process.env)) {
    if (name.startsWith("SCHOLARFLOW_LIGHTRAG_")) delete process.env[name];
  }
  seedEvalStore();
  process.env.SCHOLARFLOW_DATA_DIR = evalDataDir;
  process.env.SCHOLARFLOW_VAULT_DIR = evalVaultDir;
  process.env.SCHOLARFLOW_OBSERVE_ROOTS = evalRunDir;
  process.env.ZOTERO_MODE = "null";
  prepareEvaluationAgentDir();
}

const fixtureDocument = JSON.parse(readFileSync(fixturePath, "utf8"));
const releaseExpectations = fixtureDocument.release_expectations || {};
const fixtures = fixtureDocument.fixtures.filter((item) => {
  if (onlyFixture) return item.id === onlyFixture;
  if (onlySkill && item.skill !== onlySkill) return false;
  // Versioned suites stay out of the default/full release set unless selected.
  if (item.fixture_suite) {
    return Boolean(onlySuite) && item.fixture_suite === onlySuite;
  }
  if (onlySuite) return false;
  return true;
});
if (!fixtures.length) throw new Error(`No fixture matched ${onlyFixture ?? onlySuite ?? onlySkill ?? "the requested selection"}.`);

function writeLiveCheckpoint(results, current) {
  if (!reportPath) return;
  mkdirSync(dirname(reportPath), { recursive: true });
  writeFileSync(reportPath, `${JSON.stringify({
    schema_version: 2,
    mode: "live",
    release_mode: release,
    evaluation_policy: evaluationPolicy(),
    status: "running",
    updated_at: new Date().toISOString(),
    completed_run_count: results.length,
    current,
    behavior_results: results,
  }, null, 2)}\n`, "utf8");
}

function evaluationPolicy() {
  return live ? {
    knowledge_engine: process.env.SCHOLARFLOW_KNOWLEDGE_ENGINE,
    lightrag_env_cleared: !Object.keys(process.env).some((name) => name.startsWith("SCHOLARFLOW_LIGHTRAG_")),
    synthetic_index: "none",
  } : undefined;
}

function piCliPath() {
  const configured = process.env.SCHOLARFLOW_PI_CLI?.trim();
  if (configured) return configured;
  const development = resolve(
    packageRoot,
    "node_modules", "@earendil-works", "pi-coding-agent", "dist", "cli.js",
  );
  if (existsSync(development)) return development;
  const managed = resolve(
    repositoryRoot,
    ".pi", "npm", "node_modules", "@earendil-works", "pi-coding-agent", "dist", "cli.js",
  );
  if (!existsSync(managed)) {
    throw new Error("Pi CLI was not found. Set SCHOLARFLOW_PI_CLI or install the project-local Pi packages.");
  }
  return managed;
}

class RpcClient {
  constructor() {
    this.events = [];
    this.responses = new Map();
    this.waiters = new Map();
    this.eventWaiters = [];
    this.buffer = "";
    const args = [piCliPath(), "--mode", "rpc", "--no-session", "--no-builtin-tools", "--approve"];
    args.push(...offlineContractResourceArgs({ live, evaluationCwd, packageRoot }));
    if (!live) args.push("--offline");
    if (process.env.SCHOLARFLOW_EVAL_PROVIDER) args.push("--provider", process.env.SCHOLARFLOW_EVAL_PROVIDER);
    if (process.env.SCHOLARFLOW_EVAL_MODEL) args.push("--model", process.env.SCHOLARFLOW_EVAL_MODEL);
    const env = { ...process.env };
    env.SCHOLARFLOW_DISABLE_UPDATE_CHECK = "1";
    if (!live) {
      const agentDir = resolve(repositoryRoot, ".tmp", "pi-skill-eval-agent");
      mkdirSync(agentDir, { recursive: true });
      env.PI_CODING_AGENT_DIR = agentDir;
    }
    this.child = spawn(process.env.SCHOLARFLOW_NODE || process.execPath, args, {
      cwd: evaluationCwd,
      env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.stderr = "";
    this.child.stderr.on("data", (chunk) => { this.stderr += chunk.toString("utf8"); });
    this.child.stdout.on("data", (chunk) => this.onData(chunk.toString("utf8")));
  }

  onData(chunk) {
    this.buffer += chunk;
    while (this.buffer.includes("\n")) {
      const index = this.buffer.indexOf("\n");
      const line = this.buffer.slice(0, index).trim();
      this.buffer = this.buffer.slice(index + 1);
      if (!line) continue;
      let event;
      try { event = JSON.parse(line); } catch { continue; }
      this.events.push(event);
      for (const waiter of [...this.eventWaiters]) {
        if (!waiter.predicate(event)) continue;
        this.eventWaiters.splice(this.eventWaiters.indexOf(waiter), 1);
        waiter.resolve(event);
      }
      if (event.type === "response" && event.id) {
        this.responses.set(event.id, event);
        this.waiters.get(event.id)?.(event);
        this.waiters.delete(event.id);
      }
      if (event.type === "extension_ui_request" && ["confirm", "select", "input", "editor"].includes(event.method)) {
        // The live gate already runs against disposable state. Allow the
        // permission extension to execute tools so the domain proposal and
        // confirmation boundaries are what the test exercises. Actual
        // mutation confirmations remain denied below.
        const permissionApproval = isEvaluationPermissionRequest(event);
        const response = event.method === "confirm"
          ? { type: "extension_ui_response", id: event.id, confirmed: false }
          : { type: "extension_ui_response", id: event.id, value: permissionApproval ? "Yes" : null };
        this.send(response);
      }
    }
  }

  send(payload) {
    this.child.stdin.write(`${JSON.stringify(payload)}\n`);
  }

  async request(payload) {
    const id = payload.id || `eval-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    payload.id = id;
    const responsePromise = new Promise((resolveResponse, reject) => {
      const timer = setTimeout(() => reject(new Error(`RPC request ${id} timed out.`)), timeoutMs);
      this.waiters.set(id, (response) => { clearTimeout(timer); resolveResponse(response); });
    });
    this.send(payload);
    return responsePromise;
  }

  async waitForEvent(predicate, { after = 0, description = "Pi event" } = {}) {
    const existing = this.events.slice(after).find(predicate);
    if (existing) return existing;
    return new Promise((resolveEvent, reject) => {
      const waiter = {
        predicate,
        resolve: (event) => {
          clearTimeout(timer);
          resolveEvent(event);
        },
      };
      const timer = setTimeout(() => {
        const index = this.eventWaiters.indexOf(waiter);
        if (index >= 0) this.eventWaiters.splice(index, 1);
        reject(new Error(`${description} did not arrive before the timeout. ${this.stderr}`.trim()));
      }, timeoutMs);
      this.eventWaiters.push(waiter);
    });
  }

  async waitForSettled(after = 0) {
    return this.waitForEvent((event) => event.type === "agent_settled", {
      after,
      description: "Pi agent_settled event",
    });
  }

  async close() {
    if (this.child.exitCode !== null || this.child.signalCode !== null) return;
    await new Promise((resolveClose) => {
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        clearTimeout(forceTimer);
        resolveClose();
      };
      const forceTimer = setTimeout(() => {
        try { this.child.kill(); } catch { /* process already exited */ }
        finish();
      }, 5_000);
      this.child.once("exit", finish);
      this.child.once("error", finish);
      // Closing RPC stdin invokes Pi's supported shutdown path, allowing
      // extensions such as Hermes memory to close SQLite handles cleanly.
      try { this.child.stdin.end(); } catch { finish(); }
    });
  }
}

function summarizeEvents(events) {
  const counts = {};
  const errors = new Set();
  const stopReasons = new Set();
  const toolNames = new Set();
  for (const event of events) {
    counts[event.type] = (counts[event.type] || 0) + 1;
    if (event.toolName) toolNames.add(event.toolName);
    if (event.message?.stopReason) stopReasons.add(event.message.stopReason);
    for (const value of [event.message?.errorMessage, event.error, event.errorMessage]) {
      if (value) errors.add(String(value).slice(0, 500));
    }
  }
  return {
    counts,
    stop_reasons: [...stopReasons],
    tool_names: [...toolNames],
    errors: [...errors],
  };
}

function inspectSkillContracts(commands, discoveredTools) {
  const commandNames = new Set(commands.map((item) => item.name));
  const toolNames = new Set(discoveredTools);
  return fixtures.map((fixture) => {
    const path = resolve(packageRoot, "skills", fixture.skill, "SKILL.md");
    const source = readFileSync(path, "utf8");
    const missingToolsInPi = fixture.required_tools.filter((name) => !toolNames.has(name));
    const missingToolsInSkill = fixture.required_tools.filter((name) => !source.includes(`\`${name}\``));
    const normalizedSource = source.toLowerCase();
    const governanceOk = !fixture.governed_write || (
      normalizedSource.includes("confirmation") &&
      (normalizedSource.includes("propose") || normalizedSource.includes("proposal"))
    );
    const checks = {
      discovered_by_pi: commandNames.has(`skill:${fixture.skill}`),
      required_tools_available: missingToolsInPi.length === 0,
      required_tools_documented: missingToolsInSkill.length === 0,
      governed_write_documented: governanceOk,
      trigger_description_present: source.startsWith("---\n") && source.includes("description:"),
    };
    return {
      skill: fixture.skill,
      status: Object.values(checks).every(Boolean) ? "pass" : "fail",
      checks,
      missing_tools_in_pi: missingToolsInPi,
      missing_tools_in_skill: missingToolsInSkill,
    };
  });
}

function releaseRepeatForFixture(fixture) {
  return Math.max(1, Number(fixture.release_gate?.recommended_repeat || (fixture.release_gate ? 3 : 1)));
}

function releaseTimeoutForFixture(fixture) {
  return Math.max(20_000, Number(fixture.release_gate?.timeout_ms || 120_000));
}

function releaseLiveCommandForFixture(fixture) {
  const provider = process.env.SCHOLARFLOW_EVAL_PROVIDER || "deepseek";
  const model = process.env.SCHOLARFLOW_EVAL_MODEL || "";
  const report = `.tmp/eval/${fixture.id}-live.json`;
  const modelLine = model ? `$env:SCHOLARFLOW_EVAL_MODEL='${model}'\n` : "";
  return `$env:SCHOLARFLOW_EVAL_PROVIDER='${provider}'\n${modelLine}node packages/scholarflow-pi/scripts/evaluate-skills.mjs --live --release --fixture=${fixture.id} --repeat=${releaseRepeatForFixture(fixture)} --timeout-ms=${releaseTimeoutForFixture(fixture)} "--report=${report}"`;
}

function summarizeReleaseGates(contracts, behavior) {
  const gateFixtures = fixtures.filter((fixture) => fixture.release_gate);
  const contractBySkill = new Map(contracts.map((item) => [item.skill, item]));
  const fixtureResults = gateFixtures.map((fixture) => {
    const contract = contractBySkill.get(fixture.skill);
    const allRuns = behavior.filter((item) => item.id === fixture.id);
    const infrastructureBlockedCount = allRuns.filter((item) => item.status === "infrastructure_blocked").length;
    const runs = allRuns.filter((item) => item.status !== "infrastructure_blocked");
    const passCount = runs.filter((item) => item.status === "pass").length;
    const passRate = runs.length ? Number((passCount / runs.length).toFixed(3)) : 0;
    const minPassRate = Number(fixture.release_gate?.min_pass_rate || 1);
    const requiredRuns = releaseRepeatForFixture(fixture);
    const criticalFailureCount = runs.filter((item) => item.release_blocking_failure || item.critical_failure).length;
    const forbiddenCallCount = runs.reduce((sum, item) => sum + (item.forbidden_called?.length || 0), 0);
    const toolErrorCount = runs.reduce(
      (sum, item) => sum + (item.tool_calls || []).filter((call) => call.is_error).length,
      0,
    );
    const inventedIdSignalCount = runs.reduce(
      (sum, item) => sum + ((!item.checks?.arguments || !item.checks?.answer_forbidden) ? 1 : 0),
      0,
    );
    const unauthorizedApplyPathCount = runs.reduce(
      (sum, item) => sum + (item.forbidden_called || []).filter((name) => /apply/i.test(name)).length,
      0,
    );
    const liveReady = runs.length >= requiredRuns &&
      passRate >= minPassRate &&
      criticalFailureCount === 0 &&
      forbiddenCallCount === 0 &&
      toolErrorCount === 0 &&
      inventedIdSignalCount === 0 &&
      unauthorizedApplyPathCount === 0;
    const status = contract?.status !== "pass"
      ? "fail"
      : infrastructureBlockedCount > 0 && runs.length === 0
        ? "infrastructure_blocked"
      : runs.length === 0
        ? "pending_live"
        : liveReady
          ? "pass"
          : "fail";
    return {
      id: fixture.id,
      skill: fixture.skill,
      lane: fixture.release_gate?.lane || "default",
      contract_status: contract?.status || "missing",
      min_pass_rate: minPassRate,
      observed_pass_rate: passRate,
      required_runs: requiredRuns,
      observed_runs: runs.length,
      infrastructure_blocked_count: infrastructureBlockedCount,
      critical_failure_count: criticalFailureCount,
      forbidden_call_count: forbiddenCallCount,
      tool_error_count: toolErrorCount,
      invented_id_signal_count: inventedIdSignalCount,
      unauthorized_apply_path_count: unauthorizedApplyPathCount,
      status,
      live_command: releaseLiveCommandForFixture(fixture),
    };
  });
  const failedCount = fixtureResults.filter((item) => item.status === "fail").length;
  const infrastructureBlockedCount = fixtureResults.filter((item) => item.status === "infrastructure_blocked").length;
  const pendingLiveCount = fixtureResults.filter((item) => item.status === "pending_live").length;
  return {
    enabled: gateFixtures.length > 0,
    expectations: releaseExpectations,
    status: failedCount ? "fail" : infrastructureBlockedCount ? "infrastructure_blocked" : pendingLiveCount ? "pending_live" : "pass",
    fixture_results: fixtureResults,
    live_commands: fixtureResults.map((item) => ({
      id: item.id,
      skill: item.skill,
      repeat: item.required_runs,
      command: item.live_command,
    })),
  };
}

async function runLiveFixtures(client) {
  const results = [];
  for (const fixture of fixtures) {
    const fixtureRepeat = repeatArg ? repeat : release ? releaseRepeatForFixture(fixture) : repeat;
    for (let run = 1; run <= fixtureRepeat; run += 1) {
      process.stderr.write(`[pi-eval] start ${fixture.id} run=${run}/${fixtureRepeat}\n`);
      seedEvalStore({ reset: true });
      await client.request({ type: "new_session" });
      const eventStart = client.events.length;
      const startedAt = Date.now();
      const turns = fixture.turns?.length
        ? fixture.turns
        : [{ prompt: fixture.prompt }];
      const turnResults = [];
      let promptFailure;
      for (let turnIndex = 0; turnIndex < turns.length; turnIndex += 1) {
        const turn = typeof turns[turnIndex] === "string"
          ? { prompt: turns[turnIndex] }
          : turns[turnIndex];
        const turnEventStart = client.events.length;
        const accepted = await client.request({ type: "prompt", message: turn.prompt });
        if (!accepted.success) {
          promptFailure = accepted.error || `turn ${turnIndex + 1} prompt rejected`;
          break;
        }
        await client.waitForSettled(turnEventStart);
        const turnEvents = client.events.slice(turnEventStart);
        const turnScoring = turn.scoring
          ? scoreBehavior(
            { ...turn.scoring, release_gate: fixture.release_gate },
            {
              calls: toolTrace(turnEvents),
              final_answer: assistantText(turnEvents),
            },
          )
          : undefined;
        turnResults.push({
          turn: turnIndex + 1,
          prompt: turn.prompt,
          ...(turnScoring || { status: "observed" }),
          tool_call_count: toolTrace(turnEvents).length,
          final_answer: assistantText(turnEvents),
        });
      }
      if (promptFailure) {
        const infrastructureBlocked = isInfrastructureError(promptFailure);
        results.push({
          id: fixture.id,
          run,
          skill: fixture.skill,
          status: infrastructureBlocked ? "infrastructure_blocked" : "fail",
          error: promptFailure,
          turn_results: turnResults,
        });
        writeLiveCheckpoint(results, { fixture_id: fixture.id, run, repeat: fixtureRepeat });
        if (infrastructureBlocked) break;
        continue;
      }
      const events = client.events.slice(eventStart);
      const calls = toolTrace(events);
      const finalAnswer = assistantText(events);
      const eventSummary = summarizeEvents(events);
      const infrastructureErrors = eventSummary.errors.filter(isInfrastructureError);
      const infrastructureBlocked = infrastructureErrors.length > 0;
      const scoring = infrastructureBlocked
        ? {
          status: "infrastructure_blocked",
          score: null,
          error: infrastructureErrors.join(" | "),
        }
        : scoreBehavior(fixture, { calls, final_answer: finalAnswer });
      const failedTurns = turnResults.filter((item) => item.status === "fail");
      if (!infrastructureBlocked && failedTurns.length) {
        scoring.status = "fail";
        scoring.critical_failure = true;
        scoring.release_blocking_failure = true;
        scoring.turn_failure_count = failedTurns.length;
      }
      const stats = await client.request({ type: "get_session_stats" });
      const delegatedUsage = calls.reduce(
        (totals, call) => {
          totals.totalTokens += Number(call.result_metrics?.aggregate_usage?.totalTokens || 0);
          totals.cost += Number(call.result_metrics?.aggregate_usage?.cost || 0);
          return totals;
        },
        { totalTokens: 0, cost: 0 },
      );
      const permissionRequests = events.filter((item) => item.type === "extension_ui_request" && ["confirm", "select"].includes(item.method));
      results.push({
        id: fixture.id,
        run,
        skill: fixture.skill,
        ...scoring,
        duration_ms: Date.now() - startedAt,
        tool_call_count: calls.length,
        tool_calls: calls,
        final_answer: finalAnswer,
        usage: stats.data?.tokens,
        cost: stats.data?.cost,
        delegated_usage: delegatedUsage,
        total_cost_including_delegated: Number((Number(stats.data?.cost || 0) + delegatedUsage.cost).toFixed(9)),
        event_summary: eventSummary,
        permission_requests: permissionRequests.map((item) => ({
          method: item.method,
          title: item.title,
          approved_for_eval: isEvaluationPermissionRequest(item),
        })),
        confirmation_requests: permissionRequests.filter((item) => item.method === "confirm").length,
        turn_count: turns.length,
        turn_results: turnResults,
      });
      writeLiveCheckpoint(results, { fixture_id: fixture.id, run, repeat: fixtureRepeat });
      process.stderr.write(`[pi-eval] ${results.at(-1).status} ${fixture.id} run=${run}/${fixtureRepeat} score=${results.at(-1).score}\n`);
      if (infrastructureBlocked) break;
    }
    if (results.at(-1)?.status === "infrastructure_blocked") break;
  }
  return results;
}

const client = new RpcClient();
try {
  await new Promise((resolveReady) => setTimeout(resolveReady, 1500));
  const stateResponse = await client.request({ type: "get_state" });
  const commandsResponse = await client.request({ type: "get_commands" });
  const eventStart = client.events.length;
  await client.request({ type: "prompt", message: "/scholarflow-tools" });
  const notification = await client.waitForEvent(
    (item) => item.type === "extension_ui_request" && item.method === "notify" && String(item.message || "").includes("sf_"),
    { after: eventStart, description: "ScholarFlow tool discovery notification" },
  );
  const discoveredTools = String(notification?.message || "").split(", ").filter((item) => item.startsWith("sf_"));
  const contracts = inspectSkillContracts(commandsResponse.data?.commands || [], discoveredTools);
  const actualProvider = stateResponse.data?.model?.provider || stateResponse.data?.provider || "unknown";
  const actualModel = stateResponse.data?.model?.id || stateResponse.data?.model || "unknown";
  const requestedProvider = process.env.SCHOLARFLOW_EVAL_PROVIDER?.trim();
  const requestedModel = process.env.SCHOLARFLOW_EVAL_MODEL?.trim();
  const runtimeMismatch = live && (
    (requestedProvider && actualProvider !== requestedProvider) ||
    (requestedModel && actualModel !== requestedModel)
  );
  const behavior = runtimeMismatch
    ? [{
      id: "runtime-preflight",
      run: 0,
      skill: "runtime",
      status: "infrastructure_blocked",
      error: `Requested ${requestedProvider || "any"}/${requestedModel || "any"}, but Pi selected ${actualProvider}/${actualModel}.`,
    }]
    : live ? await runLiveFixtures(client) : [];
  const evaluatedBehavior = behavior.filter((item) => item.status !== "infrastructure_blocked");
  const releaseGates = summarizeReleaseGates(contracts, behavior);
  const infrastructureBlocked = behavior.some((item) => item.status === "infrastructure_blocked");
  const failures = [...contracts, ...behavior].filter((item) => !["pass", "infrastructure_blocked"].includes(item.status));
  const overallStatus = evaluationStatus({
    failureCount: failures.length,
    infrastructureBlocked,
    releaseMode: release,
    live,
    releaseGateStatus: releaseGates.status,
  });
  const report = {
    schema_version: 2,
    mode: live ? "live" : "contract",
    release_mode: release,
    status: overallStatus,
    evaluation_policy: evaluationPolicy(),
    pi_tool_count: discoveredTools.length,
    skill_fixture_count: fixtures.length,
    repeat: release && live && !repeatArg
      ? Math.max(...fixtures.map((fixture) => releaseRepeatForFixture(fixture)))
      : repeat,
    repeat_mode: release && live && !repeatArg ? "per_fixture_policy" : "fixed",
    runtime: live ? {
      provider: actualProvider,
      model: actualModel,
      requested_provider: requestedProvider || null,
      requested_model: requestedModel || null,
      selection_verified: !runtimeMismatch,
      isolated_store: true,
      mutation_confirmations_auto_denied: true,
      isolated_permission_requests_auto_approved: true,
    } : undefined,
    contract_results: contracts,
    behavior_results: behavior,
    release_gates: releaseGates,
    metrics: live ? {
      evaluated_run_count: evaluatedBehavior.length,
      infrastructure_blocked_count: behavior.length - evaluatedBehavior.length,
      pass_rate: evaluatedBehavior.length ? Number((evaluatedBehavior.filter((item) => item.status === "pass").length / evaluatedBehavior.length).toFixed(3)) : null,
      critical_failure_count: evaluatedBehavior.filter((item) => item.critical_failure).length,
      mean_score: evaluatedBehavior.length ? Number((evaluatedBehavior.reduce((sum, item) => sum + (item.score || 0), 0) / evaluatedBehavior.length).toFixed(3)) : null,
      mean_duration_ms: evaluatedBehavior.length ? Math.round(evaluatedBehavior.reduce((sum, item) => sum + (item.duration_ms || 0), 0) / evaluatedBehavior.length) : null,
      total_tool_calls: behavior.reduce((sum, item) => sum + (item.tool_call_count || 0), 0),
      total_cost: Number(behavior.reduce((sum, item) => sum + (item.cost || 0), 0).toFixed(6)),
      delegated_worker_cost: Number(behavior.reduce((sum, item) => sum + (item.delegated_usage?.cost || 0), 0).toFixed(6)),
      total_cost_including_delegated: Number(behavior.reduce((sum, item) => sum + (item.total_cost_including_delegated || item.cost || 0), 0).toFixed(6)),
      delegated_worker_tokens: behavior.reduce((sum, item) => sum + (item.delegated_usage?.totalTokens || 0), 0),
    } : undefined,
    failure_count: failures.length,
    infrastructure_blocked: infrastructureBlocked,
  };
  if (reportPath) {
    mkdirSync(dirname(reportPath), { recursive: true });
    writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  }
  const printableReport = reportPath ? {
    schema_version: report.schema_version,
    mode: report.mode,
    release_mode: report.release_mode,
    status: report.status,
    runtime: report.runtime,
    release_gates: report.release_gates,
    metrics: report.metrics,
    failure_count: report.failure_count,
    infrastructure_blocked: report.infrastructure_blocked,
    report_path: reportPath,
  } : report;
  process.stdout.write(`${JSON.stringify(printableReport, null, 2)}\n`);
  process.exitCode = report.status === "fail" ? 1 : infrastructureBlocked ? 2 : 0;
} finally {
  await client.close();
  if (evalRoot) {
    let cleaned = false;
    for (let attempt = 0; attempt < 10 && !cleaned; attempt += 1) {
      try {
        rmSync(evalRoot, { recursive: true, force: true });
        cleaned = true;
      } catch (error) {
        if (!(["EBUSY", "EPERM"].includes(error?.code))) throw error;
        await new Promise((resolveRetry) => setTimeout(resolveRetry, 100));
      }
    }
    if (!cleaned) process.stderr.write(`[pi-eval] cleanup deferred for ${evalRoot}\n`);
  }
}
