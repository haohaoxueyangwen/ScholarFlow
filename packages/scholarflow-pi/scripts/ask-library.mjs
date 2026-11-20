// One user task in native Pi; citation rendering is deterministic, not another LLM.
import {readFileSync,writeFileSync,appendFileSync,mkdirSync,existsSync} from 'node:fs';
import {resolve,dirname,delimiter} from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {RpcClient} from '../node_modules/@earendil-works/pi-coding-agent/dist/modes/rpc/rpc-client.js';
import {resolveComposition,runtimeArgs} from '../harness/composition.mjs';
import {resolveScholarPython} from '../harness/python-runtime.mjs';
import {parseLibraryRequest} from '../harness/library-request.mjs';
const pkg=resolve(dirname(fileURLToPath(import.meta.url)),'..'),root=resolve(pkg,'../..'),home=resolve(root,'.local/research-library');
const args=process.argv.slice(2),value=(key,fallback)=>args.includes(key)?args[args.indexOf(key)+1]:fallback;
const {qualification,stage,question}=parseLibraryRequest(args);
const python=resolveScholarPython();
const run=qualification?resolve(home,'r2'):resolve(home,'answers',randomUUID());
if(qualification&&existsSync(resolve(run,'budget.json')))throw new Error('R2 budget already consumed; no retry');
mkdirSync(run,{recursive:true});
Object.assign(process.env,{SCHOLARFLOW_CAPABILITIES:'library-read',SCHOLARFLOW_LIBRARY_GRANT_FILE:value('--grant',resolve(home,'pilot-v2/library-grant.json')),SCHOLARFLOW_DATA_DIR:resolve(home,'facts'),SCHOLARFLOW_PYTHON:python,PYTHONUTF8:'1',PYTHONNOUSERSITE:'1'});
const composition=resolveComposition();if(composition.diagnostics.length)throw new Error(JSON.stringify(composition.diagnostics));
const agent=resolve(run,'pi-state');mkdirSync(agent);writeFileSync(resolve(agent,'settings.json'),JSON.stringify({packages:[],retry:{enabled:false},compaction:{enabled:true}}));
const client=new RpcClient({cliPath:resolve(pkg,'node_modules/@earendil-works/pi-coding-agent/dist/cli.js'),cwd:resolve(pkg,'profiles/minimal'),env:{...process.env,PATH:dirname(process.execPath)+delimiter+process.env.PATH,PI_CODING_AGENT_DIR:agent},args:[...runtimeArgs(composition),'--provider','scholarflow-local','--model','qwen38-vision','--thinking','off','--no-session']});
const events=[],budget={prompts:1,responses:0,tokens:0,limits:{prompts:1,responses:8,tokens:50000,timeoutMs:600000},status:'in_progress',model:'scholarflow-local/qwen38-vision',question,stage_id:stage};
const save=()=>writeFileSync(resolve(run,'budget.json'),JSON.stringify(budget,null,2));save();
try{
 await client.start();const state=await client.getState();if(state.messageCount!==0||state.model?.id!=='qwen38-vision'||state.model?.provider!=='scholarflow-local')throw new Error('Native model mismatch');
 const off=client.onEvent(e=>{events.push(e);appendFileSync(resolve(run,'events.jsonl'),JSON.stringify(e)+'\n');if(e.type==='tool_execution_end'&&e.isError)void client.abort().catch(()=>{});if(e.type==='message_end'&&e.message?.role==='assistant'){budget.responses++;budget.tokens+=e.message.usage?.totalTokens||0;save();if(budget.responses>=8||budget.tokens>=50000)void client.abort().catch(()=>{});}});
 const prompt=`先read已启用的library-read Skill，再在stage_id=${stage}内检索证据，最多两次sf_library_search，每次limit=2。问题：${question}\n只返回JSON：{"claims":[{"text":"中文忠实归纳","citations":["返回的citation_id"]}]}。最多4条，每条最多100中文字；同一句若用两个片段，两个短citation_id都列。若返回片段不支持问题，返回{"claims":[],"abstention":"本次片段不足的具体原因"}，不要编造事实或数值。不要复制长reference_id或anchor，服务会从短句柄还原完整引用。只归纳返回片段，不加外部机制，不输出英文引文、作者归属、生成模型声明（这些由服务元数据渲染），不得声称阅读全文。不要保存或提案。工具失败则停止。`;
 writeFileSync(resolve(run,'prompt.txt'),prompt);await client.promptAndWait(prompt,undefined,600000);off();
 const messages=events.filter(e=>e.type==='message_end'&&e.message?.role==='assistant').map(e=>e.message);
 const ends=events.filter(e=>e.type==='tool_execution_end');if(messages.some(m=>['error','aborted','length'].includes(m.stopReason))||ends.some(e=>e.isError))throw new Error('Native trajectory failed');
 const searches=ends.filter(e=>e.toolName==='sf_library_search');if(searches.length<1||searches.length>2||!ends.some(e=>e.toolName==='read'))throw new Error('Read the enabled Skill and perform one or two bounded searches');
 const answer=messages.at(-1)?.content.filter(x=>x.type==='text').map(x=>x.text).join('\n').trim();
 const parsed=JSON.parse(answer.replace(/^```json\s*/,'').replace(/\s*```$/,''));const items=ends.filter(e=>e.toolName==='sf_library_search').flatMap(e=>e.result.details.data.items);
 const rendered=spawnSync(python,['-s','-B',resolve(root,'scripts/render_library_answer.py')],{cwd:root,encoding:'utf8',input:JSON.stringify({stage_id:stage,answer:parsed,items}),env:{...process.env,PYTHONUTF8:'1',PYTHONNOUSERSITE:'1'},timeout:30000});
 if(rendered.status!==0)throw new Error('Evidence rendering failed: '+rendered.stderr);
 const result=JSON.parse(rendered.stdout);writeFileSync(resolve(run,'answer.json'),JSON.stringify({model_answer:parsed,evidence:items,rendered:result},null,2));writeFileSync(resolve(run,'answer.md'),result.markdown+'\n');
 budget.status='mechanical_pass_pending_semantic_review';budget.output=resolve(run,'answer.md');
}catch(e){budget.status='failed';budget.error=String(e);process.exitCode=1;}
finally{await client.stop().catch(()=>{});save();}
console.log(JSON.stringify({status:budget.status,error:budget.error,prompts:budget.prompts,responses:budget.responses,tokens:budget.tokens,run,output:budget.output}));
