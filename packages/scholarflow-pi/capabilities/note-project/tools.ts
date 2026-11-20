import {Type} from "typebox";
import type {ExtensionAPI} from "@earendil-works/pi-coding-agent";
import {callProjection} from "./transport.ts";
const scope={stage_id:Type.String({minLength:1}),artifact_id:Type.String({minLength:1}),target_id:Type.String({minLength:1})};
const version=Type.String({pattern:"^sha256:[a-f0-9]{64}$"});
export function noteProject(pi: ExtensionAPI) {
  const tool=(name:string,operation:string,description:string,fields:any)=>({name,label:name,description,
    parameters:Type.Object({...scope,...fields},{additionalProperties:false}),
    execute:(id:string,params:any,signal?:AbortSignal,_update?:any,ctx?:any)=>callProjection(pi,operation,params,id,signal,ctx)});
  return {id:"note-project",sideEffects:"Frozen export proposals only; file writes require separate operator CLI",
    scopePolicy:"Exact host-owned note/project/target; local export or explicitly reviewed multi-file Obsidian policy; no AI indexing",tools:[
      tool("sf_note_project_context","note.project.context","Read granted source note, source/target-plan hashes and full frozen preview. Obsidian mode lists every note/log/project/journal file, before/after bytes, directories and pinned policy. Not PDF reading or AI indexing.",{mode:Type.Literal("update")}),
      tool("sf_note_propose_projection","note.project.propose","Propose frozen bytes for the exact host-granted local or Obsidian target using unchanged context source/target-plan hashes. No external write; return full ID/hash and all files for separate operator review. No model paths, text or approval.",{mode:Type.Literal("update"),expected_fingerprint:version,expected_target_hash:version,idempotency_key:Type.String({minLength:1,maxLength:160})}),
      tool("sf_note_projection_status","note.project.status","Read historical projection receipt, per-file completed/intent/not_started progress and current source/target/policy match flags. Partial is not committed; expected bytes are only recovery observation, not atomicity. Never implies AI indexing.",{proposal_id:Type.String({minLength:1})})]};
}
