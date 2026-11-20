// Native Pi configuration only; no model calls or workflow dispatcher here.
import {readFileSync,writeFileSync,mkdirSync,existsSync,realpathSync,statSync} from 'node:fs';
import {isDeepStrictEqual} from 'node:util';
import {resolve,dirname,basename,delimiter} from 'node:path';
import {fileURLToPath} from 'node:url';
import {homedir} from 'node:os';
import {spawnSync} from 'node:child_process';
import {resolveComposition,runtimeArgs} from './composition.mjs';
import {resolveScholarPython} from './python-runtime.mjs';
export const pkg=resolve(dirname(fileURLToPath(import.meta.url)),'..'),root=resolve(pkg,'../..');
export function buildResearchRuntime(workspacePath,{credentials=true}={}){
 const python=resolveScholarPython();
 const checked=spawnSync(python,['-s','-B',resolve(root,'scripts/research-workspace.py'),'check','--workspace',resolve(workspacePath)],{cwd:root,encoding:'utf8',env:{...process.env,PYTHONUTF8:'1',PYTHONNOUSERSITE:'1'},timeout:30000});
 if(checked.status!==0)throw new Error('Workspace is stale or invalid: '+checked.stderr.trim());
 const m=JSON.parse(checked.stdout),folder=dirname(realpathSync(workspacePath)),grants=resolve(folder,'grants');
 const ids=['research-workflow',...(m.pdf?['pdf-read','pdf-note']:[]),...(m.knowledge_configured?['library-read']:[])];
 const values={SCHOLARFLOW_CAPABILITIES:ids.join(','),SCHOLARFLOW_DATA_DIR:m.data_dir,SCHOLARFLOW_PYTHON:python,SCHOLARFLOW_WORKFLOW_GRANT_FILE:resolve(grants,'workflow.json'),SCHOLARFLOW_PDF_READ_GRANT_FILE:m.pdf?resolve(grants,'pdf-read.json'):'',SCHOLARFLOW_PDF_NOTE_GRANT_FILE:m.pdf?resolve(grants,'pdf-note.json'):'',SCHOLARFLOW_LIBRARY_GRANT_FILE:m.knowledge_configured?resolve(grants,'library-read.json'):'',PYTHONUTF8:'1',PYTHONNOUSERSITE:'1'};
 const prior=Object.fromEntries(Object.keys(values).map(k=>[k,process.env[k]]));Object.assign(process.env,values);let composition;
 try{composition=resolveComposition();}finally{for(const[k,v]of Object.entries(prior)){if(v===undefined)delete process.env[k];else process.env[k]=v;}}
 if(composition.diagnostics.length)throw new Error('Workspace capability unavailable: '+JSON.stringify(composition.diagnostics));
 const agent=resolve(folder,'pi-state'),cwd=resolve(folder,'cwd'),sessions=resolve(folder,'sessions');for(const p of [agent,cwd,sessions]){mkdirSync(p,{recursive:true});if(realpathSync(p)!==p)throw new Error('Aliased workspace runtime directory is not allowed');}
 if(!existsSync(resolve(agent,'settings.json')))writeFileSync(resolve(agent,'settings.json'),JSON.stringify({packages:[],retry:{enabled:false},compaction:{enabled:true}}));
 if(m.provider==='deepseek'){
  const file=resolve(agent,'models.json'),expected={providers:{deepseek:{modelOverrides:{'deepseek-v4-flash':{maxTokens:4096}}}}};
  if(!existsSync(file))writeFileSync(file,JSON.stringify(expected));
  if(realpathSync(file)!==file||statSync(file).nlink!==1||!isDeepStrictEqual(JSON.parse(readFileSync(file,'utf8')),expected))throw new Error('Workspace model configuration changed; host review required before using API credentials');
 }
 const libraryHome=basename(m.data_dir)==='facts'?dirname(m.data_dir):resolve(m.data_dir,'_research_knowledge');
 const env={...process.env,...values,SCHOLARFLOW_LIBRARY_HOME:libraryHome,SCHOLARFLOW_LIBRARY_FACTS_DIR:m.data_dir,PI_CODING_AGENT_DIR:agent,PATH:dirname(process.execPath)+delimiter+process.env.PATH};
 if(m.provider==='deepseek'&&credentials){const auth=JSON.parse(readFileSync(resolve(homedir(),'.pi/agent/auth.json'),'utf8'));const key=auth?.deepseek?.key;if(typeof key!=='string'||!key.trim())throw new Error('Existing DeepSeek API authentication required');env.DEEPSEEK_API_KEY=key;}
 const model=m.provider==='deepseek'?'deepseek-v4-flash':'qwen38-vision';
 return {manifest:m,composition,agent,cwd,sessions,env,cliPath:resolve(pkg,'node_modules/@earendil-works/pi-coding-agent/dist/cli.js'),args:[...runtimeArgs(composition,{includeLocalQwenProvider:m.provider==='scholarflow-local'}),'--provider',m.provider,'--model',model,'--thinking','off'],model};
}
