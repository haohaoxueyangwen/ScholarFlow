import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "typebox";

import type { ScholarFlowPiAPI } from "./pi-compat.ts";
import {
  RESEARCH_WORKER_TOOL_NAMES,
  RESEARCH_WORKER_MAX_TOOL_CALLS,
  RESEARCH_WORKER_MAX_TURNS,
  buildResearchWorkerPrompt,
  extractSuccessfulToolEvidenceRefs,
  isResearchWorkerAllowedTool,
  normalizeResearchFacets,
  capOpenResearchFacets,
  parseResearchWorkerBundle,
  recoverWorkerBundleFromToolEvidence,
  summarizeResearchWorkerRuns,
} from "./research-worker-contract.js";
import { searchScholarlyWorks } from "./scholarly-search.js";

const DEFAULT_TIMEOUT_MS = 180_000;
const MAX_STDERR_CHARACTERS = 4_000;
const WORKER_SYSTEM_PROMPT = "You are a short-lived, read-only ScholarFlow evidence worker. Use only the active evidence tools within their fixed budget. Your final response must be the exact JSON object requested by the user prompt: no narration, no Markdown, no unsupported claims, and no state changes.";
const extensionDir = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(extensionDir, "..", "..", "..");

type Usage = {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  reasoning: number;
  totalTokens: number;
  cost: number;
  turns: number;
};

type WorkerRun = {
  facet: string;
  status: "ok" | "process_error" | "timeout" | "aborted" | "format_error" | "evidence_empty" | "boundary_violation" | "tool_limit" | "turn_limit";
  exit_code: number | null;
  model: string;
  provider: string;
  tool_names: string[];
  tool_call_count: number;
  tool_error_count: number;
  usage: Usage;
  elapsed_ms: number;
  bundle: Record<string, unknown> | null;
  error: string | null;
  raw_excerpt?: string | null;
};

const FacetSchema = Type.Object(
  {
    name: Type.String({ minLength: 1, maxLength: 80, description: "Unique short name for one independent evidence surface." }),
    focus: Type.String({ minLength: 1, maxLength: 1_200, description: "Non-overlapping scope assigned to this worker." }),
    queries: Type.Optional(Type.Array(Type.String({ maxLength: 240 }), { maxItems: 4 })),
    // Accepted for compatibility with models that naturally place a
    // per-worker option next to the worker facet. The delegate collapses it
    // into the single bounded run-level limit below.
    max_sources_per_worker: Type.Optional(Type.Integer({ minimum: 2, maximum: 8 })),
  },
  { additionalProperties: false },
);

const ResearchDelegateSchema = Type.Object(
  {
    question: Type.String({ minLength: 1, maxLength: 4_000, description: "Shared research question for all workers." }),
    facets: Type.Array(FacetSchema, {
      minItems: 2,
      maxItems: 4,
      description: "Two to four independent evidence surfaces. Do not create duplicate or sequential facets.",
    }),
    project_stage_id: Type.Optional(Type.String({ maxLength: 160 })),
    max_sources_per_worker: Type.Optional(Type.Integer({ minimum: 2, maximum: 8, default: 5 })),
  },
  { additionalProperties: false },
);

