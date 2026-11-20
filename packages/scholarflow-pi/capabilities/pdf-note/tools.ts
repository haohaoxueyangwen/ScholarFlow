import {Type} from "typebox";
import {dirname,resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {noteWrite} from "../note-write/tools.ts";
import {callGovernedWrite} from "../../harness/write-client.ts";
const directory=dirname(fileURLToPath(import.meta.url)),root=resolve(directory,"../../../..");
export function pdfNote(pi:any){
  const actions=["context","propose","status"],names=["sf_pdf_note_context","sf_pdf_note_propose","sf_pdf_note_status"];
  const original=noteWrite(pi);
  return {id:"pdf-note",sideEffects:"proposal/control metadata only; exact-hash commit is trusted operator CLI only",scopePolicy:"Exact PDF note/project/host-approved fragments. All source excerpts and references are service-owned; derived notes are not original papers or AI indexes.",tools:original.tools.map((tool:any,index:number)=>{
    const parameters={...tool.parameters,properties:{...tool.parameters.properties}};
    if(index===1){parameters.properties.source_versions=Type.Record(Type.String({pattern:"^pdf-source-[a-f0-9]{24}$"}),Type.String({pattern:"^sha256:[a-f0-9]{64}$"}),{additionalProperties:false});parameters.properties.quotes=Type.Optional(Type.Record(Type.String({pattern:"^pdf-source-[a-f0-9]{24}$"}),Type.String({minLength:1}),{additionalProperties:false,description:"Omit for summaries/translations. Only map a context source_id to its entire unchanged original excerpt; sentence keys and partial quotations are invalid."}));}
    return {...tool,name:names[index],label:"PDF source note "+actions[index],description:index===0?"Read exact granted PDF note context with source_id/fingerprint, verified original excerpt and page/block/character anchors. Explicit create/update required.":index===1?"Propose a derived PDF source note, not a commit. Copy ALL source_id/fingerprint pairs from context into source_versions and exact expected_fingerprint; optional quotes must exactly match corresponding original fragments. Model cannot set source_snapshot or approve.":"Read the full opaque proposal ID and durable note receipt. Saved note does not imply Obsidian projection or AI indexing.",parameters,
      execute:async(id:string,params:any,signal?:AbortSignal,_update?:any,ctx?:any)=>{
        const request:any={protocol_version:1,request_id:id,task_id:process.env.SCHOLARFLOW_TASK_ID||ctx?.sessionManager?.getSessionId?.()||`pi-call:${id}`,operation:"pdf.note."+actions[index],scope:{kind:"note",stage_id:params.stage_id,artifact_id:params.artifact_id},params:{...params},timeout_ms:14750};delete request.params.stage_id;
        if(index===1){request.idempotency_key=params.idempotency_key;delete request.params.idempotency_key;}else delete request.params.artifact_id;
        return callGovernedWrite(pi,{root,adapter:resolve(directory,"adapter.py"),grantEnv:"SCHOLARFLOW_PDF_NOTE_GRANT_FILE"},request,signal);
      }};
  })};
}
