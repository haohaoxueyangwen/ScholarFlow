import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import type {ExtensionAPI} from "@earendil-works/pi-coding-agent";
import {callGovernedWrite} from "../../harness/write-client.ts";
const directory = dirname(fileURLToPath(import.meta.url));
const root = resolve(directory, "../../../..");
export async function callTaskWrite(pi: ExtensionAPI, operation: string, params: any, id: string, signal?: AbortSignal, ctx?: any) {
  const request: any = {protocol_version: 1, request_id: id,
    task_id: process.env.SCHOLARFLOW_TASK_ID || ctx?.sessionManager?.getSessionId?.() || `pi-call:${id}`,
    operation, scope: {kind: "task", stage_id: params.stage_id, task_id: params.task_id}, params: {...params}, timeout_ms: 14750};
  delete request.params.stage_id;
  if (operation === "task.write.propose") {
    request.idempotency_key = params.idempotency_key;
    delete request.params.idempotency_key;
  } else delete request.params.task_id;
  return callGovernedWrite(pi, {root, adapter: resolve(directory, "adapter.py"), grantEnv: "SCHOLARFLOW_TASK_WRITE_GRANT_FILE"}, request, signal);
}
