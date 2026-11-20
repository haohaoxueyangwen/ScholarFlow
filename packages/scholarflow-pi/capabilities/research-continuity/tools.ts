import {Type} from 'typebox';
import {readFileSync} from 'node:fs';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
import {validateContinuityGrant} from './grant.mjs';
import {callReadOperation} from '../../harness/operation-client.ts';
const directory=dirname(fileURLToPath(import.meta.url)),kind='scholarflow.research-checkpoint.v1';
export function researchContinuity(pi:any){
 const file=process.env.SCHOLARFLOW_CONTINUITY_GRANT_FILE;
 const current=()=>{if(!file)throw new Error('Host continuity grant required');const g=JSON.parse(readFileSync(file,'utf8').replace(/^\uFEFF/,''));if(!validateContinuityGrant(g))throw new Error('Invalid current continuity grant');return g;};
 const initial=current();
 const observe=(id:string,params:any,signal:any,ctx:any)=>callReadOperation(pi,'continuity.observe',params,{kind:'project_stage',stage_id:params.stage_id},null,id,signal,30000,ctx,{adapter:resolve(directory,'adapter.py'),grantFile:file});
 const answer=(value:any,authority:any)=>({content:[{type:'text',text:JSON.stringify(value,null,2)}],details:{...value,authority_read_receipt:authority}});
 return {id:'research-continuity',sideEffects:'Only explicit checkpoints append non-authoritative Pi-native custom entries. No domain facts, approvals, note files or index writes.',scopePolicy:`Research project ${initial.stage_id}; workflow ${initial.workflow_id}; host objective ${initial.objective}. Checkpoint is handoff, never source evidence or approval. Current grants are revalidated on every observation.`,tools:[
  {name:'sf_research_checkpoint',label:'Save Pi research handoff',description:'Save non-authoritative summary and next actions in the current Pi session only, after current project/source/grant checks. Does not publish a research fact, note or approval.',parameters:Type.Object({stage_id:Type.String(),summary:Type.String({minLength:1,maxLength:8000}),next_actions:Type.Array(Type.String({minLength:1,maxLength:500}),{maxItems:6}),source_ids:Type.Array(Type.String({pattern:'^source-[a-f0-9]{24}$'}),{minItems:1,maxItems:20,uniqueItems:true})},{additionalProperties:false}),
   async execute(id:string,p:any,signal:any,_u:any,ctx:any){
    const g=current();if(p.stage_id!==g.stage_id)throw new Error('Checkpoint project differs from current grant');
    const sessionFile=ctx?.sessionManager?.getSessionFile?.();if(!sessionFile)throw new Error('Persistent native Pi session required; --no-session cannot create durable checkpoints');
    const result=await observe(id,{stage_id:p.stage_id,source_ids:p.source_ids,purpose:'checkpoint'},signal,ctx);const a=result.details.data;
    if(signal?.aborted||a.workflow_id!==g.workflow_id||a.principal!==g.principal||a.objective!==g.objective||JSON.stringify(current())!==JSON.stringify(g)||!p.summary.trim())throw new Error('Checkpoint cancelled or host workflow changed');
    const entry={version:1,checkpoint_id:'checkpoint-'+randomUUID(),project_id:g.stage_id,workflow_id:g.workflow_id,principal:g.principal,objective:a.objective,summary:p.summary,next_actions:p.next_actions,source_ids:p.source_ids,authority_snapshot:a.authority_snapshot,native_session_id:ctx.sessionManager.getSessionId(),created_at:new Date().toISOString(),authoritative:false,requires_fact_verification:true};
    pi.appendEntry(kind,entry);return answer({status:'checkpoint_saved',checkpoint_id:entry.checkpoint_id,project_id:entry.project_id,workflow_id:entry.workflow_id,authoritative:false,control_metadata_persisted:true,research_facts_written:false},result.details);
   }},
  {name:'sf_research_resume',label:'Revalidate Pi research handoff',description:'Read the latest matching checkpoint on the current Pi branch, then revalidate the current project, source hashes, active project bindings and grants. Stale checkpoint summary is withheld. ok means call succeeded; only data.status=fresh permits evidence-based continuation.',parameters:Type.Object({stage_id:Type.String()},{additionalProperties:false}),
   async execute(id:string,p:any,signal:any,_u:any,ctx:any){
    const g=current();if(p.stage_id!==g.stage_id)throw new Error('Resume project differs from current grant');
    const entry=[...ctx.sessionManager.getBranch()].reverse().find((x:any)=>x.type==='custom'&&x.customType===kind&&x.data?.version===1&&x.data.project_id===g.stage_id&&x.data.workflow_id===g.workflow_id&&x.data.principal===g.principal);
    if(!entry)return answer({status:'no_matching_checkpoint',project_id:g.stage_id,workflow_id:g.workflow_id,research_facts_written:false},null);
    const saved=entry.data;const result=await observe(id,{stage_id:p.stage_id,source_ids:saved.source_ids,purpose:'resume',expected:saved.authority_snapshot},signal,ctx);const a=result.details.data;
    return answer({...a,checkpoint_id:saved.checkpoint_id,authoritative:false,handoff:a.status==='fresh'?{summary:saved.summary,next_actions:saved.next_actions,source_ids:saved.source_ids}:null},result.details);
   }}
 ]};
}
