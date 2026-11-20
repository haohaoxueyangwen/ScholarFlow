import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { ScholarFlowPiAPI } from "./pi-compat.ts";

export type BridgePayload = {
  protocol_version: number;
  ok: boolean;
  operation: string;
  data?: unknown;
  error?: { code?: string; type?: string; message?: string; details?: unknown };
};

const extensionDir = dirname(fileURLToPath(import.meta.url));
const packageRoot = resolve(extensionDir, "..");
const repositoryRoot = resolve(packageRoot, "..", "..");
const bridgeRunner = resolve(packageRoot, "scripts", "bridge_runner.py");

function configuredDataDir(): string {
  const configured = process.env.SCHOLARFLOW_DATA_DIR?.trim();
  return configured ? resolve(repositoryRoot, configured) : resolve(repositoryRoot, ".tmp", "scholarflow-cli");
}

function configuredVaultDir(): string | undefined {
  const configured = process.env.SCHOLARFLOW_VAULT_DIR?.trim();
  return configured ? resolve(repositoryRoot, configured) : undefined;
}

export async function callBridge(
  pi: ScholarFlowPiAPI,
  commandArgs: string[],
  signal?: AbortSignal,
  requestId?: string,
): Promise<BridgePayload> {
  const hostArgs = [bridgeRunner, "--data-dir", configuredDataDir()];
  if (requestId) hostArgs.push("--request-id", requestId);
  const vaultDir = configuredVaultDir();
  if (vaultDir) hostArgs.push("--vault-dir", vaultDir);
  // A LightRAG mix query includes remote keyword extraction plus local CPU
  // reranking and is intentionally bounded by the knowledge-engine timeout.
  // The generic 15-second bridge budget caused successful long queries to be
  // observed as empty exit-code-0 tool results and retried by Pi.
  const timeoutMs = commandArgs[0] === "knowledge-search" ? 150_000 : 15_000;
  const result = await pi.exec(
    process.env.SCHOLARFLOW_PYTHON?.trim() || "python",
    [...hostArgs, ...commandArgs],
    { signal, timeout: timeoutMs },
  );
  const stdout = result.stdout.trim();
  let payload: BridgePayload | undefined;
  try { payload = JSON.parse(stdout) as BridgePayload; } catch { /* handled below */ }
  if (result.code !== 0 || !payload?.ok) {
    const message = payload?.error?.message || result.stderr.trim() || stdout || `bridge exited with code ${result.code}`;
    throw new Error(`ScholarFlow bridge failed: ${message}`);
  }
  return payload;
}

export function toolResult(payload: BridgePayload) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(payload.data, null, 2) }],
    details: payload,
  };
}

export function artifactProposalToolResult(payload: BridgePayload) {
  const result = toolResult(payload);
  const data = payload.data && typeof payload.data === "object"
    ? payload.data as Record<string, unknown>
    : {};
  const contract = [
    `kind=${String(data.kind || "the proposal kind")}`,
    `status=${String(data.status || "draft")}`,
  ];
  if (data.project_stage_id) contract.push(`project_stage_id=${String(data.project_stage_id)}`);
  const refs = Array.isArray(data.evidence_refs) ? data.evidence_refs : [];
  if (refs.length) contract.push(`evidence_ref=${String(refs[0])}`);
  result.content.push({
    type: "text" as const,
    text: `FINAL ANSWER REQUIRED: include these exact tokens in the next assistant message: ${contract.join(", ")}. Do not omit project_stage_id when present, and do not imply that the proposal was applied or call an apply tool.`,
  });
  return result;
}

export async function confirmAndApply(
  pi: ScholarFlowPiAPI,
  proposalId: string,
  getCommand: string,
  applyCommand: string,
  toolCallId: string,
  signal: AbortSignal | undefined,
  confirm: (title: string, message: string) => Promise<boolean>,
) {
  const preview = await callBridge(pi, [getCommand, "--proposal-id", proposalId], signal, toolCallId);
  const proposal = preview.data as { operation?: string; target?: Record<string, unknown>; diff?: unknown };
  const accepted = await confirm(
    `Apply ${proposal.operation ?? "ScholarFlow mutation"}?`,
    `${JSON.stringify(proposal.target ?? {}, null, 2)}\n\n${JSON.stringify(proposal.diff ?? {}, null, 2)}`,
  );
  if (!accepted) {
    return {
      content: [{ type: "text" as const, text: "The proposal was not applied because the user declined confirmation." }],
      details: { proposal_id: proposalId, applied: false, reason: "user_declined" },
      terminate: true,
    };
  }
  return toolResult(await callBridge(
    pi,
    [applyCommand, "--proposal-id", proposalId, "--confirmed", "--confirmed-by", "interactive_user", "--confirmed-at", new Date().toISOString()],
    signal,
    toolCallId,
  ));
}
