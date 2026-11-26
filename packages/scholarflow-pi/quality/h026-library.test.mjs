import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,writeFileSync,mkdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {loadExtensions} from '../node_modules/@earendil-works/pi-coding-agent/dist/core/extensions/loader.js';
import {validateLibraryGrant} from '../capabilities/library-read/grant.mjs';
import {resolveComposition,catalog} from '../harness/composition.mjs';
const pkg=resolve(dirname(fileURLToPath(import.meta.url)),'..');

test('library exact host grant rejects path, approval, malformed scope and aliases in model fields',()=>{
 const g={principal:'pi-local',stage_ids:['stage-library-a']};assert.equal(validateLibraryGrant(g),true);
 for(const value of [{...g,approve:true},{...g,root:'private'},{...g,stage_ids:['stage-library-../../x']},{...g,principal:' pi-local'},{...g,stage_ids:[]}])assert.equal(validateLibraryGrant(value),false);
});
test('library missing opt-in does not disable the other independent capability',()=>{
 const previous=process.env.SCHOLARFLOW_LIBRARY_GRANT_FILE;delete process.env.SCHOLARFLOW_LIBRARY_GRANT_FILE;
 try{const c=resolveComposition({ids:['project-evidence','library-read']});assert.deepEqual(c.enabled.map(x=>x.id),['project-evidence']);assert.equal(c.diagnostics[0].id,'library-read');}finally{if(previous!==undefined)process.env.SCHOLARFLOW_LIBRARY_GRANT_FILE=previous;}
});
test('PDF note native schema retains optionality and constrains quotation keys to source IDs',async()=>{
 const loaded=await loadExtensions([resolve(pkg,'capabilities/pdf-note/extension.ts')],pkg);assert.deepEqual(loaded.errors,[]);
 const tools=loaded.extensions[0].tools;assert.deepEqual([...tools.keys()],['sf_pdf_note_context','sf_pdf_note_propose','sf_pdf_note_status']);
 const schema=tools.get('sf_pdf_note_propose').definition.parameters;assert.equal(schema.required.includes('quotes'),false);assert.equal(schema.properties.quotes.additionalProperties,false);assert.deepEqual(Object.keys(schema.properties.quotes.patternProperties),['^pdf-source-[a-f0-9]{24}$']);assert.equal(schema.properties.source_versions.additionalProperties,false);
});
test('standalone native library extension exposes only search and protects host files',async()=>{
 const dir=mkdtempSync(resolve(tmpdir(),'sf-h026-'));const file=resolve(dir,'grant.json');writeFileSync(file,JSON.stringify({principal:'pi-local',stage_ids:['stage-library-a']}));
 const previous=process.env.SCHOLARFLOW_LIBRARY_GRANT_FILE;process.env.SCHOLARFLOW_LIBRARY_GRANT_FILE=file;
 try{const loaded=await loadExtensions([resolve(catalog['library-read'].root,'extension.ts')],pkg);assert.deepEqual(loaded.errors,[]);const extension=loaded.extensions[0];assert.deepEqual([...extension.tools.keys()],['sf_library_search']);const guard=extension.handlers.get('tool_call')[0];assert.equal((await guard({toolName:'read',input:{path:file}},{cwd:pkg})).block,true);assert.equal((await guard({toolName:'bash',input:{}},{cwd:pkg})).block,true);assert.equal(await guard({toolName:'read',input:{path:resolve(catalog['library-read'].root,'skills/library-read/SKILL.md')}},{cwd:pkg}),undefined);}finally{if(previous===undefined)delete process.env.SCHOLARFLOW_LIBRARY_GRANT_FILE;else process.env.SCHOLARFLOW_LIBRARY_GRANT_FILE=previous;}
});