export function registerResearchWorkers(pi: ScholarFlowPiAPI) {
  if (isResearchWorkerProcess()) {
    registerScholarlySearchTool(pi);
    return;
  }
  pi.registerTool({
    name: "sf_research_delegate",
    label: "ScholarFlow Open Research",
    description: "Run 2-4 isolated, short-lived, read-only Pi research workers over independent evidence surfaces and return structured evidence bundles with aggregate usage. Prefer 2 workers, use 3 for a clearly broad question, and use 4 only when the user explicitly supplied four independent scopes.",
    promptSnippet: "Use once for broad open research when independent evidence surfaces materially improve coverage; prefer 2 workers and do not use for stored-only status, a single lookup, or a sequential task.",
    executionMode: "parallel",
    parameters: ResearchDelegateSchema,
    async execute(_toolCallId, params, signal, onUpdate, _ctx) {
      const question = String(params.question ?? "").trim();
      let facets;
      try {
        facets = capOpenResearchFacets(normalizeResearchFacets(params.facets), question);
      } catch (error) {
        return {
          content: [{ type: "text" as const, text: error instanceof Error ? error.message : String(error) }],
          details: { schema_version: 1, read_only_enforced: true, workers: [] },
          isError: true,
        };
      }
      const nestedMaxSources = params.facets
        .map((facet) => facet.max_sources_per_worker)
        .find((value) => Number.isInteger(value));
      const maxSources = params.max_sources_per_worker ?? nestedMaxSources ?? 5;
      const activeWorkerTools = configuredWorkerTools();
      const started = Date.now();
      const states = facets.map((facet) => ({ facet: facet.name, status: "queued" }));
      const emitUpdate = () => onUpdate?.({
        content: [{
          type: "text" as const,
          text: `Open research workers: ${states.filter((item) => item.status === "ok").length}/${states.length} complete; ${states.filter((item) => item.status === "running").length} running.`,
        }],
        details: { schema_version: 1, read_only_enforced: true, workers: states },
      });
      const runs = await Promise.all(facets.map(async (facet, index) => {
        states[index].status = "running";
        emitUpdate();
        const run = await runResearchWorker({
          question,
          facet,
          projectStageId: params.project_stage_id,
          maxSources,
          signal,
        });
        states[index].status = run.status;
        emitUpdate();
        return run;
      }));
      const summary = summarizeResearchWorkerRuns(runs);
      const details = {
        ...summary,
        elapsed_ms: Date.now() - started,
        allowed_tools: activeWorkerTools,
        workers: runs,
      };
      const publicPayload = {
        ...summary,
        elapsed_ms: details.elapsed_ms,
        workers: runs.map((run) => ({
          facet: run.facet,
          status: run.status,
          tool_names: run.tool_names,
          tool_call_count: run.tool_call_count,
          usage: run.usage,
          bundle: run.bundle,
          error: run.error,
        })),
      };
      return {
        content: [{ type: "text" as const, text: JSON.stringify(publicPayload, null, 2) }],
        details,
        isError: summary.successful_worker_count === 0,
      };
    },
  });
}

function registerScholarlySearchTool(pi: ScholarFlowPiAPI) {
  pi.registerTool({
    name: "sf_research_search",
    label: "ScholarFlow Scholarly Search",
    description: "Search OpenAlex for scholarly works over one to four queries and return bounded structured metadata with stable DOI/OpenAlex evidence refs. Read-only and keyless for casual use; OPENALEX_API_KEY is optional.",
    promptSnippet: "Use once as the primary evidence call in a research worker; prefer 2-4 focused queries and cite only returned refs.",
    parameters: Type.Object({
      queries: Type.Array(Type.String({ minLength: 1, maxLength: 400 }), { minItems: 2, maxItems: 4 }),
      max_results_per_query: Type.Optional(Type.Integer({ minimum: 1, maximum: 4, default: 2 })),
    }, { additionalProperties: false }),
    async execute(_toolCallId, params, signal) {
      const result = await searchScholarlyWorks({
        queries: params.queries,
        maxResultsPerQuery: params.max_results_per_query,
        signal,
      });
      return {
        content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }],
        details: {
          schema_version: 1,
          provider: result.provider,
          read_only: true,
          query_count: result.query_count,
          result_count: result.result_count,
          query_error_count: result.query_errors.length,
          source_refs: result.items.map((item) => item.ref),
        },
        isError: result.result_count === 0 && result.query_errors.length === result.query_count,
      };
    },
  });
}

export function isResearchWorkerProcess() {
  return String(process.env.SCHOLARFLOW_RESEARCH_WORKER ?? "").trim() === "1";
}

