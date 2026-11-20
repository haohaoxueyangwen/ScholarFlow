import {readFileSync} from 'node:fs';
import {validateWorkflow} from './grant.mjs';
export function researchWorkflow(_pi:any){
 const g=JSON.parse(readFileSync(process.env.SCHOLARFLOW_WORKFLOW_GRANT_FILE!,'utf8'));
 if(!validateWorkflow(g))throw new Error('Explicit host-selected workflow required');
 return {id:'research-workflow',tools:[],scopePolicy:`Host-selected project=${g.stage_id}; PDF document=${g.document_id}; create-note artifact=${g.artifact_id}; existing project knowledge configured=${g.knowledge_configured}. Read research-workflow Skill for the assembled workflow. Switching projects/materials requires returning to the host menu, never inventing authority. New PDF/notes are NOT automatically in the AI index.`};
}
