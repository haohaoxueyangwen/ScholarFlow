import {resolve} from "node:path";
import type {ExtensionAPI} from "@earendil-works/pi-coding-agent";
import {ReadOperationFailure} from "./operation-client.ts";
// Transport only. The domain facade revalidates fields, grants, scope and receipt.
export async function callGovernedWrite(pi: ExtensionAPI, config: {root: string; adapter: string; grantEnv: string},
    request: any, signal?: AbortSignal) {
  const failure = (code: string, execution: string) => new ReadOperationFailure({...request, ok: false, status: code, source_versions: [],
    error: {code, message: "Capability-write transport failed; inspect proposal/durable receipt", retryable: false},
    receipt: {execution, domain_write: execution === "not_started" ? "not_attempted" : "unknown"}});
  if (signal?.aborted) throw failure("cancelled", "not_started");
  const grant = process.env[config.grantEnv];
  if (!grant) throw failure("not_configured", "not_started");
  const encoded = JSON.stringify(request);
  if (encoded.length > 16000) throw failure("invalid_argument", "not_started");
  let result;
  try { result = await pi.exec(process.env.SCHOLARFLOW_PYTHON || "python", [config.adapter, "--data-dir",
    resolve(config.root, process.env.SCHOLARFLOW_DATA_DIR || ".tmp/scholarflow-cli"), "--grant-file", resolve(config.root, grant), "--request-json", encoded], {signal, timeout: 15000}); }
  catch { throw failure(signal?.aborted ? "cancelled" : "dependency_unavailable", "unknown"); }
  if (signal?.aborted || result.killed) throw failure(signal?.aborted ? "cancelled" : "timeout", "unknown");
  let response;
  try { response = JSON.parse(result.stdout); } catch { throw failure("protocol_error", "unknown"); }
  if (!response || response.protocol_version !== 1 || response.request_id !== request.request_id || response.task_id !== request.task_id
      || response.operation !== request.operation || JSON.stringify(response.scope) !== JSON.stringify(request.scope)
      || typeof response.ok !== "boolean" || !response.receipt || !Array.isArray(response.source_versions)) throw failure("protocol_error", "unknown");
  if (!response.ok || result.code !== 0) throw new ReadOperationFailure(response);
  return {content: [{type: "text" as const, text: JSON.stringify(response.data, null, 2)}], details: response};
}
