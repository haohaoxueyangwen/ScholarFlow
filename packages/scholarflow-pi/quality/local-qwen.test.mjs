import assert from "node:assert/strict";
import test from "node:test";
import {createServer} from "node:http";
import {mkdirSync,readFileSync} from "node:fs";
import {resolve,dirname} from "node:path";
import {fileURLToPath} from "node:url";
import {spawnSync} from "node:child_process";
import {randomUUID} from "node:crypto";
import {loadExtensions} from "../node_modules/@earendil-works/pi-coding-agent/dist/core/extensions/loader.js";
import {streamSimple} from "../node_modules/@earendil-works/pi-coding-agent/node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js";
import {PROVIDER_ID,MODEL_ID,TIMEOUT_MS,localBaseUrl,modelConfig,boundedStream,toolsEnabled,TOOL_CALLING_QUALIFIED} from "../providers/local-qwen/config.mjs";
const pkg=resolve(dirname(fileURLToPath(import.meta.url)),".."),root=resolve(pkg,"../..");
const extension=resolve(pkg,"providers/local-qwen/extension.ts");
const context={systemPrompt:"Synthetic protocol test only",messages:[{role:"user",content:"Reply READY",timestamp:0}]};

test("qualified optional provider has explicit tool opt-out and no silent invalid override",()=>{
  assert.equal(TOOL_CALLING_QUALIFIED,true);assert.equal(toolsEnabled({}),true);
  assert.equal(toolsEnabled({SCHOLARFLOW_QWEN_TOOL_CALLING:"0"}),false);
  assert.equal(toolsEnabled({SCHOLARFLOW_QWEN_TOOL_CALLING:"1"}),true);
  assert.throws(()=>toolsEnabled({SCHOLARFLOW_QWEN_TOOL_CALLING:"yes"}),/0 or 1/);
});

test("loopback model metadata and server defaults",()=>{
  assert.equal(localBaseUrl(),"http://127.0.0.1:18080/v1");
  for(const url of ["http://example.org/v1","https://127.0.0.1/v1","http://u:p@localhost/v1","http://localhost/v1?key=secret","http://localhost/other"])
    assert.throws(()=>localBaseUrl(url),/loopback/);
  const config=modelConfig();assert.equal(config.contextWindow,262144);assert.deepEqual(config.input,["text","image"]);
  // Provider choice is configured by each user, not shipped here.
});

test("transport keeps cancellation and timeout, forbids retries, checks final payload",async()=>{
  let captured;const wrapped=boundedStream((m,c,o)=>{captured=o;return "native-stream";});
  const signal=new AbortController().signal;
  assert.equal(wrapped(modelConfig(),context,{signal}),"native-stream");
  assert.equal(captured.signal,signal);assert.equal(captured.timeoutMs,TIMEOUT_MS);assert.equal(captured.maxRetries,0);
  assert.equal(captured.maxTokens,4096);assert.equal(captured.apiKey,"local");
  const image={type:"image_url",image_url:{url:"data:image/png;base64,AA=="}};
  const first={messages:[{role:"user",content:[image]}]};await captured.onPayload(first,modelConfig());
  await assert.rejects(captured.onPayload({messages:[],tools:[{type:"function"}]},modelConfig()),/disabled for this run/);
  wrapped(modelConfig(),context,{onPayload:()=>({messages:[{content:[image,image]}]})});
  await assert.rejects(captured.onPayload(first,modelConfig()),/at most one image/);
});

test("native loader and isolated Pi model catalog register without login or network",async()=>{
  const loaded=await loadExtensions([extension],root);assert.deepEqual(loaded.errors,[]);
  const run=resolve(root,".tmp/h024-tests",randomUUID());mkdirSync(run,{recursive:true});
  const result=spawnSync(process.execPath,[resolve(pkg,"node_modules/@earendil-works/pi-coding-agent/dist/cli.js"),"--offline","--no-extensions","--no-skills","--no-context-files","--no-prompt-templates","--no-themes","-e",extension,"--list-models",MODEL_ID],
    {cwd:root,env:{...process.env,PI_CODING_AGENT_DIR:run},encoding:"utf8",timeout:30000});
  assert.equal(result.status,0,result.stderr);assert.match(result.stdout,/scholarflow-local/);assert.match(result.stdout,/qwen38-vision/);
});

test("real Pi API serializes thinking/tool/image compatibility and blocks multi-image before HTTP",async()=>{
  const requests=[];
  const server=createServer(async(req,res)=>{
    let raw="";for await(const data of req)raw+=data;requests.push(JSON.parse(raw));
    res.writeHead(200,{"content-type":"text/event-stream"});
    const base={id:"synthetic",object:"chat.completion.chunk",created:0,model:MODEL_ID};
    res.write("data: "+JSON.stringify({...base,choices:[{index:0,delta:{role:"assistant",reasoning:"Synthetic reasoning.",content:"READY"},finish_reason:null}]})+"\n\n");
    res.end("data: "+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:"stop"}],usage:{prompt_tokens:12,completion_tokens:4,total_tokens:16}})+"\n\ndata: [DONE]\n\n");
  });
  await new Promise(r=>server.listen(0,"127.0.0.1",r));
  try{
    const model={...modelConfig(),provider:PROVIDER_ID,api:"openai-completions",baseUrl:`http://127.0.0.1:${server.address().port}/v1`};
    const invoke=boundedStream(streamSimple,{allowTools:true});
    for(const [reasoning,effort]of [[undefined,undefined],["low","low"],["medium","medium"],["high","xhigh"],["xhigh","xhigh"]]){
      const answer=await invoke(model,{...context,tools:[{name:"synthetic_echo",description:"Synthetic only",parameters:{type:"object",properties:{value:{type:"string"}},required:["value"]}}]}, {reasoning,timeoutMs:5000}).result();
      assert.equal(answer.stopReason,"stop",answer.errorMessage);assert.equal(answer.content.find(b=>b.type==="text").text,"READY");
      assert.equal(answer.content.find(b=>b.type==="thinking").thinking,"Synthetic reasoning.");
      const payload=requests.at(-1);assert.deepEqual(payload.chat_template_kwargs,effort?{enable_thinking:true,reasoning_effort:effort}:{enable_thinking:false});
      assert.equal(payload.max_tokens,4096);assert.equal(payload.reasoning_effort,undefined);assert.equal(payload.store,undefined);
      assert.equal(payload.messages[0].role,"system");assert.equal(payload.tools[0].function.strict,undefined);
    }
    const image={type:"image",data:"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jMwkAAAAASUVORK5CYII=",mimeType:"image/png"};
    const vision={...context,messages:[{role:"user",content:[image,{type:"text",text:"Synthetic image"}],timestamp:0}]};
    const one=await invoke(model,vision,{reasoning:"low",timeoutMs:5000}).result();assert.equal(one.stopReason,"stop");
    assert.match(requests.at(-1).messages.at(-1).content[0].image_url.url,/^data:image\/png;base64,/);
    const count=requests.length;const two=await invoke(model,{...vision,messages:[...vision.messages,...vision.messages]},{timeoutMs:5000}).result();
    assert.equal(two.stopReason,"error");assert.match(two.errorMessage,/at most one image/);assert.equal(requests.length,count);
  }finally{server.closeAllConnections();await new Promise(r=>server.close(r));}
});
