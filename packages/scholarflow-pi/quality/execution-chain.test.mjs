/**
 * T5.2 / T5.3 — execution-chain integration:
 * model params → tool_call guard → registered execute → bridge args.
 * Blocked calls must not reach the bridge; session audit survives turns.
 */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";

import {
  getBlockedExecutionAudit,
  getSessionExecutionAudit,
} from "../extensions/execution-audit.js";
import { STAGE_BOUND_TOOLS } from "../extensions/turn-context.js";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const piPackageRoot = resolve(packageRoot, "node_modules/@earendil-works/pi-coding-agent");
const jitiEntry = require.resolve("jiti", { paths: [piPackageRoot] });
const { createJiti } = await import(pathToFileURL(jitiEntry).href);
const jiti = createJiti(import.meta.url);
const { default: registerScholarFlowExtension } = await jiti.import("../extensions/index.ts");

function bridgeResult(operation, data) {
  return {
    code: 0,
    stdout: JSON.stringify({ protocol_version: 1, ok: true, operation, data }),
    stderr: "",
  };
}

function createRuntime() {
  const hooks = new Map();
  const tools = new Map();
  const activeHistory = [];
  const execCalls = [];
  let activeTools = [];
  const builtins = ["memory_search", "session_search", "read", "write", "bash", "web_search"]
    .map((name) => ({ name }));

  const pi = {
    async exec(command, args, options) {
      execCalls.push({ command, args: [...args], options });
      const operation = args.find((arg) => [
        "project-get",
        "project-search",
        "artifact-search",
        "artifact-propose-publish",
        "artifact-proposal-get",
        "artifact-apply-publish",
      ].includes(arg));
      if (operation === "project-get") {
        const stageIndex = args.indexOf("--stage-id");
        const stageId = stageIndex >= 0 ? args[stageIndex + 1] : undefined;
        return bridgeResult(operation, {
          stage_id: stageId,
          project_name: stageId === "stage-eval-calibration" ? "Calibration Track" : "Eval Thesis",
          title: "fixture",
          next_actions: ["Continue"],
        });
      }
      if (operation === "project-search") {
        return bridgeResult(operation, {
          items: [{ stage_id: "stage-eval-optics", project_name: "Eval Thesis" }],
        });
      }
      if (operation === "artifact-search") return bridgeResult(operation, { items: [] });
      if (operation === "artifact-propose-publish") {
        return bridgeResult(operation, {
          kind: "research_plan",
          status: "draft",
          project_stage_id: "stage-eval-optics",
          evidence_refs: ["stage-eval-optics"],
        });
      }
      if (operation === "artifact-proposal-get") {
        return bridgeResult(operation, {
          operation: "artifact.apply_publish",
          target: { artifact_id: "artifact-fixture" },
          diff: { status: { before: "draft", after: "final" } },
        });
      }
      if (operation === "artifact-apply-publish") {
        return bridgeResult(operation, { artifact_id: "artifact-fixture", applied: true });
      }
      throw new Error(`Unexpected fake Pi execution: ${command} ${args.join(" ")}`);
    },
    getAllTools() {
      return [...builtins, ...tools.values()];
    },
    getActiveTools() {
      return [...activeTools];
    },
    setActiveTools(names) {
      activeTools = [...names];
      activeHistory.push([...names]);
    },
    on(event, handler) {
      const listeners = hooks.get(event) ?? [];
      listeners.push(handler);
      hooks.set(event, listeners);
    },
    registerCommand() {},
    registerTool(definition) {
      tools.set(definition.name, definition);
    },
  };

  registerScholarFlowExtension(pi);

  function beforeAgentStart(prompt) {
    const handler = hooks.get("before_agent_start")?.[0];
    assert.ok(handler, "extension registers before_agent_start");
    return handler({ prompt, systemPrompt: "base system prompt" });
  }

  function toolCall(toolName, input = {}) {
    const event = { toolName, input: { ...input } };
    let outcome;
    for (const handler of hooks.get("tool_call") ?? []) {
      const result = handler(event);
      if (result !== undefined) outcome = result;
    }
    return { outcome, event };
  }

  function toolResult(toolName, data, { isError = false } = {}) {
    for (const handler of hooks.get("tool_result") ?? []) {
      handler({
        toolName,
        isError,
        content: [],
        details: { data },
      });
    }
  }

  async function enterLane(lane) {
    const guarded = toolCall("sf_lane_enter", { lane });
    if (guarded.outcome?.block) return { ...guarded, result: undefined };
    const definition = tools.get("sf_lane_enter");
    assert.ok(definition, "extension registers sf_lane_enter");
    const result = await definition.execute("fixture-lane-call", { lane });
    return { ...guarded, result };
  }

  /**
   * Critical T5.2 path: execute uses post-guard event.input, never the pre-guard copy.
   */
  async function invokeTool(toolName, input = {}) {
    const call = toolCall(toolName, input);
    if (call.outcome?.block) {
      return { ...call, result: null, bridged: false };
    }
    const definition = tools.get(toolName);
    assert.ok(definition, `missing tool ${toolName}`);
    const before = execCalls.length;
    const result = await definition.execute(`fixture-${toolName}`, call.event.input);
    return {
      ...call,
      result,
      bridged: execCalls.length > before,
      bridgeCall: execCalls.at(-1),
    };
  }

  return {
    execCalls,
    beforeAgentStart,
    sessionStart() {
      for (const handler of hooks.get("session_start") ?? []) handler();
    },
    toolCall,
    toolResult,
    enterLane,
    invokeTool,
    getTool: (name) => tools.get(name),
    getActiveTools: () => [...activeTools],
  };
}

