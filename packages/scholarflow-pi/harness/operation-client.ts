import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import type {ExtensionAPI} from "@earendil-works/pi-coding-agent";

const directory = dirname(fileURLToPath(import.meta.url));
const root = resolve(directory, "../../..");
export class ReadOperationFailure extends Error {
  constructor(public response: any) {
    super(`ScholarFlow read: ${response.error?.code}: ${response.error?.message}; request_id=${response.request_id}; execution=${response.receipt?.execution}`);
  }
}

export function localGrant(capability: string, stages: string[] = []) {
  return {principal: "pi-local", capabilities: [capability], project_catalog: capability === "project-evidence",
    paper_catalog: capability === "paper-read" && process.env.SCHOLARFLOW_PAPER_SCOPE === "local-catalog-read", stage_ids: stages};
}

export async function callReadOperation(pi: ExtensionAPI, operation: string, params: unknown, scope: unknown,
  grant: unknown, id: string, signal?: AbortSignal, timeout = 15000, ctx?: any,
  trustedTransport?: {adapter: string; grantFile: string}) {
  const taskId = process.env.SCHOLARFLOW_TASK_ID || ctx?.sessionManager?.getSessionId?.() || `pi-call:${id}`;
  const request = {protocol_version: 1, request_id: id, task_id: taskId, operation, scope, params,
    timeout_ms: Math.max(1, Math.min(30000, timeout - 250))};
  const failure = (code: string, message: string, execution: string) => new ReadOperationFailure({
    ...request, ok: false, status: ["cancelled", "timeout"].includes(code) ? code : "error", source_versions: [],
    error: {code, message, retryable: code === "timeout"},
    receipt: {request_id: id, task_id: taskId, operation, read_only: true, execution, audit: {persisted: false}}});
  if (signal?.aborted) throw failure("cancelled", "Cancelled before process launch", "not_started");
  const encoded = JSON.stringify(request), authorized = JSON.stringify(grant);
  if (encoded.length + authorized.length > 16000) throw failure("invalid_argument", "CLI request/grant exceeds bounded argument size", "not_started");
  const args = [trustedTransport?.adapter || resolve(directory, "operation_runner.py"), "--data-dir", resolve(root, process.env.SCHOLARFLOW_DATA_DIR || ".tmp/scholarflow-cli"),
    ...(trustedTransport ? ["--grant-file", resolve(root, trustedTransport.grantFile)] : ["--grant-json", authorized]), "--request-json", encoded];
  if (process.env.SCHOLARFLOW_READ_AUDIT_PATH) args.push("--audit-path", resolve(root, process.env.SCHOLARFLOW_READ_AUDIT_PATH));
  let result;
  try { result = await pi.exec(process.env.SCHOLARFLOW_PYTHON || "python", args, {signal, timeout}); }
  catch { throw failure(signal?.aborted ? "cancelled" : "dependency_unavailable", "Local process transport failed; execution completion cannot be confirmed", "unknown"); }
  if (signal?.aborted) throw failure("cancelled", "Transport stopped; query/audit completion is unknown, inspect receipt by request ID if configured", "unknown");
  if (result.killed) throw failure("timeout", "Transport deadline elapsed; query/audit completion is unknown", "unknown");
  let payload;
  try { payload = JSON.parse(result.stdout); } catch { throw failure("dependency_unavailable", "No valid response from the local read process", "unknown"); }
  if (!payload || typeof payload !== "object" || payload.protocol_version !== 1 || payload.request_id !== id || payload.task_id !== taskId || payload.operation !== operation || !payload.receipt
      || typeof payload.ok !== "boolean" || !Array.isArray(payload.source_versions)
      || payload.receipt.request_id !== id || payload.receipt.task_id !== taskId || payload.receipt.operation !== operation
      || payload.receipt.read_only !== true || typeof payload.receipt.audit?.persisted !== "boolean"
      || JSON.stringify(payload.scope) !== JSON.stringify(scope)
      || (payload.ok ? payload.status !== "ok" || !("data" in payload) : !payload.error?.code)) {
    throw failure("protocol_error", "Read response identity/envelope mismatch", "unknown");
  }
  if (result.code !== 0 || !payload.ok) throw new ReadOperationFailure(payload);
  return {content: [{type: "text" as const, text: JSON.stringify(payload.data, null, 2)}], details: payload};
}
