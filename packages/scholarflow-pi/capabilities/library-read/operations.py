"""Project-scoped pure retrieval over a host-owned local LightRAG index."""
import json,re,subprocess,os
from pathlib import Path
from scholarflow.domain_queries import DomainQueryError
from scholarflow.read_operations import Field,Operation,ReadFacade,ReadGrant

ROOT=Path(__file__).resolve().parents[4]

def load_grant(path):
    p=Path(path).absolute()
    try:
        if p.resolve()!=p or any(x.is_symlink() or getattr(x,'is_junction',lambda:False)() for x in (p,*p.parents)) or not p.is_file() or p.stat().st_nlink!=1 or p.stat().st_size>65536:raise ValueError()
        raw=p.read_bytes();value=json.loads(raw.decode('utf-8-sig'))
        if set(value)!={'principal','stage_ids'} or not isinstance(value['principal'],str) or not 1<=len(value['principal'])<=160 or value['principal'].strip()!=value['principal'] or not isinstance(value['stage_ids'],list) or not 1<=len(value['stage_ids'])<=100 or any(not isinstance(s,str) or not re.fullmatch(r'stage-[A-Za-z0-9_-]{1,160}',s) for s in value['stage_ids']):raise ValueError()
        return ReadGrant(value['principal'],frozenset({'library-read'}),stage_ids=frozenset(value['stage_ids'])),raw
    except (OSError,ValueError,UnicodeError,KeyError,TypeError):raise DomainQueryError('permission_denied','Current host library grant unavailable or invalid') from None

def read_grant(path):return load_grant(path)[0]

def build_facade(data_dir,grant_file,audit_path=None):
    home=Path(os.environ.get('SCHOLARFLOW_LIBRARY_HOME',str(ROOT/'.local/research-library'))).absolute()
    expected=Path(os.environ.get('SCHOLARFLOW_LIBRARY_FACTS_DIR',str(home/'facts'))).absolute()
    actual=Path(data_dir).absolute()
    if actual!=expected or actual.resolve()!=actual or not (expected==home/'facts' or home==expected/'_research_knowledge') or any(p.is_symlink() or getattr(p,'is_junction',lambda:False)() for p in (home,*home.parents)):raise DomainQueryError('scope_conflict','Library capability requires its host-owned facts root; another data_dir must not query this library')
    def search(snapshot,params,control,authority):
        grant,raw=load_grant(grant_file)
        if grant.principal!=authority.principal or params['stage_id'] not in grant.stage_ids:raise DomainQueryError('permission_denied','Library scope revoked')
        if len(params['query'])>2000:raise DomainQueryError('invalid_argument','Query exceeds 2000 characters')
        control.check()
        args=[str(ROOT/'.tmp/lightrag-native/venv/Scripts/python.exe'),'-s','-B',str(ROOT/'scripts/library_knowledge.py'),'query','--stage-id',params['stage_id'],'--query',params['query'],'--limit',str(params.get('limit',5))]
        try:completed=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=27)
        except subprocess.TimeoutExpired:raise DomainQueryError('timeout','Local semantic retrieval deadline elapsed') from None
        except OSError:raise DomainQueryError('dependency_unavailable','Pinned local LightRAG runtime unavailable') from None
        if completed.returncode:raise DomainQueryError('dependency_unavailable','Local index/embedding unavailable; no text-model fallback')
        try:data=json.loads(completed.stdout)
        except ValueError:raise DomainQueryError('internal_error','Invalid local knowledge response') from None
        if not isinstance(data,dict) or not isinstance(data.get('items'),list) or data.get('text_generation_calls')!=0 or any(i.get('stage_id')!=params['stage_id'] for i in data['items']):raise DomainQueryError('scope_conflict','Knowledge response violates project/LLM boundary')
        if load_grant(grant_file)[1]!=raw:raise DomainQueryError('permission_denied','Library grant changed during retrieval')
        control.check();return data
    op=Operation('library.search','library-read','project_stage',{'stage_id':Field(str,True),'query':Field(str,True),'limit':Field(int,minimum=1,maximum=10)},lambda *_:None,stage_field='stage_id',context_handler=search)
    return ReadFacade([op],data_dir,audit_path)
