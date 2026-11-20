import {Type} from "typebox";
import type {ExtensionAPI} from "@earendil-works/pi-coding-agent";
import {callProjectWrite} from "./transport.ts";
export function projectWrite(pi: ExtensionAPI) {
  return {id: "project-write", sideEffects: "proposal/control metadata only; commits require separate trusted operator CLI", scopePolicy: "Exact host-granted create/update stage IDs. No model approval or apply tool.", tools: [
    {name: "sf_project_write_context", label: "Read write context", description: "Read the exact host-granted target's record or absence and expected_fingerprint. Create context also returns creation_basis and any exact authorized_goal; no project writes.",
      parameters: Type.Object({stage_id: Type.String({minLength: 1}), mode: Type.Union([Type.Literal("create"), Type.Literal("update")])}, {additionalProperties: false}),
      execute: (id: string, params: any, signal?: AbortSignal, _update?: any, ctx?: any) => callProjectWrite(pi, "project.write.context", params, id, signal, ctx)},
    {name: "sf_project_propose_write", label: "Propose project changes", description: "Persist a project proposal, not a project commit. Use the current object fingerprint (or absence fingerprint for create); retain the proposal ID/hash for human operator review.",
      parameters: Type.Object({stage_id: Type.String({minLength: 1}), mode: Type.Union([Type.Literal("create"), Type.Literal("update")]),
        changes: Type.Record(Type.String(), Type.Unknown()), expected_fingerprint: Type.String({minLength: 1}), idempotency_key: Type.String({minLength: 1, maxLength: 160})}, {additionalProperties: false}),
      execute: (id: string, params: any, signal?: AbortSignal, _update?: any, ctx?: any) => callProjectWrite(pi, "project.write.propose", params, id, signal, ctx)},
    {name: "sf_project_write_status", label: "Read project proposal status", description: "Read this principal's proposal, review hash and actual durable commit status. Pending is not saved project progress; a committed receipt is historical and may not be current project state.",
      parameters: Type.Object({stage_id: Type.String({minLength: 1}), proposal_id: Type.String({minLength: 1})}, {additionalProperties: false}),
      execute: (id: string, params: any, signal?: AbortSignal, _update?: any, ctx?: any) => callProjectWrite(pi, "project.write.status", params, id, signal, ctx)},
  ]};
}