async function runResearchWorker({ question, facet, projectStageId, maxSources, signal }: {
  question: string;
  facet: { name: string; focus: string; queries: string[] };
  projectStageId?: string;
  maxSources: number;
  signal?: AbortSignal;
}): Promise<WorkerRun> {
  const provider = process.env.SCHOLARFLOW_RESEARCH_WORKER_PROVIDER
    || process.env.SCHOLARFLOW_PI_PROVIDER
    || "deepseek";
  const model = process.env.SCHOLARFLOW_RESEARCH_WORKER_MODEL
    || process.env.SCHOLARFLOW_PI_MODEL
    || "deepseek-v4-flash";
  const thinking = process.env.SCHOLARFLOW_RESEARCH_WORKER_THINKING?.trim() || "off";
  const timeoutMs = boundedInteger(
    process.env.SCHOLARFLOW_RESEARCH_WORKER_TIMEOUT_MS,
    30_000,
    600_000,
    DEFAULT_TIMEOUT_MS,
  );
  const prompt = buildResearchWorkerPrompt({ question, facet, projectStageId, maxSources });
  const activeWorkerTools = configuredWorkerTools();
  const explicitExtensions = workerExtensionPaths();
  const args = [
    "--mode", "json",
    "-p",
    "--no-session",
    "--no-builtin-tools",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-context-files",
    "--approve",
    ...explicitExtensions.flatMap((extensionPath) => ["--extension", extensionPath]),
    "--system-prompt", WORKER_SYSTEM_PROMPT,
    "--provider", provider,
    "--model", model,
    "--thinking", thinking,
    "--tools", activeWorkerTools.join(","),
    prompt,
  ];
  const invocation = getPiInvocation(args);
  const usage: Usage = {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    reasoning: 0,
    totalTokens: 0,
    cost: 0,
    turns: 0,
  };
  const toolNames = new Set<string>();
  const observedSourceRefs = new Set<string>();
  let toolErrorCount = 0;
  let toolCallCount = 0;
  let toolLimitExceeded = false;
  let turnLimitExceeded = false;
  let finalText = "";
  let stderr = "";
  let timedOut = false;
  let aborted = false;
  const started = Date.now();
  const workerAgentDir = await prepareWorkerAgentDir(activeWorkerTools);
  const workerCwd = path.join(workerAgentDir, "workspace");
  await fs.promises.mkdir(workerCwd, { recursive: true });
  let exitCode: number | null;
  try {
    exitCode = await new Promise<number | null>((resolve) => {
      const child = spawn(invocation.command, invocation.args, {
        // Run outside the parent project so its interactive permission policy
        // cannot override the worker's deny-by-default headless policy. Only
        // the three explicit, reviewed extensions below are loaded.
        cwd: workerCwd,
        env: {
          ...process.env,
          PI_CODING_AGENT_DIR: workerAgentDir,
          SCHOLARFLOW_RESEARCH_WORKER: "1",
        },
        shell: false,
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      });
      let stdoutBuffer = "";
      let settled = false;
      const finish = (code: number | null) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        signal?.removeEventListener("abort", abortHandler);
        if (stdoutBuffer.trim()) consumeLine(stdoutBuffer);
        resolve(code);
      };
      const terminate = () => {
        child.kill();
        setTimeout(() => child.kill("SIGKILL"), 5_000).unref();
      };
      const abortHandler = () => {
        aborted = true;
        terminate();
      };
      const timeout = setTimeout(() => {
        timedOut = true;
        terminate();
      }, timeoutMs);
      const consumeLine = (line: string) => {
        if (!line.trim()) return;
        let event: any;
        try {
          event = JSON.parse(line);
        } catch {
          return;
        }
        if (event.type === "tool_execution_start" && event.toolName) {
          toolNames.add(String(event.toolName));
          toolCallCount += 1;
          if (toolCallCount > RESEARCH_WORKER_MAX_TOOL_CALLS) {
            toolLimitExceeded = true;
          }
        }
        if (event.type === "tool_execution_end") {
          if (event.isError) {
            toolErrorCount += 1;
          } else {
            for (const ref of extractSuccessfulToolEvidenceRefs(event)) observedSourceRefs.add(ref);
          }
        }
        if (event.type !== "message_end" || !event.message || event.message.role !== "assistant") return;
        usage.turns += 1;
        if (usage.turns > RESEARCH_WORKER_MAX_TURNS) {
          turnLimitExceeded = true;
          terminate();
          return;
        }
        const messageUsage = event.message.usage ?? {};
        usage.input += Number(messageUsage.input ?? 0);
        usage.output += Number(messageUsage.output ?? 0);
        usage.cacheRead += Number(messageUsage.cacheRead ?? 0);
        usage.cacheWrite += Number(messageUsage.cacheWrite ?? 0);
        usage.reasoning += Number(messageUsage.reasoning ?? 0);
        usage.totalTokens += Number(messageUsage.totalTokens ?? 0);
        usage.cost += Number(messageUsage.cost?.total ?? 0);
        const textParts: string[] = [];
        for (const part of event.message.content ?? []) {
          if (part?.type === "toolCall" && part.name) toolNames.add(String(part.name));
          if (part?.type === "text" && part.text) textParts.push(String(part.text));
        }
        if (textParts.length) finalText = textParts.join("\n");
      };
      child.stdout.on("data", (chunk) => {
        stdoutBuffer += chunk.toString();
        const lines = stdoutBuffer.split(/\r?\n/);
        stdoutBuffer = lines.pop() ?? "";
        for (const line of lines) consumeLine(line);
      });
      child.stderr.on("data", (chunk) => {
        stderr = `${stderr}${chunk.toString()}`.slice(-MAX_STDERR_CHARACTERS);
      });
      child.on("error", (error) => {
        stderr = `${stderr}\n${error.message}`.trim().slice(-MAX_STDERR_CHARACTERS);
        finish(1);
      });
      child.on("close", finish);
      if (signal?.aborted) abortHandler();
      else signal?.addEventListener("abort", abortHandler, { once: true });
    });
  } finally {
    await fs.promises.rm(workerAgentDir, { recursive: true, force: true });
  }
  let parsed = parseResearchWorkerBundle(finalText, facet.name, observedSourceRefs, maxSources);
  if (
    (parsed.status !== "ok"
      || !Array.isArray(parsed.bundle?.sources) || parsed.bundle.sources.length === 0
      || !Array.isArray(parsed.bundle?.findings) || parsed.bundle.findings.length === 0)
    && observedSourceRefs.size > 0
  ) {
    const recovered = recoverWorkerBundleFromToolEvidence(
      facet.name,
      observedSourceRefs,
      parsed.error || "worker JSON was missing source-grounded findings",
    );
    if (recovered) parsed = { status: "ok", bundle: recovered, error: null, raw_excerpt: parsed.raw_excerpt };
  }
  const forbiddenTools = [...toolNames].filter((name) => !isResearchWorkerAllowedTool(name));
  const processStatus = aborted
    ? "aborted"
    : timedOut
      ? "timeout"
      : forbiddenTools.length
        ? "boundary_violation"
      : toolLimitExceeded
        ? "tool_limit"
        : turnLimitExceeded
          ? "turn_limit"
          : exitCode !== 0
            ? "process_error"
          : parsed.status === "ok" && (
              !Array.isArray(parsed.bundle?.sources) || parsed.bundle.sources.length === 0
              || !Array.isArray(parsed.bundle?.findings) || parsed.bundle.findings.length === 0
            )
            ? "evidence_empty"
            : parsed.status;
  return {
    facet: facet.name,
    status: processStatus,
    exit_code: exitCode,
    model,
    provider,
    tool_names: [...toolNames].sort(),
    tool_call_count: toolCallCount,
    tool_error_count: toolErrorCount,
    usage,
    elapsed_ms: Date.now() - started,
    bundle: parsed.bundle,
    error: processStatus === "ok"
      ? null
      : processStatus === "evidence_empty"
        ? "worker returned no source-grounded findings"
        : processStatus === "tool_limit"
          ? `worker exceeded the fixed ${RESEARCH_WORKER_MAX_TOOL_CALLS}-call evidence budget`
        : processStatus === "turn_limit"
            ? `worker exceeded the fixed ${RESEARCH_WORKER_MAX_TURNS}-turn budget`
            : processStatus === "boundary_violation"
              ? `worker attempted tools outside the read-only boundary: ${forbiddenTools.join(", ")}`
              : parsed.error || stderr || `worker exited with code ${exitCode}`,
    raw_excerpt: parsed.raw_excerpt,
  };
}

