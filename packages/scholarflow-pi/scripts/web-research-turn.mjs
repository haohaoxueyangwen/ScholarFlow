// One user turn in a Pi-native workspace. No additional agent or model client.
import {readFileSync,writeFileSync,existsSync,realpathSync,statSync} from 'node:fs';
import {dirname,resolve,relative,isAbsolute} from 'node:path';
import {buildResearchRuntime} from '../harness/research-runtime.mjs';
import {RpcClient} from '../node_modules/@earendil-works/pi-coding-agent/dist/modes/rpc/rpc-client.js';

const index=process.argv.indexOf('--workspace');
if(index<0||!process.argv[index+1])throw new Error('Workspace required');
const input=JSON.parse(readFileSync(0,'utf8'));
if(typeof input.prompt!=='string'||!input.prompt.trim()||input.prompt.length>10000)throw new Error('Bounded prompt required');
const r=buildResearchRuntime(process.argv[index+1]);
const pointerFile=resolve(dirname(process.argv[index+1]),'web-session.json');
let session=null;
if(existsSync(pointerFile)){
 const p=JSON.parse(readFileSync(pointerFile,'utf8'));session=resolve(p.path);
 const rel=relative(r.sessions,session);
 if(!rel||rel.startsWith('..')||isAbsolute(rel)||realpathSync(session)!==session||statSync(session).nlink!==1)throw new Error('Invalid native session pointer');
}
const client=new RpcClient({cliPath:r.cliPath,cwd:r.cwd,env:r.env,args:[...r.args,...(session?['--session',session]:['--session-dir',r.sessions])]});
let output;
try{
 await client.start();const state=await client.getState();
 if(!state.sessionFile||relative(r.sessions,resolve(state.sessionFile)).startsWith('..'))throw new Error('Pi returned a session outside this workspace');
 if(session&&resolve(state.sessionFile)!==session)throw new Error('Native session changed unexpectedly');
 writeFileSync(pointerFile,JSON.stringify({id:state.sessionId,path:state.sessionFile}));
 const usage={responses:0,tokens:0,model_reported_usd:0};
 const maxTokens=Number(process.env.SCHOLARFLOW_WEB_TEST_MAX_TOKENS||0);
 const maxResponses=Number(process.env.SCHOLARFLOW_WEB_TEST_MAX_RESPONSES||0);
 const maxCost=Number(process.env.SCHOLARFLOW_WEB_TEST_MAX_USD||0);
 let budgetExceeded=false,toolFailure='';
 const off=client.onEvent(e=>{if(e.type==='tool_execution_end'&&e.isError&&maxTokens){
  const detail=(e.result?.content||[]).filter(c=>c.type==='text').map(c=>c.text).join(' ');
  const code=/ScholarFlow read: ([a-z_]+):/.exec(detail)?.[1]||'tool_failed';
  toolFailure=`tool_error:${e.toolName}:${code}`;void client.abort().catch(()=>{});
 }
 if(e.type==='message_end'&&e.message?.role==='assistant'){
  usage.responses++;usage.tokens+=e.message.usage?.totalTokens||0;usage.model_reported_usd+=e.message.usage?.cost?.total||0;
  if((maxTokens&&usage.tokens>=maxTokens)||(maxResponses&&usage.responses>=maxResponses)||(maxCost&&usage.model_reported_usd>=maxCost)){budgetExceeded=true;void client.abort().catch(()=>{});}
 }});
 const events=await client.promptAndWait(input.prompt,undefined,600000);off();
 const messages=events.filter(e=>e.type==='message_end'&&e.message?.role==='assistant').map(e=>e.message);
 const answer=messages.at(-1)?.content?.filter(c=>c.type==='text').map(c=>c.text).join('\n')||'';
 const failed=events.filter(e=>e.type==='tool_execution_end'&&e.isError);
 const sources=[];
 for(const event of events.filter(e=>e.type==='tool_execution_end'&&e.toolName==='sf_library_search'&&!e.isError)){
  for(const item of event.result?.details?.data?.items||[]){
   if(item.stage_id!==r.manifest.stage_id)throw new Error('Knowledge result crossed project scope');
   sources.push(item);
  }
 }
 if(budgetExceeded)throw new Error('Frozen test budget reached');
 if(toolFailure)throw new Error(toolFailure);
 if(!answer||messages.some(m=>['error','aborted','length'].includes(m.stopReason)))throw new Error('Pi did not complete this turn');
 output={status:'completed',answer,sources,tool_errors:failed.map(e=>e.toolName),usage,session_id:state.sessionId};
}finally{await client.stop().catch(()=>{});}
process.stdout.write(JSON.stringify(output));
