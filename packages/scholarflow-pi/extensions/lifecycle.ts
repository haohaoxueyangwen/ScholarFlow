import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { ScholarFlowPiAPI } from "./pi-compat.ts";

type LifecyclePayload = {
  status?: string;
  update_count?: number;
  blocked_count?: number;
  components?: Array<{ package?: string; current?: string; target?: string; action?: string }>;
  error?: string;
  phase?: string;
  counts?: Record<string, number>;
};

const extensionDir = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(extensionDir, "..", "..", "..");
const lifecycleScript = resolve(repositoryRoot, "scripts", "pi_lifecycle.py");

async function runLifecycle(pi: ScholarFlowPiAPI, args: string[], timeout: number): Promise<LifecyclePayload> {
  const result = await pi.exec(
    process.env.SCHOLARFLOW_PYTHON?.trim() || "python",
    [lifecycleScript, ...args, "--json"],
    { timeout },
  );
  const stdout = result.stdout.trim();
  let payload: LifecyclePayload | undefined;
  try { payload = JSON.parse(stdout) as LifecyclePayload; } catch { /* reported below */ }
  if (result.code !== 0 || !payload) {
    const detail = payload?.error || result.stderr.trim() || stdout || `exit code ${result.code}`;
    throw new Error(`ScholarFlow Pi lifecycle failed: ${detail}`);
  }
  return payload;
}

function updateSummary(payload: LifecyclePayload): string {
  const updates = (payload.components || []).filter((item) => item.action === "update" || item.action === "review_required");
  if (!updates.length) return "ScholarFlow Pi runtime and reviewed extensions are current.";
  return updates.map((item) => `${item.package}: ${item.current} → ${item.target} (${item.action})`).join("\n");
}

export function registerPiLifecycle(pi: ScholarFlowPiAPI): void {
  pi.registerCommand("scholarflow-update", {
    description: "Check, qualify, or apply controlled Pi and extension updates.",
    handler: async (rawArgs, ctx) => {
      const action = rawArgs.trim().toLowerCase() || "check";
      try {
        if (action === "status") {
          const payload = await runLifecycle(pi, ["status"], 30_000);
          ctx.ui.notify(`ScholarFlow runtime status: ${payload.status ?? "unknown"}.`, payload.status === "error" ? "error" : "info");
          return;
        }
        if (action === "audit") {
          const payload = await runLifecycle(pi, ["audit"], 240_000);
          ctx.ui.notify(`Pi dependency audit: ${payload.status ?? "unknown"} ${JSON.stringify(payload.counts ?? {})}`, payload.status === "pass" ? "info" : "warning");
          return;
        }
        if (action === "apply") {
          const accepted = await ctx.ui.confirm(
            "Qualify and update ScholarFlow Pi?",
            "A candidate runtime will be installed in isolation and all deterministic tests will run before project and global Pi versions change. Restart Pi after success.",
          );
          if (!accepted) return;
          const payload = await runLifecycle(pi, ["upgrade", "--apply", "--global", "--refresh"], 1_200_000);
          ctx.ui.notify(
            payload.status === "pass"
              ? "ScholarFlow Pi update passed. Restart Pi to use the updated runtime."
              : `Update stopped safely at ${payload.phase ?? "unknown"}: ${payload.error ?? payload.status ?? "failed"}`,
            payload.status === "pass" ? "info" : "error",
          );
          return;
        }
        const payload = await runLifecycle(pi, ["check", "--refresh"], 120_000);
        ctx.ui.notify(updateSummary(payload), payload.blocked_count ? "warning" : "info");
      } catch (error) {
        ctx.ui.notify(error instanceof Error ? error.message : String(error), "error");
      }
    },
  });

  pi.on("session_start", (_event, ctx) => {
    if (
      process.env.SCHOLARFLOW_DISABLE_UPDATE_CHECK === "1" ||
      process.env.PI_OFFLINE === "1" ||
      process.argv.includes("--offline")
    ) return;
    void runLifecycle(pi, ["check", "--cache-ttl-hours", "24"], 90_000)
      .then((payload) => {
        if (!payload.update_count && !payload.blocked_count) return;
        ctx.ui.notify(`${updateSummary(payload)}\nRun /scholarflow-update apply after review.`, "warning");
      })
      .catch(() => undefined);
  });
}