function getPiInvocation(args: string[]) {
  const currentScript = process.argv[1];
  if (currentScript && fs.existsSync(currentScript)) {
    return { command: process.execPath, args: [currentScript, ...args] };
  }
  const executable = path.basename(process.execPath).toLowerCase();
  if (!/^(node|bun)(\.exe)?$/.test(executable)) return { command: process.execPath, args };
  return { command: "pi", args };
}

function configuredWorkerTools() {
  const knowledgeEnabled = String(process.env.SCHOLARFLOW_KNOWLEDGE_ENGINE ?? "").trim().toLowerCase() === "lightrag";
  return knowledgeEnabled
    ? [...RESEARCH_WORKER_TOOL_NAMES]
    : RESEARCH_WORKER_TOOL_NAMES.filter((name) => !name.startsWith("sf_knowledge_"));
}

function workerExtensionPaths() {
  const candidates = [
    path.join(extensionDir, "index.ts"),
    path.join(repositoryRoot, ".pi", "npm", "node_modules", "pi-web-access", "index.ts"),
    path.join(repositoryRoot, ".pi", "npm", "node_modules", "@gotgenes", "pi-permission-system", "src", "index.ts"),
  ];
  const missing = candidates.filter((candidate) => !fs.existsSync(candidate));
  if (missing.length) {
    throw new Error(`research worker extensions are unavailable: ${missing.join(", ")}`);
  }
  return candidates;
}

