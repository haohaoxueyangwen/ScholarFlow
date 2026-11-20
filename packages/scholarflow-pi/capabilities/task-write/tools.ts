import {Type} from "typebox";
import type {ExtensionAPI} from "@earendil-works/pi-coding-agent";
import {callTaskWrite} from "./transport.ts";
const target = {stage_id: Type.String({minLength: 1}), task_id: Type.String({minLength: 1})};
const mode = Type.Union([Type.Literal("create"), Type.Literal("update")]);
const nullable = (maximum: number) => Type.Union([Type.String({minLength: 1, maxLength: maximum}), Type.Null()]);
export function taskWrite(pi: ExtensionAPI) {
  return {id: "task-write", sideEffects: "proposal/control metadata only; commits require separate trusted operator CLI",
    scopePolicy: "Exact host-granted task ID and stage ID per action. No reassignment, model approval or apply.", tools: [
      {name: "sf_task_write_context", label: "Read task write context", description: "Read the exact authorized task in its project, or its absence, and current expected_fingerprint. No domain writes. stage_id and task_id are both required; create/update is explicit.",
        parameters: Type.Object({...target, mode}, {additionalProperties: false}),
        execute: (id: string, params: any, signal?: AbortSignal, _update?: any, ctx?: any) => callTaskWrite(pi, "task.write.context", params, id, signal, ctx)},
      {name: "sf_task_propose_write", label: "Propose task changes", description: "Persist a pending task proposal, NOT a saved task. Read write_context first; copy its fingerprint. Create requires title; project association is injected by the service. Only bounded task fields; retain the opaque full proposal ID/hash for operator review.",
        parameters: Type.Object({...target, mode, changes: Type.Object({title: Type.Optional(Type.String({minLength: 1, maxLength: 1000})),
          description: Type.Optional(nullable(8000)), status: Type.Optional(Type.Union([Type.Literal("pending"), Type.Literal("in_progress"), Type.Literal("blocked"), Type.Literal("done"), Type.Literal("cancelled")])),
          priority: Type.Optional(nullable(1000)), due_hint: Type.Optional(nullable(1000))}, {additionalProperties: false}),
          expected_fingerprint: Type.String({minLength: 1}), idempotency_key: Type.String({minLength: 1, maxLength: 160})}, {additionalProperties: false}),
        execute: (id: string, params: any, signal?: AbortSignal, _update?: any, ctx?: any) => callTaskWrite(pi, "task.write.propose", params, id, signal, ctx)},
      {name: "sf_task_write_status", label: "Read task proposal status", description: "Read this principal's exact proposal and durable receipt in its exact project/task. Does not write task facts. A historical committed receipt may differ from current write_context; neither proves complete edit history. Copy the FULL proposal ID unchanged, including sha256:.",
        parameters: Type.Object({...target, proposal_id: Type.String({minLength: 1})}, {additionalProperties: false}),
        execute: (id: string, params: any, signal?: AbortSignal, _update?: any, ctx?: any) => callTaskWrite(pi, "task.write.status", params, id, signal, ctx)},
    ]};
}
