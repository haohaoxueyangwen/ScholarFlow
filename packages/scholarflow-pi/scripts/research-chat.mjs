import {spawnSync} from 'node:child_process';
import {buildResearchRuntime} from '../harness/research-runtime.mjs';
const args=process.argv.slice(2),workspace=args[args.indexOf('--workspace')+1];
if(!args.includes('--workspace')||!workspace)throw new Error('--workspace is required');
try{
 const r=buildResearchRuntime(workspace,{credentials:!args.includes('--status')});
 if(args.includes('--status'))console.log(JSON.stringify({provider:r.manifest.provider,model:r.model,stage_id:r.manifest.stage_id,capabilities:r.composition.enabled.map(x=>x.id),pdf:r.manifest.pdf?.document_id??null,knowledge_configured:r.manifest.knowledge_configured,native_sessions:r.sessions,model_messages_sent:0}));
 else{
  console.log(`Pi research: ${r.manifest.project_title} | ${r.manifest.provider}/${r.model}`);
  console.log('Exit Pi to return to the host menu and review note proposals. No automatic indexing or external publication.');
  const child=spawnSync(process.execPath,[r.cliPath,...r.args,'--session-dir',r.sessions,'--continue'],{cwd:r.cwd,env:r.env,stdio:'inherit'});
  if(child.error)throw new Error('Native Pi process could not start');process.exitCode=child.status??1;
 }
}catch(error){console.error(error.message);process.exitCode=1;}
process.exit(process.exitCode||0);