async function prepareWorkerAgentDir(activeWorkerTools: string[]) {
  const sourceAgentDir = process.env.PI_CODING_AGENT_DIR?.trim()
    || path.join(os.homedir(), ".pi", "agent");
  const agentDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), "scholarflow-research-worker-"));
  for (const name of ["auth.json", "models.json", "models-store.json", "web-search.json"]) {
    try {
      await fs.promises.copyFile(path.join(sourceAgentDir, name), path.join(agentDir, name));
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }
  const permissionDir = path.join(agentDir, "extensions", "pi-permission-system");
  await fs.promises.mkdir(permissionDir, { recursive: true });
  const explicitAllows = Object.fromEntries(
    activeWorkerTools.map((name) => [name, "allow"]),
  );
  const policy = {
    permissionReviewLog: true,
    yoloMode: false,
    permission: {
      "*": "deny",
      ...explicitAllows,
      path: "deny",
      external_directory: "deny",
      skill: "deny",
      mcp: "deny",
      bash: "deny",
      write: "deny",
      edit: "deny",
    },
  };
  await fs.promises.writeFile(
    path.join(permissionDir, "config.json"),
    `${JSON.stringify(policy, null, 2)}\n`,
    { encoding: "utf8", mode: 0o600 },
  );
  return agentDir;
}

function boundedInteger(value: string | undefined, minimum: number, maximum: number, fallback: number) {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(parsed) ? Math.max(minimum, Math.min(maximum, parsed)) : fallback;
}
