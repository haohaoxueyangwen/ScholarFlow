import {isAbsolute} from "node:path";
export function validateProjectionGrant(value) {
  if (!value.targets || typeof value.targets !== "object" || Array.isArray(value.targets)) return false;
  const entries = Object.entries(value.targets);
  if (!entries.length || entries.length > 100) return false;
  const safe=p=>typeof p==="string"&&p.length>0&&p.split("/").every(x=>x&&!/[\\:<>"|?*\x00-\x1f]/.test(x)&&!/[. ]$/.test(x)&&x!==".."&&x.toLowerCase()!==".obsidian");
  return entries.every(([id,t])=>{
    if (!(id.trim()===id&&id.length>0&&id.length<=200&&t&&typeof t==="object"
      &&typeof t.stage_id==="string"&&/^stage-.+/.test(t.stage_id)&&typeof t.artifact_id==="string"&&/^artifact-.+/.test(t.artifact_id)
      &&typeof t.root==="string"&&isAbsolute(t.root)&&safe(t.path)&&/\.md$/i.test(t.path))) return false;
    if(t.policy==="local-markdown-v1") return JSON.stringify(Object.keys(t).sort())===JSON.stringify(["artifact_id","path","policy","root","stage_id","subdir"])
      &&safe(t.subdir)&&t.path.startsWith(t.subdir+"/");
    const keys=["stage_id","artifact_id","root","path","policy","policy_document","policy_document_hash","repo_policy_hash","policy_reviewed_by","related_links","journal_date","files"].sort();
    const library=t.policy==="obsidian-library-v1";
    const strict=t.policy==="obsidian-example_vault-v1"||library;
    if(strict){keys.push("policy_documents","tags");keys.sort();}
    if(Object.hasOwn(t,"recovery_of")) {
      if(typeof t.recovery_of!=="string"||!/^sha256:[a-f0-9]{64}$/.test(t.recovery_of)) return false;
      keys.push("recovery_of");keys.sort();
    }
    if(!["obsidian-project-v1","obsidian-example_vault-v1","obsidian-library-v1"].includes(t.policy)||JSON.stringify(Object.keys(t).sort())!==JSON.stringify(keys)
      ||t.policy_document!=="CLAUDE.md"||t.policy_reviewed_by!=="interactive_user"||!t.path.startsWith(library?"wiki/sources/ScholarFlow-Local/":"wiki/syntheses/")
      ||![t.policy_document_hash,t.repo_policy_hash].every(h=>typeof h==="string"&&/^sha256:[a-f0-9]{64}$/.test(h))
      ||!Array.isArray(t.related_links)||!t.related_links.length||t.related_links.length>20
      ||!t.related_links.every(x=>typeof x==="string"&&x.trim().length>0&&x.length<=300&&!/[\x00-\x1f]/.test(x))
      ||!(t.journal_date===null||typeof t.journal_date==="string"&&/^\d{4}-\d{2}-\d{2}$/.test(t.journal_date))
      ||!t.files||typeof t.files!=="object"||Array.isArray(t.files)) return false;
    if(strict&&(!t.policy_documents||JSON.stringify(Object.keys(t.policy_documents).sort())!==JSON.stringify(["AGENTS.md","CLAUDE.md"])
      ||t.policy_documents["CLAUDE.md"]!==t.policy_document_hash||!Object.values(t.policy_documents).every(h=>/^sha256:[a-f0-9]{64}$/.test(h))
      ||!Array.isArray(t.tags)||!t.tags.length||t.tags.length>10||!t.tags.every(t=>typeof t==="string"&&/^[^\s/#\[\]`]+\/[^\s/#\[\]`]+$/.test(t)&&t.length<=100)
      ||!t.related_links.every(l=>/^\[\[wiki\/.+\]\]$/.test(l)))) return false;
    if(library&&(t.journal_date!==null||t.related_links.length<3))return false;
    const paths=[t.path,"wiki/log.md",...(library?[]:["10_Projects/example_vault.md"]),...(t.journal_date===null?[]:[`30_Journal/${t.journal_date} work log.md`]),...(strict?["wiki/index.md"]:[])].sort();
    const granted=Object.keys(t.files).sort();
    return (t.recovery_of?granted.length>0&&granted.every(p=>paths.includes(p)):JSON.stringify(granted)===JSON.stringify(paths))
      &&granted.every(p=>safe(p)&&["create_only","create_or_replace"].includes(t.files[p]));
  });
}