test("T5.2: filled claimed stage_id reaches bridge via post-guard execute input", async () => {
  const runtime = createRuntime();
  runtime.sessionStart();
  runtime.beforeAgentStart(
    "Project stage-eval-optics is already confirmed. Read its stored blocker and cite the stage id.",
  );
  await runtime.enterLane("project");

  const filled = await runtime.invokeTool("sf_project_get", {});
  assert.equal(filled.outcome?.block, undefined);
  assert.equal(filled.event.input.stage_id, "stage-eval-optics");
  assert.equal(filled.bridged, true);
  assert.ok(filled.bridgeCall.args.includes("project-get"));
  assert.equal(
    filled.bridgeCall.args[filled.bridgeCall.args.indexOf("--stage-id") + 1],
    "stage-eval-optics",
  );
  assert.equal(filled.result.details.execution_arguments.stage_id, "stage-eval-optics");

  const bridgeAudits = getSessionExecutionAudit().filter((entry) => entry.kind === "bridge_execute");
  assert.equal(bridgeAudits.at(-1)?.bridgeArguments?.stage_id, "stage-eval-optics");
});

test("T5.2: conflicting stage id is blocked and does not call bridge", async () => {
  const runtime = createRuntime();
  runtime.sessionStart();
  runtime.beforeAgentStart("Inspect stage-eval-optics.");
  await runtime.enterLane("project");
  runtime.toolResult("sf_project_get", {
    stage_id: "stage-eval-optics",
    project_name: "Eval Thesis",
  });

  const before = runtime.execCalls.length;
  const conflicted = await runtime.invokeTool("sf_project_get", {
    stage_id: "stage-eval-calibration",
  });
  assert.equal(conflicted.outcome?.block, true);
  assert.equal(conflicted.bridged, false);
  assert.equal(runtime.execCalls.length, before);
  assert.ok(getBlockedExecutionAudit("sf_project_get").length >= 1);
  const lastBlock = getBlockedExecutionAudit("sf_project_get").at(-1);
  assert.equal(lastBlock?.originalParams?.stage_id, "stage-eval-calibration");
  assert.equal(lastBlock?.executedParams?.stage_id, "stage-eval-calibration");
});

