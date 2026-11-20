"""Host-reviewed Obsidian policy projection; frozen bytes, recoverable steps.

This is not a natural-language constitution compiler or a multi-file transaction.
The host grant acknowledges the pinned policy document against this fixed policy.
"""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

from .contracts import KBWriteRequest
from .governed_writes import digest
from .kb_write_policy import evaluate_kb_write
from .kb_write_validator import required_side_effects
from .mutations import MutationError, record_fingerprint
from .note_projection import MAX_EXPORT_BYTES, file_hash, render_note
from .persistence import atomic_write_text, file_lock
from .proposal_state import load_proposal_state, save_proposal_state

POLICY = "obsidian-project-v1"
EXAMPLE_VAULT_POLICY = "obsidian-example_vault-v1"
LIBRARY_POLICY = "obsidian-library-v1"
VAULT_POLICIES = {POLICY,EXAMPLE_VAULT_POLICY,LIBRARY_POLICY}
MAX_PLAN_BYTES = 640000
TARGET_KEYS = {"stage_id","artifact_id","root","path","policy","policy_document","policy_document_hash",
               "repo_policy_hash","policy_reviewed_by","related_links","journal_date","files"}


def repo_policy_hash():
    root=Path(__file__).parent
    names=("contracts.py","kb_target_resolver.py","kb_write_policy.py","kb_write_validator.py","note_projection.py","vault_projection.py")
    return "sha256:"+digest({name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names})


