export function validatePdfGrant(grant) {
  const plain=v=>!!v&&typeof v==="object"&&!Array.isArray(v);
  const id=(v,re)=>typeof v==="string"&&v.length<=200&&re.test(v);
  if(!plain(grant)||Object.keys(grant).sort().join(",")!=="documents,principal"||typeof grant.principal!=="string"||!grant.principal||grant.principal.trim()!==grant.principal||grant.principal.length>160||!plain(grant.documents))return false;
  const entries=Object.entries(grant.documents);
  return entries.length>=1&&entries.length<=100&&entries.every(([alias,d])=>{
    if(!id(alias,/^[A-Za-z0-9_-]+$/)||!plain(d)||Object.keys(d).sort().join(",")!=="content_hash,import_id,page_ranges,stage_id,structure_hash"||!id(d.stage_id,/^stage-.+$/)||!id(d.import_id,/^pdf_[a-f0-9]{24}$/)||![d.content_hash,d.structure_hash].every(h=>id(h,/^sha256:[a-f0-9]{64}$/))||!Array.isArray(d.page_ranges)||d.page_ranges.length<1||d.page_ranges.length>100)return false;
    let last=0;return d.page_ranges.every(p=>{const ok=Array.isArray(p)&&p.length===2&&p.every(Number.isSafeInteger)&&p[0]>last&&p[0]>=1&&p[1]>=p[0]&&p[1]<=100000;last=p?.[1];return ok;});
  });
}