test("T5.2: truncated rewrite is blocked before bridge", async () => {
  const runtime = createRuntime();
  runtime.sessionStart();
  runtime.beforeAgentStart("Inspect stage-eval-optics.");
  await runtime.enterLane("project");

  const before = runtime.execCalls.length;
  const blocked = await runtime.invokeTool("sf_project_get", { stage_id: "eval-optics" });
  assert.equal(blocked.outcome?.block, true);
  assert.equal(runtime.execCalls.length, before);
});

test("T5.2: STAGE_BOUND_TOOLS param names match registered tool schemas", () => {
  const runtime = createRuntime();
  for (const [toolName, paramKey] of Object.entries(STAGE_BOUND_TOOLS)) {
    const tool = runtime.getTool(toolName);
    if (!tool) continue;
    const props = tool.parameters?.properties ?? tool.parameters?.type?.properties;
    // TypeBox objects expose .properties on the schema object used by Pi.
    const schemaProps = tool.parameters?.properties
      ?? tool.parameters?.[Object.getOwnPropertySymbols(tool.parameters)[0]]
      ?? null;
    const keys = schemaProps
      ? Object.keys(schemaProps)
      : Object.keys(tool.parameters ?? {});
    // Fallback: inspect TypeBox by stringifying common shape
    const encoded = JSON.stringify(tool.parameters ?? {});
    assert.match(
      encoded,
      new RegExp(`"${paramKey}"`),
      `${toolName} schema must declare ${paramKey}`,
    );
    void keys;
  }
});

test("T5.2 / T5.3: intercept audit survives the next turn and clears on session_start", async () => {
  const runtime = createRuntime();
  runtime.sessionStart();
  runtime.beforeAgentStart("Inspect stage-eval-optics.");
  await runtime.enterLane("project");
  await runtime.invokeTool("sf_project_get", { stage_id: "eval-optics" });
  const afterTurn1 = getBlockedExecutionAudit("sf_project_get").length;
  assert.ok(afterTurn1 >= 1);

  runtime.beforeAgentStart(
    "Now focus only on the second project from my original comparison request; cite its stage id.",
  );
  assert.equal(
    getBlockedExecutionAudit("sf_project_get").length,
    afterTurn1,
    "next turn must not wipe prior intercept records",
  );
  assert.ok(getSessionExecutionAudit().some((entry) => entry.turnId === 1));
  assert.ok(getSessionExecutionAudit().some((entry) => entry.turnId === 2));

  runtime.sessionStart();
  assert.deepEqual(getSessionExecutionAudit(), []);
});

test("T5.3: dual comparison then second-project follow-up re-reads without fuzzy search", async () => {
  const runtime = createRuntime();
  runtime.sessionStart();
  runtime.beforeAgentStart(
    "Compare the current blockers for Eval Thesis and Calibration Track. Ground the comparison in ScholarFlow and cite both stage ids.",
  );
  await runtime.enterLane("project");
  runtime.toolResult("sf_project_search", {
    items: [{ stage_id: "stage-eval-optics", project_name: "Eval Thesis" }],
  });
  runtime.toolResult("sf_project_search", {
    items: [{ stage_id: "stage-eval-calibration", project_name: "Calibration Track" }],
  });

  runtime.beforeAgentStart(
    "Now focus only on the second project from my original comparison request, Calibration Track. Give its next action and cite its stage id; do not mix in Eval Thesis task or experiment ids.",
  );
  const blockedSearch = runtime.toolCall("sf_project_search", { query: "Calibration Track" });
  assert.equal(blockedSearch.outcome?.block, true);

  const get = await runtime.invokeTool("sf_project_get", {});
  assert.equal(get.outcome?.block, undefined);
  assert.equal(get.event.input.stage_id, "stage-eval-calibration");
  assert.equal(get.bridged, true);
  assert.equal(
    get.bridgeCall.args[get.bridgeCall.args.indexOf("--stage-id") + 1],
    "stage-eval-calibration",
  );
});