def vault_path(root, relative):
    if not isinstance(relative,str) or not relative or "\\" in relative or ":" in relative:
        raise MutationError("permission_denied","Canonical relative vault path required")
    parts=relative.split("/")
    for part in parts:
        if (not part or part in {".",".."} or part.lower()==".obsidian" or part.endswith(("."," "))
                or any(ord(c)<32 or c in '<>"|?*[]`' for c in part)
                or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?",part)):
            raise MutationError("permission_denied","Unsafe vault path component")
    path=root.joinpath(*parts)
    if path.suffix!=".md": raise MutationError("permission_denied","Markdown vault files only")
    for component in (path,*path.parents):
        if component==root: break
        if component.is_symlink() or getattr(component,"is_junction",lambda:False)():
            raise MutationError("permission_denied","Vault aliases are not allowed")
    if root not in path.resolve().parents or (path.exists() and (not path.is_file() or path.stat().st_nlink>1)):
        raise MutationError("permission_denied","Regular non-aliased file inside vault required")
    return path


def validate_target(target):
    strict=isinstance(target,dict) and target.get("policy") in {EXAMPLE_VAULT_POLICY,LIBRARY_POLICY}
    library=isinstance(target,dict) and target.get("policy")==LIBRARY_POLICY
    keys=TARGET_KEYS|({"policy_documents","tags"} if strict else set())
    if not isinstance(target,dict) or set(target) not in (keys,keys|{"recovery_of"}) or target["policy"] not in VAULT_POLICIES:
        raise MutationError("permission_denied","Exact reviewed Obsidian policy target required")
    root=Path(target["root"])
    if not root.is_absolute() or not root.is_dir() or root.absolute()!=root.resolve():
        raise MutationError("permission_denied","Existing non-aliased absolute vault required")
    for component in (root,*root.parents):
        if component.is_symlink() or getattr(component,"is_junction",lambda:False)():
            raise MutationError("permission_denied","Vault root aliases are not allowed")
    if target["policy_reviewed_by"]!="interactive_user" or target["policy_document"]!="CLAUDE.md":
        raise MutationError("policy_rejected","Host review of the exact vault CLAUDE.md is required")
    for key in ("policy_document_hash","repo_policy_hash"):
        if not isinstance(target[key],str) or not re.fullmatch(r"sha256:[a-f0-9]{64}",target[key]):
            raise MutationError("policy_rejected","Pinned policy hashes required")
    policy=vault_path(root,target["policy_document"])
    if not policy.is_file(): raise MutationError("policy_rejected","Granted vault policy document is missing")
    if policy.stat().st_size>MAX_EXPORT_BYTES: raise MutationError("resource_limit","Policy document exceeds read budget")
    recovery=target.get("recovery_of")
    if recovery is not None and (not isinstance(recovery,str) or not re.fullmatch(r"sha256:[a-f0-9]{64}",recovery)):
        raise MutationError("permission_denied","Exact recovery proposal identity required")
    if not isinstance(target["related_links"],list) or not 1<=len(target["related_links"])<=20:
        raise MutationError("policy_rejected","Host-owned related links required")
    for link in target["related_links"]:
        if not isinstance(link,str) or not link.strip() or len(link)>300 or any(ord(c)<32 for c in link):
            raise MutationError("policy_rejected","Invalid host related link")
    if strict:
        documents=target["policy_documents"]
        if not isinstance(documents,dict) or set(documents)!={"CLAUDE.md","AGENTS.md"} or documents["CLAUDE.md"]!=target["policy_document_hash"]:
            raise MutationError("policy_rejected","Both vault instruction documents must be pinned")
        for name,value in documents.items():
            path=vault_path(root,name)
            if not isinstance(value,str) or not re.fullmatch(r"sha256:[a-f0-9]{64}",value) or not path.is_file() or path.stat().st_size>MAX_EXPORT_BYTES:
                raise MutationError("policy_rejected","Exact bounded policy documents required")
        tags=target["tags"]
        if not isinstance(tags,list) or not 1<=len(tags)<=10 or any(not isinstance(t,str) or not re.fullmatch(r"[^\s/#\[\]`]+/[^\s/#\[\]`]+",t) or len(t)>100 for t in tags):
            raise MutationError("policy_rejected","Hierarchical domain/subdomain tags required")
        for link in target["related_links"]:
            if not link.startswith("[[wiki/") or not link.endswith("]]"):
                raise MutationError("policy_rejected","Full wiki-path backlinks required")
            relative=link[2:-2]
            if not relative.endswith(".md"): relative+=".md"
            if not vault_path(root,relative).is_file():
                raise MutationError("policy_rejected","Granted backlink must resolve to an existing wiki page")
    date=target["journal_date"]
    if date is not None:
        try:
            if not isinstance(date,str) or datetime.strptime(date,"%Y-%m-%d").date().isoformat()!=date: raise ValueError()
        except (ValueError,TypeError): raise MutationError("policy_rejected","Exact UTC journal date required")
    note=vault_path(root,target["path"])
    if not target["path"].startswith("wiki/sources/ScholarFlow-Local/" if library else "wiki/syntheses/"):
        raise MutationError("policy_rejected","This policy only projects synthesis notes")
    if library and (date is not None or len(target["related_links"])<3):
        raise MutationError("policy_rejected","Library policy requires three existing backlinks and no project/journal writes")
    required=[target["path"],"wiki/log.md"]+([] if library else ["10_Projects/example_vault.md"])
    if date is not None: required.append(f"30_Journal/{date} work log.md")
    if strict: required.append("wiki/index.md")
    files=target["files"]
    if (not isinstance(files,dict) or not files or (set(files)!=set(required) if recovery is None else not set(files)<=set(required)) or len(required)!=len(set(required))
            or any(mode not in {"create_only","create_or_replace"} for mode in files.values())):
        raise MutationError("permission_denied","All and only required file effects must be explicitly granted")
    for relative in required: vault_path(root,relative)
    return note


def policy_matches(target):
    root=Path(target["root"])
    documents=target.get("policy_documents",{target["policy_document"]:target["policy_document_hash"]})
    return all(file_hash(vault_path(root,name))==value for name,value in documents.items()) and repo_policy_hash()==target["repo_policy_hash"]


def read_text(path):
    if not path.exists(): return None
    if path.stat().st_size>MAX_EXPORT_BYTES: raise MutationError("resource_limit","Existing vault file exceeds byte budget")
    raw=path.read_bytes()
    if len(raw)>MAX_EXPORT_BYTES: raise MutationError("resource_limit","Vault read exceeds byte budget")
    try: return raw.decode("utf-8")
    except UnicodeDecodeError: raise MutationError("policy_rejected","Vault file must be valid UTF-8; no silent conversion")


def append_once(before, block, marker, heading):
    if before is not None and marker in before: return before
    base=before if before is not None else heading+"\n"
    return base+("" if base.endswith("\n") else "\n")+"\n"+block+"\n"


def plan_projection(record,target,now,*,recovery=None):
    validate_target(target)
    if not policy_matches(target): raise MutationError("policy_changed","Policy document or implementation changed; host re-review required")
    root=Path(target["root"]); date=now.astimezone(timezone.utc).date().isoformat()
    if recovery: date=recovery["original"]["normalized_changes"]["utc_date"]
    if not recovery and target["journal_date"] is not None and target["journal_date"]!=date:
        raise MutationError("policy_rejected","Journal grant must match proposal UTC date; completed proposals keep their frozen date")
    library=target["policy"]==LIBRARY_POLICY
    request=KBWriteRequest(title=record["title"],content_kind="source" if library else "synthesis",body=record["content"],target_path=target["path"],
        source_context={"scope":"global" if library else "project","journal_required":target["journal_date"] is not None},
        related_links=target["related_links"],source_paths=["research-artifact:"+record["artifact_id"]],
        side_effects_acknowledged=list(target["files"])+([s for s,r in recovery["progress"]["steps"].items() if r["phase"]=="completed"] if recovery else []))
    decision=evaluate_kb_write(root,request,current_date=date)
    if not decision.allowed or any(not check.passed for check in decision.constitution_checks):
        raise MutationError("policy_rejected",decision.reason)
    paths=[target["path"],*required_side_effects(request,current_date=date)]
    if target["policy"] in {EXAMPLE_VAULT_POLICY,LIBRARY_POLICY}: paths.append("wiki/index.md")
    if not recovery and set(paths)!=set(target["files"]): raise MutationError("permission_denied","Side effects not fully granted")
    # JSON quotes title/newlines for frontmatter and one-line trace entries.
    q=lambda v:json.dumps(v,ensure_ascii=False)
    body=render_note(record).replace('policy: "local-markdown-v1"','policy: '+q(target["policy"]),1)
    body=body.replace('type: "scholarflow-note-projection"','type: "source"' if library else 'type: "synthesis"',1)
    body=body.replace("\n---\n\n","\nrelated: "+q(target["related_links"])+"\nsources: "+q(request.source_paths)+"\n---\n\n",1)
    if target["policy"] in {EXAMPLE_VAULT_POLICY,LIBRARY_POLICY}:
        body=body.replace("\n---\n\n","\ndate: "+q(date)+"\nupdated: "+q(date)+"\ntags: "+q(target["tags"])+"\n---\n\n",1)
        body=body.replace("status: "+q(record["status"]),"status: \"draft\"\nsource_note_status: "+q(record["status"]),1)
        body+="\n## 相关页面\n\n"+"\n".join("- "+link for link in target["related_links"])+"\n"
    marker="<!-- sf-projection:"+digest({"note":record_fingerprint(record),"path":target["path"],"policy":target["policy"]})+" -->"
    files=[]; dirs=set(); total=0
    for index,relative in enumerate(paths):
        if relative not in target["files"]: continue
        path=vault_path(root,relative); before=read_text(path)
        if before is not None and target["files"][relative]=="create_only":
            raise MutationError("permission_denied","Existing file is not authorized for replacement")
        if recovery:
            after=next(s["after_text"] for s in recovery["original"]["normalized_changes"]["files"] if s["relative_path"]==relative)
        elif index==0: after=body
        elif relative=="wiki/log.md":
            after=append_once(before,f"## {date}\n- **更新** {q(target['path'])} — {q(record['title'])}\n{marker}",marker,"# 操作日志")
        elif relative in {"10_Projects/example_vault.md","wiki/index.md"}:
            link="[["+target["path"]+"]]"
            heading="## ScholarFlow 受控笔记投影" if relative=="wiki/index.md" else "## 当前活跃知识项目"
            after=before if before is not None and link in before else append_once(before,f"{heading}\n- {link} - {q(record['title'])}",link,"# 知识库索引" if relative=="wiki/index.md" else "# example_vault")
        else: after=append_once(before,f"- KB sync: {q(record['title'])} -> {q(target['path'])}\n{marker}",marker,f"# {date} work log")
        if len(after.encode("utf-8"))>MAX_EXPORT_BYTES: raise MutationError("resource_limit","Rendered vault file exceeds byte budget")
        total+=len((before or "").encode("utf-8"))+len(after.encode("utf-8"))
        if total>MAX_PLAN_BYTES: raise MutationError("resource_limit","Complete frozen before/after plan exceeds byte budget")
        for parent in path.parents:
            if parent==root: break
            if not parent.exists(): dirs.add(parent.relative_to(root).as_posix())
        before_hash="sha256:"+hashlib.sha256(before.encode("utf-8")).hexdigest() if before is not None else record_fingerprint(None)
        if file_hash(path)!=before_hash: raise MutationError("target_version_conflict","Vault changed while freezing before bytes")
        files.append({"relative_path":relative,"path":str(path),"before_text":before,"before_hash":before_hash,
            "before_exists":before is not None,"before_hash_kind":"sha256_utf8_file_bytes" if before is not None else "missing_file_record_fingerprint_null_not_empty_file_hash",
            "trace_marker":marker if marker in after else None,
            "after_text":after,"after_hash":"sha256:"+hashlib.sha256(after.encode("utf-8")).hexdigest(),"permission":target["files"][relative]})
    return {"files":files,"utc_date":date,"missing_directories":sorted(dirs),"policy_id":target["policy"],"policy_version":1,
        "policy_document_hashes":target.get("policy_documents",{"CLAUDE.md":target["policy_document_hash"]}),
        "recovery_of":target.get("recovery_of"),"recovery_original_hash":digest(recovery["original"]) if recovery else None,
        "policy_document_hash":target["policy_document_hash"],"repo_policy_hash":target["repo_policy_hash"],
        "policy_checks":decision.to_dict(),"policy_coverage":"host-reviewed fixed repository rules; not arbitrary natural-language enforcement",
        "temporary_effects":"cooperating file locks and atomic replacement temporary files in granted directories"}


class VaultProjectionService:
    def __init__(self,store,*,clock=None,grant_guard=None):
        self.store,self.root=store,store.root_dir
        self.clock=clock or (lambda:datetime.now(timezone.utc)); self.grant_guard=grant_guard

    def get_proposal(self,pid):
        p=load_proposal_state(self.root)["proposals"].get(pid)
        if not p or p.get("operation")!="note.obsidian.project": raise MutationError("not_found","Vault projection not found")
        return p

    def plan(self,record,target):
        recovery=None
        if target.get("recovery_of"):
            original=self.get_proposal(target["recovery_of"])
            state=load_proposal_state(self.root); progress=state.get("projection_steps",{}).get(original["proposal_id"])
            if not progress or progress.get("phase")=="completed" or original["proposal_id"] in state["rejections"]:
                raise MutationError("scope_conflict","Recovery requires an existing unfinished multi-file operation")
            old=original["target"]
            for key in ("stage_id","artifact_id","root","path","policy","policy_document_hash","repo_policy_hash","journal_date","related_links","policy_documents","tags"):
                if old.get(key)!=target.get(key): raise MutationError("policy_rejected","Changed source or policy requires human reconciliation, not old-byte recovery")
            if record_fingerprint(record)!=original["base_fingerprint"]:
                raise MutationError("version_conflict","Changed source requires human reconciliation before a separate projection")
            unfinished={s["relative_path"] for s in original["normalized_changes"]["files"]
                if progress.get("steps",{}).get(s["relative_path"],{}).get("phase")!="completed"}
            if set(target["files"])!=unfinished:
                raise MutationError("permission_denied","Recovery grant must list exactly unfinished files, never completed targets")
            recovery={"original":original,"progress":progress}
        return plan_projection(record,target,self.clock(),recovery=recovery)

    def propose(self,target_id,target,*,expected_target_hash,request_id=None):
        note=self.store.get_research_artifact(target["artifact_id"])
        if not note or note.project_stage_id!=target["stage_id"] or note.kind.value!="synthesis" or not note.source_snapshot:
            raise MutationError("scope_conflict","Existing exact-project source note required")
        plan=self.plan(note.to_dict(),target)
        if expected_target_hash!="sha256:"+digest(plan): raise MutationError("target_version_conflict","Vault plan changed; reread all effects")
        identity={"operation":"note.obsidian.project","target":{"target_id":target_id,**target},"mode":"update",
            "base_fingerprint":record_fingerprint(note.to_dict()),"normalized_changes":plan}
        now=self.clock(); pid="sha256:"+digest(identity); state=load_proposal_state(self.root)
        if pid in state["proposals"]: return state["proposals"][pid]
        p={"proposal_version":1,"proposal_id":pid,**identity,"created_at":now.isoformat(),"expires_at":(now+timedelta(hours=24)).isoformat(),
            "diff":{"files":[{k:s[k] for k in ("path","before_hash","after_hash")} for s in plan["files"]],"implicit_files":[],
                "directories":plan["missing_directories"]},"policy":{"requires_confirmation":True,"version":POLICY,"side_effect_class":"external_multi_file_write"},
            "warnings":["Not a cross-file transaction or AI indexing. Host-reviewed fixed policy, not automatic CLAUDE.md interpretation."]}
        state["proposals"][pid]=p; save_proposal_state(self.root,state); return p

    def receipt(self,p):
        step=load_proposal_state(self.root).get("projection_steps",{}).get(p["proposal_id"])
        return step if step and step.get("phase")=="completed" else None

    def apply(self,pid,*,confirmation,request_id=None):
        p=self.get_proposal(pid)
        if not confirmation or confirmation.get("proposal_id")!=pid or confirmation.get("confirmed") is not True or confirmation.get("confirmed_by")!="interactive_user":
            raise MutationError("confirmation_required","Separate exact-hash operator confirmation required")
        target={k:v for k,v in p["target"].items() if k!="target_id"}; validate_target(target)
        plan=p["normalized_changes"]; root=Path(target["root"])
        with ExitStack() as locks:
            locks.enter_context(file_lock(self.root/"research_artifacts.json"))
            locks.enter_context(file_lock(root/".scholarflow-projection"))
            state=load_proposal_state(self.root); previous=state.get("projection_steps",{}).get(pid)
            if previous and previous.get("phase")=="completed": return previous
            progress=previous or {"proposal_id":pid,"operation":p["operation"],"phase":"partial","steps":{},"source_fingerprint":p["base_fingerprint"],
                "policy_document_hash":plan["policy_document_hash"],"repo_policy_hash":plan["repo_policy_hash"],"utc_date":plan["utc_date"],
                "observation":"expected-byte recovery is not exclusive-writer proof; no cross-file atomicity"}
            def preflight():
                validate_target(target)
                if self.grant_guard: self.grant_guard(p["target"])
                if self.clock()>=datetime.fromisoformat(p["expires_at"]): raise MutationError("expired_proposal","Vault approval expired; historical steps retained")
                if not policy_matches(target): raise MutationError("policy_changed","Policy changed; remaining writes stopped")
                note=self.store.get_research_artifact(target["artifact_id"])
                if record_fingerprint(note.to_dict() if note else None)!=p["base_fingerprint"]: raise MutationError("version_conflict","Source changed; remaining writes stopped")
                for step in plan["files"]:
                    current=file_hash(vault_path(root,step["relative_path"])); done=progress["steps"].get(step["relative_path"])
                    expected=step["after_hash"] if done and done["phase"]=="completed" else step["before_hash"]
                    recover=done and done["phase"]=="intent" and current==step["after_hash"]
                    if current!=expected and not recover: raise MutationError("target_version_conflict","Manual changes preserved; remaining writes stopped")
            preflight()  # No new vault directories before complete source/policy/target preflight.
            for step in sorted(plan["files"],key=lambda s:s["path"].casefold()):
                path=vault_path(root,step["relative_path"]); path.parent.mkdir(parents=True,exist_ok=True)
                vault_path(root,step["relative_path"]); locks.enter_context(file_lock(path))
            preflight()
            for step in plan["files"]:
                relative=step["relative_path"]; done=progress["steps"].get(relative)
                if done and done["phase"]=="completed": continue
                preflight()
                path=vault_path(root,relative); current=file_hash(path)
                recovered=bool(done and current==step["after_hash"])
                progress["steps"][relative]={"phase":"intent","path":str(path),"before_hash":step["before_hash"],"after_hash":step["after_hash"],"request_id":request_id}
                state=load_proposal_state(self.root); state.setdefault("projection_steps",{})[pid]=progress
                save_proposal_state(self.root,state)
                preflight()  # Includes fresh host grant read immediately before every write.
                if not recovered and current!=step["after_hash"]: atomic_write_text(path,step["after_text"])
                if file_hash(path)!=step["after_hash"]: raise MutationError("target_version_conflict","Post-write drift; inspect durable intent")
                progress["steps"][relative]={**progress["steps"][relative],"phase":"completed","completed_at":self.clock().isoformat(),"recovery_observed_expected_bytes":recovered}
                state=load_proposal_state(self.root); state.setdefault("projection_steps",{})[pid]=progress; save_proposal_state(self.root,state)
            preflight()
            progress={**progress,"phase":"completed","completed_at":self.clock().isoformat()}
            state=load_proposal_state(self.root); state.setdefault("projection_steps",{})[pid]=progress
            state["commits"][pid]={"response":progress,"committed_at":progress["completed_at"]}; save_proposal_state(self.root,state)
            return progress

    def observation(self,p):
        target={k:v for k,v in p["target"].items() if k!="target_id"}; root=Path(target["root"])
        saved=load_proposal_state(self.root).get("projection_steps",{}).get(p["proposal_id"])
        steps=[]
        for step in p["normalized_changes"]["files"]:
            receipt=(saved or {}).get("steps",{}).get(step["relative_path"])
            steps.append({"relative_path":step["relative_path"],"phase":receipt["phase"] if receipt else "not_started",
                "receipt":receipt,"matches_current":file_hash(vault_path(root,step["relative_path"]))==step["after_hash"],
                "before_matches_current":file_hash(vault_path(root,step["relative_path"]))==step["before_hash"]})
        return {"projection_steps":steps,"partial_execution":bool(saved and saved.get("phase")!="completed"),
            "policy_matches_current":policy_matches(target),"projection_target_matches_current":all(s["matches_current"] for s in steps),
            "vault_sync":bool(saved and saved.get("phase")=="completed"),"vault_sync_is_historical":True,"ai_index_updated":False,
            "projection_receipt_kind":"multi_file_steps_not_cross_file_transaction","recovery":"No automatic rollback; rereview unfinished targets after source/policy/target drift"}
