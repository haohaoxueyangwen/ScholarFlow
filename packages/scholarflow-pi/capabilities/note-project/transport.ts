import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import type {ExtensionAPI} from "@earendil-works/pi-coding-agent";
import {callGovernedWrite} from "../../harness/write-client.ts";
const directory = dirname(fileURLToPath(import.meta.url));
export function callProjection(pi: ExtensionAPI, operation: string, params: any, id: string, signal?: AbortSignal, ctx?: any) {
  const {stage_id, artifact_id, target_id, idempotency_key, ...fields} = params;
  const request: any = {protocol_version:1,request_id:id,task_id:process.env.SCHOLARFLOW_TASK_ID||ctx?.sessionManager?.getSessionId?.()||`pi-call:${id}`,
    operation,scope:{kind:"note_projection",stage_id,artifact_id,target_id},params:fields,timeout_ms:14750};
  if(operation === "note.project.propose") request.idempotency_key=idempotency_key;
  return callGovernedWrite(pi,{root:resolve(directory,"../../../.."),adapter:resolve(directory,"adapter.py"),grantEnv:"SCHOLARFLOW_NOTE_PROJECT_GRANT_FILE"},request,signal);
}
