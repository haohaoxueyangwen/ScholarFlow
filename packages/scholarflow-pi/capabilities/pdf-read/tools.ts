import { Type } from "typebox";
import { readFileSync } from "node:fs";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import type {ExtensionAPI} from "@earendil-works/pi-coding-agent";
import {callReadOperation} from "../../harness/operation-client.ts";
import {validatePdfGrant} from "./grant.mjs";

const directory=dirname(fileURLToPath(import.meta.url));
export function pdfRead(pi: ExtensionAPI) {
  const file=process.env.SCHOLARFLOW_PDF_READ_GRANT_FILE;
  if(!file)throw new Error("Host PDF grant file required");
  const grant=JSON.parse(readFileSync(file,"utf8").replace(/^\uFEFF/,""));
  if(!validatePdfGrant(grant))throw new Error("Invalid host PDF grant");
  const call=(operation:string,id:string,params:any,signal?:AbortSignal,ctx?:any)=>callReadOperation(pi,operation,params,{kind:"project_stage",stage_id:params.stage_id},null,id,signal,15000,ctx,{adapter:resolve(directory,"adapter.py"),grantFile:file});
  const common={stage_id:Type.String({minLength:1,maxLength:200}),document_id:Type.String({minLength:1,maxLength:200})};
  return {id:"pdf-read",scopePolicy:"Read only host-approved staged PDFs. Bindings: "+JSON.stringify(Object.entries(grant.documents).map(([document_id,d]:any)=>({document_id,stage_id:d.stage_id,page_ranges:d.page_ranges})))+". Bindings are a discovery snapshot, not permanent authority; every call revalidates current grant and source hashes. No file paths, parsing, writes or index effects.",tools:[
    {name:"sf_pdf_inspect",label:"Inspect approved PDF",description:"Read diagnostics and paginated block IDs for an exact host-approved project/document alias. No body text; follow next_offset and do not claim full-document coverage from one page.",
      parameters:Type.Object({...common,offset:Type.Optional(Type.Integer({minimum:0})),limit:Type.Optional(Type.Integer({minimum:1,maximum:100}))},{additionalProperties:false}),
      async execute(id:string,params:any,signal?:AbortSignal,_update?:any,ctx?:any){return call("pdf.inspect",id,params,signal,ctx);}},
    {name:"sf_pdf_read_fragment",label:"Read approved PDF fragment",description:"Read one exact authorized block and optional Unicode character range, with page/hash/parser provenance. At most 64000 UTF-8 bytes; ask for a smaller range on resource_limit, never silently truncate. Translation is derived, not source text.",
      parameters:Type.Object({...common,block_id:Type.String({minLength:1}),char_start:Type.Optional(Type.Integer({minimum:0})),char_end:Type.Optional(Type.Integer({minimum:1}))},{additionalProperties:false}),
      async execute(id:string,params:any,signal?:AbortSignal,_update?:any,ctx?:any){return call("pdf.fragment",id,params,signal,ctx);}},
  ]};
}
