#!/usr/bin/env node

/**
 * Bounded live smoke of Pi's native session.compact() using synthetic context.
 *
 * This deliberately uses the registered model unchanged. Only Pi's in-memory
 * compaction keepRecentTokens is reduced, and only inside isolated temp state.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  createAgentSession,
  createExtensionRuntime,
  estimateTokens,
  ModelRuntime,
  SessionManager,
  SettingsManager,
} from "../packages/scholarflow-pi/node_modules/@earendil-works/pi-coding-agent/dist/index.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PI_PACKAGE_JSON = path.join(
  ROOT,
  "packages",
  "scholarflow-pi",
  "node_modules",
  "@earendil-works",
  "pi-coding-agent",
  "package.json",
);
const TEMP_PREFIX = "scholarflow-pi-native-compact-";
const MAX_NORMAL_PROMPTS = 3;
const MAX_FORCED_COMPACTIONS = 1;
const MAX_INPUT_TOKENS = 3_000;
const KEEP_RECENT_TOKENS = 1;

const FIXTURE = Object.freeze({
  evidenceId: "SYN-EVIDENCE-014",
  evidenceRef: "synthetic://fixture/pi-native-compaction-smoke/EVIDENCE-014#observation-02",
  decisionBoundary:
    "SYN-DECISION-021: report only the stated fixture observation; never claim external validation",
  pendingTaskId: "SYN-PENDING-034",
  pendingTaskText:
    "after compaction, repeat the exact evidence reference and decision boundary; do not run tools or take external action",
});
const PENDING_TASK_MARKERS = [
  "repeat the exact evidence reference and decision boundary",
  "do not run tools or take external action",
];
const DECISION_MARKERS = [
  "SYN-DECISION-021",
  "report only the stated fixture observation",
  "never claim external validation",
];

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function estimateUserTextTokens(text) {
  return estimateTokens({ role: "user", content: text, timestamp: 0 });
}

function isolatedResourceLoader() {
  return {
    getExtensions: () => ({ extensions: [], errors: [], runtime: createExtensionRuntime() }),
    getSkills: () => ({ skills: [], diagnostics: [] }),
    getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }),
    getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () =>
      "You are handling a synthetic-memory smoke fixture only. Use no tools and make no external claims.",
    getSystemPromptSource: () => undefined,
    getAppendSystemPrompt: () => [],
    getAppendSystemPromptSources: () => [],
    extendResources: () => {},
    reload: async () => {},
  };
}

function assistantText(message) {
  if (!message || message.role !== "assistant") return "";
  if (typeof message.content === "string") return message.content.trim();
  if (!Array.isArray(message.content)) return "";
  return message.content
    .filter((part) => part?.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n")
    .trim();
}

function latestAssistantMessage(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.role === "assistant" && assistantText(messages[index])) return messages[index];
  }
  throw new Error("Pi returned no assistant text response");
}

function numeric(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function usageSummary(usage) {
  if (!usage || typeof usage !== "object") return null;
  return {
    inputTokens: numeric(usage.input),
    outputTokens: numeric(usage.output),
    cacheReadTokens: numeric(usage.cacheRead),
    cacheWriteTokens: numeric(usage.cacheWrite),
    totalTokens: numeric(usage.totalTokens),
  };
}

function fixturePreservationChecks(text) {
  return {
    evidenceId: text.includes(FIXTURE.evidenceId),
    evidenceRef: text.includes(FIXTURE.evidenceRef),
    decisionBoundary: DECISION_MARKERS.every((marker) => text.includes(marker)),
    pendingTaskId: text.includes(FIXTURE.pendingTaskId),
    pendingTaskPurpose: text.includes(PENDING_TASK_MARKERS[0]),
    pendingTaskNoActionBoundary: text.includes(PENDING_TASK_MARKERS[1]),
  };
}

function sessionEntryRole(entry) {
  if (entry?.type !== "message") return undefined;
  return entry.message?.role;
}

function failureCategory(error) {
  const message = error instanceof Error ? error.message : "";
  if (/401|403|unauthori[sz]ed|api.?key|credential|auth/i.test(message)) {
    return "authentication-or-authorization failure; details withheld";
  }
  if (/429|rate.?limit/i.test(message)) return "rate limit; details withheld";
  if (/timeout|timed out|ETIMEDOUT/i.test(message)) return "request timeout; details withheld";
  if (/network|fetch|socket|ECONN/i.test(message)) return "network or transport failure; details withheld";
  if (/not registered/i.test(message)) return "requested Pi model is not registered";
  if (/nothing to compact|already compacted/i.test(message)) return "Pi native compaction had no eligible history";
  if (/assertion|expected|must be|was not|did not|missing/i.test(message)) return "smoke assertion failed";
  return "runtime or model error; details withheld";
}

function isOwnedTempRoot(tempRoot) {
  const resolvedBase = path.resolve(os.tmpdir());
  const resolvedRoot = path.resolve(tempRoot);
  return (
    path.dirname(resolvedRoot) === resolvedBase &&
    path.basename(resolvedRoot).startsWith(TEMP_PREFIX)
  );
}

function isUnderOwnedTempRoot(tempRoot, candidate) {
  if (!isOwnedTempRoot(tempRoot)) return false;
  const relative = path.relative(path.resolve(tempRoot), path.resolve(candidate));
  return relative !== "" && relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}

function removeOwnedTempRoot(tempRoot) {
  assert(isOwnedTempRoot(tempRoot), "refusing cleanup outside this smoke's owned temp directory");
  fs.rmSync(path.resolve(tempRoot), { recursive: true, force: true });
}

function createEventObserver(session) {
  const events = [];
  const unsubscribe = session.subscribe((event) => {
    if (event.type === "compaction_start") {
      events.push({ type: event.type, reason: event.reason });
    } else if (event.type === "compaction_end") {
      events.push({
        type: event.type,
        reason: event.reason,
        aborted: event.aborted,
        hasResult: Boolean(event.result),
        tokensBefore: numeric(event.result?.tokensBefore),
        estimatedTokensAfter: numeric(event.result?.estimatedTokensAfter),
        summaryChars: typeof event.result?.summary === "string" ? event.result.summary.length : null,
        usage: usageSummary(event.result?.usage),
        errorCategory: event.errorMessage ? failureCategory(new Error(event.errorMessage)) : null,
      });
    }
  });
  return { events, unsubscribe };
}

async function promptOnce(session, prompt, calls, label) {
  assert(calls.normalPromptsAttempted < MAX_NORMAL_PROMPTS, "normal prompt limit exceeded");
  calls.normalPromptsAttempted += 1;
  const startIndex = session.messages.length;
  const startedAt = performance.now();
  await session.prompt(prompt);
  await session.waitForIdle();
  const message = latestAssistantMessage(session.messages.slice(startIndex));
  calls.normalPromptsCompleted += 1;
  return {
    label,
    response: assistantText(message),
    usage: usageSummary(message.usage),
    estimatedPromptTokens: estimateUserTextTokens(prompt),
    elapsedMs: Math.round(performance.now() - startedAt),
  };
}

async function disposeSession(session) {
  if (session) {
    try {
      await session.dispose();
    } catch {
      // Disposal is best effort; test evidence is already in the session file.
    }
  }
}

async function run() {
  const result = {
    status: "failed",
    runtime: {
      node: process.versions.node,
      platform: process.platform,
      arch: process.arch,
      piPackage: null,
    },
    model: null,
    isolation: null,
    calls: {
      normalPromptsAttempted: 0,
      normalPromptsCompleted: 0,
      maxNormalPrompts: MAX_NORMAL_PROMPTS,
      forcedCompactionsAttempted: 0,
      forcedCompactionsCompleted: 0,
      maxForcedCompactions: MAX_FORCED_COMPACTIONS,
    },
    normalTurns: [],
    compactionEvents: [],
    compaction: null,
    reload: null,
    tokenBudget: null,
    checks: {},
    failure: null,
    tempArtifacts: null,
  };

  let tempRoot;
  let keepTemp = true;
  let session;
  let unsubscribe;
  let sessionManager;

  try {
    assert(process.versions.node === "22.19.0", "run this smoke with the pinned Node 22.19.0 binary");

    const piPackage = JSON.parse(fs.readFileSync(PI_PACKAGE_JSON, "utf8"));
    result.runtime.piPackage = piPackage.version;

    tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), TEMP_PREFIX));
    const cwd = path.join(tempRoot, "workspace");
    const agentDir = path.join(tempRoot, "agent");
    const sessionDir = path.join(tempRoot, "session-store");
    fs.mkdirSync(cwd, { recursive: true });
    fs.mkdirSync(agentDir, { recursive: true });
    fs.mkdirSync(sessionDir, { recursive: true });

    result.isolation = {
      cwdIsTemporary: isUnderOwnedTempRoot(tempRoot, cwd),
      agentDirIsTemporaryAndInitiallyEmpty:
        isUnderOwnedTempRoot(tempRoot, agentDir) && fs.readdirSync(agentDir).length === 0,
      sessionStoreIsTemporaryAndInitiallyEmpty:
        isUnderOwnedTempRoot(tempRoot, sessionDir) && fs.readdirSync(sessionDir).length === 0,
      workspaceInitiallyEmpty: fs.readdirSync(cwd).length === 0,
      settingsStorage: "in-memory",
      resourceLoader: "empty extensions, skills, prompts, themes, and agents files",
      credentials: "existing Pi ModelRuntime auth reused; no credential value read or emitted by this script",
      tempCleanedOnSuccess: false,
    };
    assert(result.isolation.cwdIsTemporary, "workspace cwd is not isolated under the owned temp directory");
    assert(result.isolation.agentDirIsTemporaryAndInitiallyEmpty, "temporary agent directory is not empty");
    assert(result.isolation.sessionStoreIsTemporaryAndInitiallyEmpty, "temporary session store is not empty");
    assert(result.isolation.workspaceInitiallyEmpty, "temporary workspace is not empty");

    // Reuse Pi's configured auth and model catalog, but skip unrelated model-catalog refresh traffic.
    const modelRuntime = await ModelRuntime.create({ refreshOnCreate: false });
    const model = modelRuntime.getModel("deepseek", "deepseek-v4-flash");
    assert(model, "deepseek/deepseek-v4-flash is not registered in Pi ModelRuntime");
    result.model = {
      provider: "deepseek",
      id: "deepseek-v4-flash",
      contextWindowReadOnly: numeric(model.contextWindow),
      maxTokensReadOnly: numeric(model.maxTokens),
      modelCapacityOverridden: false,
      thinkingLevel: "off",
    };

    const resourceLoader = isolatedResourceLoader();
    const settingsManager = SettingsManager.inMemory({
      compaction: {
        enabled: false,
        reserveTokens: 1_024,
        keepRecentTokens: KEEP_RECENT_TOKENS,
      },
      retry: { enabled: false },
    });
    sessionManager = SessionManager.create(cwd, sessionDir);
    const created = await createAgentSession({
      cwd,
      agentDir,
      model,
      modelRuntime,
      thinkingLevel: "off",
      noTools: "all",
      resourceLoader,
      sessionManager,
      settingsManager,
    });
    session = created.session;
    const activeToolNames = session.getActiveToolNames();
    result.isolation.activeToolNames = activeToolNames;
    result.isolation.activeToolCount = activeToolNames.length;
    assert(activeToolNames.length === 0, "Pi session unexpectedly has active tools");
    result.checks = { noActiveTools: true, emptyTempResources: true };

    const seedPrompt = `Synthetic fixture only; this is not real research data.
Remember these exact records for the next turn:
Evidence ID: ${FIXTURE.evidenceId}
Evidence reference: ${FIXTURE.evidenceRef}
Decision boundary: ${FIXTURE.decisionBoundary}
Named pending task: ${FIXTURE.pendingTaskId} (${FIXTURE.pendingTaskText})
Do not execute the pending task or make external claims. Reply with exactly ACK.`;
    const replayPrompt = `Using only the context that survives the compaction, return exactly four lines labeled EVIDENCE_ID, EVIDENCE_REF, DECISION_BOUNDARY, and PENDING_TASK with their exact stored values. If a value is unavailable, write MISSING. Do not execute the pending task.`;
    const compactionInstructions = "The history contains synthetic evidence records, a decision boundary, and a named pending task. Preserve those records verbatim, including their identifiers, references, purpose, and no-action constraints. Use only records present in the history; do not invent or fill missing values. Perform no external action.";
    const userTextEstimates = {
      seedPrompt: estimateUserTextTokens(seedPrompt),
      replayPrompt: estimateUserTextTokens(replayPrompt),
      compactionInstructions: estimateUserTextTokens(compactionInstructions),
    };
    const sentTextEstimate = Object.values(userTextEstimates).reduce((total, count) => total + count, 0);
    assert(sentTextEstimate < MAX_INPUT_TOKENS, "synthetic fixture text exceeds the 3,000-token smoke budget");

    const calls = result.calls;
    const seedTurn = await promptOnce(session, seedPrompt, calls, "seed");
    result.normalTurns.push({
      label: seedTurn.label,
      usage: seedTurn.usage,
      estimatedPromptTokens: seedTurn.estimatedPromptTokens,
      elapsedMs: seedTurn.elapsedMs,
      exactAck: seedTurn.response === "ACK",
    });
    assert(seedTurn.response === "ACK", "seed turn did not return the required neutral ACK");
    result.checks.seedAckContainsNoFixtureValues = !Object.values(FIXTURE).some((value) => seedTurn.response.includes(value));
    assert(result.checks.seedAckContainsNoFixtureValues, "seed response echoed fixture values outside the summary");

    const branchBeforeCompaction = sessionManager.getBranch();
    const assistantEntry = [...branchBeforeCompaction]
      .reverse()
      .find((entry) => sessionEntryRole(entry) === "assistant");
    assert(assistantEntry?.id, "could not identify the retained assistant turn before compaction");

    const observer = createEventObserver(session);
    result.compactionEvents = observer.events;
    unsubscribe = observer.unsubscribe;
    calls.forcedCompactionsAttempted += 1;
    assert(calls.forcedCompactionsAttempted <= MAX_FORCED_COMPACTIONS, "forced compaction limit exceeded");
    const compactionStartedAt = performance.now();
    const compacted = await session.compact(compactionInstructions);
    calls.forcedCompactionsCompleted += 1;
    const compactedBranch = sessionManager.getBranch();
    const compactionEntry = [...compactedBranch].reverse().find((entry) => entry.type === "compaction");
    const summary = typeof compacted.summary === "string" ? compacted.summary : "";
    const summaryChecks = fixturePreservationChecks(summary);
    const endEvents = observer.events.filter((event) => event.type === "compaction_end");
    result.compaction = {
      reason: endEvents.at(-1)?.reason ?? null,
      aborted: endEvents.at(-1)?.aborted ?? null,
      eventStartCount: observer.events.filter((event) => event.type === "compaction_start").length,
      eventEndCount: endEvents.length,
      summaryChars: summary.length,
      summaryIsNonEmptyAndNotTheOriginalPrompt: summary.trim().length > 0 && summary !== seedPrompt,
      summaryContainsFixture: summaryChecks,
      firstKeptEntryIsPriorAssistant: compacted.firstKeptEntryId === assistantEntry.id,
      persistedCompactionEntryExists: Boolean(compactionEntry?.summary),
      tokensBefore: numeric(compacted.tokensBefore),
      estimatedTokensAfter: numeric(compacted.estimatedTokensAfter),
      usage: usageSummary(compacted.usage),
      elapsedMs: Math.round(performance.now() - compactionStartedAt),
    };
    result.checks.manualNativeCompactionEvent =
      result.compaction.reason === "manual" &&
      result.compaction.aborted === false &&
      result.compaction.eventStartCount === 1 &&
      result.compaction.eventEndCount === 1;
    result.checks.nonNoopSummaryPersisted =
      result.compaction.summaryIsNonEmptyAndNotTheOriginalPrompt &&
      result.compaction.persistedCompactionEntryExists &&
      result.compaction.firstKeptEntryIsPriorAssistant &&
      (result.compaction.tokensBefore ?? 0) > 0;
    result.checks.summaryContainsAllFixtureRecords = Object.values(summaryChecks).every(Boolean);
    assert(result.checks.manualNativeCompactionEvent, "native manual compaction event was missing, aborted, or duplicated");
    assert(result.checks.nonNoopSummaryPersisted, "Pi did not persist a non-empty native compaction summary");
    assert(result.checks.summaryContainsAllFixtureRecords, "native summary did not preserve every exact fixture record");

    const sessionFile = session.sessionFile;
    assert(sessionFile && fs.existsSync(sessionFile), "Pi session file was not persisted for the reload check");
    unsubscribe();
    unsubscribe = undefined;
    await disposeSession(session);
    session = undefined;

    const reopenedManager = SessionManager.open(sessionFile, sessionDir, cwd);
    const reloaded = await createAgentSession({
      cwd,
      agentDir,
      model,
      modelRuntime,
      thinkingLevel: "off",
      noTools: "all",
      resourceLoader,
      sessionManager: reopenedManager,
      settingsManager,
    });
    session = reloaded.session;
    const reloadedCompaction = [...reopenedManager.getBranch()]
      .reverse()
      .find((entry) => entry.type === "compaction");
    const reloadedTools = session.getActiveToolNames();
    result.reload = {
      sessionFileReopened: true,
      compactionEntryPresent: Boolean(reloadedCompaction),
      summaryMatchesBeforeReload: reloadedCompaction?.summary === summary,
      activeToolCount: reloadedTools.length,
    };
    result.checks.persistentReload =
      result.reload.compactionEntryPresent && result.reload.summaryMatchesBeforeReload;
    result.checks.noToolsAfterReload = reloadedTools.length === 0;
    assert(result.checks.persistentReload, "compaction summary did not survive persistent session reload");
    assert(result.checks.noToolsAfterReload, "reloaded isolated session unexpectedly has active tools");

    const replayTurn = await promptOnce(session, replayPrompt, calls, "post-reload-replay");
    result.normalTurns.push({
      label: replayTurn.label,
      usage: replayTurn.usage,
      estimatedPromptTokens: replayTurn.estimatedPromptTokens,
      elapsedMs: replayTurn.elapsedMs,
    });
    const replayChecks = fixturePreservationChecks(replayTurn.response);
    result.reload.replayPreservation = replayChecks;
    result.checks.postReloadTurnPreservesAllFixtureRecords = Object.values(replayChecks).every(Boolean);
    assert(result.checks.postReloadTurnPreservesAllFixtureRecords, "post-reload turn lost a required exact fixture record");

    const summaryEstimate = estimateUserTextTokens(summary);
    const syntheticPayloadEstimate =
      userTextEstimates.seedPrompt * 2 +
      userTextEstimates.replayPrompt +
      userTextEstimates.compactionInstructions +
      summaryEstimate;
    const callUsages = [seedTurn.usage, usageSummary(compacted.usage), replayTurn.usage];
    const knownInputTokens = callUsages.reduce((total, usage) => total + (usage?.inputTokens ?? 0), 0);
    const allCallInputUsageReturned = callUsages.every((usage) => usage?.inputTokens !== null && usage?.inputTokens !== undefined);
    const measuredTotalInputTokens = allCallInputUsageReturned ? knownInputTokens : null;
    result.tokenBudget = {
      maxInputTokens: MAX_INPUT_TOKENS,
      piEstimator: "Pi estimateTokens character-based heuristic for the synthetic prompt/summary payload",
      estimates: {
        ...userTextEstimates,
        summary: summaryEstimate,
        syntheticPayloadAcrossCalls: syntheticPayloadEstimate,
        compactedSourceContextTokens: numeric(compacted.tokensBefore),
      },
      measuredInputTokensByCall: callUsages.map((usage) => usage?.inputTokens ?? null),
      measuredTotalInputTokens,
      measuredUsageReturnedForAllCalls: allCallInputUsageReturned,
    };
    result.checks.syntheticPayloadUnder3k = syntheticPayloadEstimate < MAX_INPUT_TOKENS;
    result.checks.measuredInputUnder3k =
      measuredTotalInputTokens === null || measuredTotalInputTokens <= MAX_INPUT_TOKENS;
    assert(result.checks.syntheticPayloadUnder3k, "estimated synthetic payload exceeded 3,000 tokens");
    assert(result.checks.measuredInputUnder3k, "reported runtime input usage exceeded 3,000 tokens");

    result.calls.normalPromptLimitRespected = calls.normalPromptsAttempted <= MAX_NORMAL_PROMPTS;
    result.calls.forcedCompactionLimitRespected = calls.forcedCompactionsAttempted <= MAX_FORCED_COMPACTIONS;
    result.status = "passed";
    keepTemp = false;
  } catch (error) {
    result.failure = {
      type: error instanceof Error ? error.name : "Error",
      category: failureCategory(error),
    };
    if (tempRoot) result.tempArtifacts = { preservedForFailureReview: true, path: tempRoot };
  } finally {
    if (unsubscribe) {
      try {
        unsubscribe();
      } catch {
        // Preserve the primary test result.
      }
    }
    await disposeSession(session);
    if (tempRoot && !keepTemp) {
      try {
        removeOwnedTempRoot(tempRoot);
        if (result.isolation) result.isolation.tempCleanedOnSuccess = true;
        result.tempArtifacts = { preservedForFailureReview: false, cleanedAfterPass: true };
      } catch {
        result.tempArtifacts = { preservedForFailureReview: true, cleanupIssue: "safe temp cleanup failed" };
      }
    }
  }

  process.stdout.write(`${JSON.stringify(result)}\n`);
  if (result.status !== "passed") process.exitCode = 1;
}

export { FIXTURE, fixturePreservationChecks, isolatedResourceLoader, disposeSession };

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) run().catch(() => {
  // Do not print raw exceptions: provider/runtime errors could contain sensitive request details.
  process.stdout.write(
    `${JSON.stringify({ status: "failed", failure: { category: "unhandled runtime error; details withheld" } })}\n`,
  );
  process.exitCode = 1;
});
