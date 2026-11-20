import {Type} from "typebox";
import type {ExtensionAPI} from "@earendil-works/pi-coding-agent";
import {callNoteWrite} from "./transport.ts";
const target = {stage_id: Type.String({minLength: 1}), artifact_id: Type.String({minLength: 1})};
const mode = Type.Union([Type.Literal("create"), Type.Literal("update")]);
export function noteWrite(pi: ExtensionAPI) {
  return {id: "note-write", sideEffects: "proposal/control metadata only; human note commits need separate trusted operator CLI",
    scopePolicy: "Exact granted artifact ID, stage and stored-summary paper IDs; no full PDF, model approval, reassignment or vault access.", tools: [
      {name: "sf_note_write_context", label: "Read note and sources", description: "Read the exact granted synthesis note or its absence, base fingerprint, and granted stored-summary source versions. This is NOT PDF full text. Explicit create/update and exact IDs required. No note fact writes.",
        parameters: Type.Object({...target, mode}, {additionalProperties: false}),
        execute: (id: string, params: any, signal?: AbortSignal, _update?: any, ctx?: any) => callNoteWrite(pi, "note.write.context", params, id, signal, ctx)},
      {name: "sf_note_propose_write", label: "Propose source note", description: "Persist a pending human-note proposal, NOT a saved note or AI knowledge index. Read write_context first. Copy base fingerprint and selected source record fingerprints into source_versions. Create requires title/content. Only title/content/status; source snapshot/references/project/kind are service-owned. Return full opaque proposal ID/hash for operator review.",
        parameters: Type.Object({...target, mode, changes: Type.Object({title: Type.Optional(Type.String({minLength: 1, maxLength: 1000})),
          content: Type.Optional(Type.String({minLength: 1, maxLength: 12000})),
          status: Type.Optional(Type.Union([Type.Literal("draft"), Type.Literal("reviewed"), Type.Literal("final"), Type.Literal("archived")]))}, {additionalProperties: false}),
          source_versions: Type.Record(Type.String({pattern: "^paper-.+"}), Type.String({pattern: "^sha256:[0-9a-f]{64}$"})),
          expected_fingerprint: Type.String({minLength: 1}), idempotency_key: Type.String({minLength: 1, maxLength: 160})}, {additionalProperties: false}),
        execute: (id: string, params: any, signal?: AbortSignal, _update?: any, ctx?: any) => callNoteWrite(pi, "note.write.propose", params, id, signal, ctx)},
      {name: "sf_note_write_status", label: "Read note proposal receipt", description: "Read exact principal/project/note proposal, historical durable receipt and current source-version comparison. Committed does not mean the current note or sources are unchanged, nor that a vault or AI index was updated. Copy the FULL proposal ID including sha256: unchanged.",
        parameters: Type.Object({...target, proposal_id: Type.String({minLength: 1})}, {additionalProperties: false}),
        execute: (id: string, params: any, signal?: AbortSignal, _update?: any, ctx?: any) => callNoteWrite(pi, "note.write.status", params, id, signal, ctx)},
    ]};
}
