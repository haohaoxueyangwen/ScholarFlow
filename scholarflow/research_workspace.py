"""Host-selected scope and operator controls; Pi remains the only model runtime."""
from pathlib import Path
from uuid import uuid4
import hashlib,json,re
from .storage import ScholarFlowStore
from .reader_workspace import ReaderWorkspaceStore,MAX_READER_PDF_BYTES
from .pdf_reading import PdfReadGrant,PdfSourceService
from .pdf_notes import capability_module,read_pdf_note_sources
from .persistence import atomic_write_json
from .proposal_state import load_proposal_state
from .mutations import record_fingerprint

ROOT=Path(__file__).resolve().parents[1]
HOME=ROOT/'.local/research-workspaces'
DEFAULT_DATA=ROOT/'.local/research-library/facts'

def sha(raw):return 'sha256:'+hashlib.sha256(raw).hexdigest()
def safe(path,*,file=False,exists=True):
    path=Path(path).absolute()
    if path.resolve()!=path or any(p.is_symlink() or getattr(p,'is_junction',lambda:False)() for p in (path,*path.parents)):
        raise ValueError('Canonical paths required; links/junctions are not authorized')
    if exists and not path.exists():raise ValueError('Selected path does not exist')
    if path.exists() and not file and not path.is_dir():raise ValueError('Directory required for this scope')
    if file and (not path.is_file() or path.stat().st_nlink!=1):raise ValueError('Regular, non-hardlinked file required')
    return path

def projects(data=DEFAULT_DATA):
    data=safe(data)
    return sorted([p.to_dict() for p in ScholarFlowStore(data).list_project_stages() if not p.stage_id.startswith('stage-library-')],key=lambda p:(p.get('project_name',''),p['stage_id']))

def page_ranges(value,count):
    if not isinstance(value,str) or not value.strip():raise ValueError('Explicit page range required')
    result=[]
    for part in value.split(','):
        if not re.fullmatch(r'\s*\d+(?:-\d+)?\s*',part):raise ValueError('Use pages such as 1 or 1-3,5')
        pair=[int(x) for x in part.strip().split('-')];a,b=(pair*2 if len(pair)==1 else pair)
        if not 1<=a<=b<=count:raise ValueError('Pages exceed the parsed PDF')
        result.append([a,b])
    result.sort()
    if any(a[1]>=b[0] for a,b in zip(result,result[1:])):raise ValueError('Page ranges must not overlap')
    return result

def library_home(data):
    data=safe(data)
    return safe(data.parent if data.name == 'facts' else data/'_research_knowledge',exists=False)

def knowledge_configured(data,stage):
    path=library_home(data)/'project-indexes'/hashlib.sha256(stage.encode()).hexdigest()[:32]/'active-sources.json'
    return path.is_file() and any(not x.get('revoked') for x in json.loads(path.read_text(encoding='utf-8')).get('entries',{}).values())

def prepare(data,stage_id,*,pdf=None,import_id=None,pages='1',note_blocks=None,provider='deepseek',home=HOME):
    data=safe(data);store=ScholarFlowStore(data);stage=store.get_project_stage(stage_id)
    if not stage or stage_id.startswith('stage-library-'):raise ValueError('Select an existing exact research project, not a source group')
    if provider not in {'deepseek','scholarflow-local'}:raise ValueError('Explicit supported provider required')
    home=safe(home,exists=False);home.mkdir(parents=True,exist_ok=True)
    folder=home/('workspace-'+uuid4().hex);folder.mkdir();safe(folder)
    grants=folder/'grants';grants.mkdir()
    manifest={'version':1,'workspace_id':folder.name,'data_dir':str(data),'stage_id':stage_id,'project_fingerprint':record_fingerprint(stage.to_dict()),'project_title':stage.title,'provider':provider,'pdf':None,'knowledge_configured':knowledge_configured(data,stage_id)}
    if pdf is not None and import_id is not None:raise ValueError('Choose either an original PDF or an existing import')
    if pdf is not None or import_id is not None:
        reader=ReaderWorkspaceStore(data)
        if import_id is not None:
            item=reader.get_import(import_id)
            if stage_id not in item.get('project_stage_ids',[]):raise ValueError('PDF is not linked to the selected project')
            original=safe(reader.pdf_path(import_id),file=True)
        else:original=safe(pdf,file=True)
        if original.suffix.lower()!='.pdf' or not 0<original.stat().st_size<=MAX_READER_PDF_BYTES:raise ValueError('Select a PDF between 1 byte and 80 MB')
        raw=original.read_bytes()
        if len(raw)>MAX_READER_PDF_BYTES:raise ValueError('PDF grew beyond the import limit')
        if import_id is None:item=reader.import_pdf(original.name,raw,project_stage_ids=[stage_id])
        elif sha(raw)!=item.get('content_hash'):raise ValueError('Imported PDF changed after upload')
        if item['status']!='parsed':raise ValueError('PDF text unavailable: '+item.get('parse_error','unknown')+'; no reading grant created')
        parsed=data/'_reader/imports'/item['import_id']/'structured.json';structure=json.loads(parsed.read_text(encoding='utf-8'))
        allowed=page_ranges(pages,structure['page_count']);alias='selected-pdf'
        value={'principal':'interactive-user','documents':{alias:{'stage_id':stage_id,'import_id':item['import_id'],'content_hash':item['content_hash'],'structure_hash':sha(parsed.read_bytes()),'page_ranges':allowed}}}
        authority=PdfReadGrant.from_dict(value);atomic_write_json(grants/'pdf-read.json',value)
        all_blocks=[b for b in structure['blocks'] if isinstance(b.get('page_start'),int) and isinstance(b.get('page_end'),int) and any(a<=b['page_start']<=b['page_end']<=z for a,z in allowed) and b.get('text','').strip()]
        selected=all_blocks[:8] if note_blocks is None else [next((b for b in all_blocks if b['block_id']==key),None) for key in note_blocks]
        if not selected or any(b is None for b in selected) or len(selected)>8 or len({b['block_id'] for b in selected})!=len(selected):raise ValueError('Choose 1-8 distinct blocks inside the approved pages')
        selectors=[{'document_id':alias,'block_id':b['block_id'],'char_start':0,'char_end':len(b['text'])} for b in selected]
        artifact='artifact-'+uuid4().hex
        note={'principal':'interactive-user','create_notes':{artifact:stage_id},'update_notes':{},'pdf_grant_file':str(grants/'pdf-read.json'),'pdf_sources':{stage_id:selectors}}
        api=capability_module('pdf-note');read_pdf_note_sources(store,stage_id,api.PdfNoteGrant.from_dict(note))
        atomic_write_json(grants/'pdf-note.json',note)
        manifest['pdf']={'document_id':alias,'artifact_id':artifact,'import_id':item['import_id'],'original_path':str(original),'original_hash':sha(raw),'page_ranges':allowed,'selected_blocks':selectors,'total_page_blocks':len(all_blocks),'note_blocks':len(selectors)}
        # Observe the same service the model will use before publishing the scope.
        PdfSourceService(data).inspect(stage_id,alias,authority)
    if manifest['knowledge_configured']:atomic_write_json(grants/'library-read.json',{'principal':'interactive-user','stage_ids':[stage_id]})
    workflow={'principal':'interactive-user','stage_id':stage_id,'document_id':manifest['pdf']['document_id'] if manifest['pdf'] else None,'artifact_id':manifest['pdf']['artifact_id'] if manifest['pdf'] else None,'knowledge_configured':manifest['knowledge_configured']}
    atomic_write_json(grants/'workflow.json',workflow);atomic_write_json(folder/'workspace.json',manifest)
    return folder/'workspace.json'

def load(path,*,refresh=False):
    path=safe(path,file=True);m=json.loads(path.read_text(encoding='utf-8'))
    fields={'version','workspace_id','data_dir','stage_id','project_fingerprint','project_title','provider','pdf','knowledge_configured'}
    if set(m)!=fields or m['version']!=1 or path.name!='workspace.json' or path.parent.name!=m['workspace_id'] or not re.fullmatch(r'workspace-[a-f0-9]{32}',m['workspace_id']):raise ValueError('Invalid host workspace identity')
    if m['provider'] not in {'deepseek','scholarflow-local'} or type(m['knowledge_configured']) is not bool:raise ValueError('Invalid provider or knowledge capability state')
    stage=ScholarFlowStore(safe(m['data_dir'])).get_project_stage(m['stage_id'])
    if not stage or record_fingerprint(stage.to_dict())!=m['project_fingerprint']:raise ValueError('Project changed; prepare a fresh scope before continuing')
    if m['pdf']:
        original=safe(m['pdf']['original_path'],file=True)
        if sha(original.read_bytes())!=m['pdf']['original_hash']:raise ValueError('Original PDF changed; old workspace is not current')
        grant=PdfReadGrant.from_dict(json.loads(safe(path.parent/'grants/pdf-read.json',file=True).read_text(encoding='utf-8')))
        if set(grant.documents)!={m['pdf']['document_id']} or grant.principal!='interactive-user' or grant.documents[m['pdf']['document_id']]['stage_id']!=m['stage_id']:raise ValueError('PDF authority differs from selected project')
        PdfSourceService(m['data_dir']).inspect(m['stage_id'],m['pdf']['document_id'],grant)
        api=capability_module('pdf-note');ng=json.loads(safe(path.parent/'grants/pdf-note.json',file=True).read_text(encoding='utf-8'))
        if ng['create_notes']!={m['pdf']['artifact_id']:m['stage_id']} or ng['update_notes'] or ng['pdf_grant_file']!=str(path.parent/'grants/pdf-read.json') or ng['pdf_sources']!={m['stage_id']:m['pdf']['selected_blocks']}:raise ValueError('Note authority differs from selected fragments')
        read_pdf_note_sources(ScholarFlowStore(m['data_dir']),m['stage_id'],api.PdfNoteGrant.from_dict(ng))
    expected={'principal':'interactive-user','stage_id':m['stage_id'],'document_id':m['pdf']['document_id'] if m['pdf'] else None,'artifact_id':m['pdf']['artifact_id'] if m['pdf'] else None,'knowledge_configured':m['knowledge_configured']}
    actual_workflow=json.loads(safe(path.parent/'grants/workflow.json',file=True).read_text(encoding='utf-8'))
    recovery=refresh and not m['knowledge_configured'] and actual_workflow==dict(expected,knowledge_configured=True) and knowledge_configured(m['data_dir'],m['stage_id'])
    if actual_workflow!=expected and not recovery:raise ValueError('Workflow authority differs from selected workspace')
    if m['knowledge_configured'] and json.loads(safe(path.parent/'grants/library-read.json',file=True).read_text(encoding='utf-8'))!={'principal':'interactive-user','stage_ids':[m['stage_id']]}:raise ValueError('Knowledge authority differs from selected project')
    if refresh and not m['knowledge_configured'] and knowledge_configured(m['data_dir'],m['stage_id']):
        grant_path=path.parent/'grants/library-read.json';library_grant={'principal':'interactive-user','stage_ids':[m['stage_id']]}
        if grant_path.exists() and json.loads(safe(grant_path,file=True).read_text(encoding='utf-8'))!=library_grant:raise ValueError('Knowledge authority differs from selected project')
        atomic_write_json(grant_path,library_grant)
        m['knowledge_configured']=True;expected['knowledge_configured']=True
        atomic_write_json(path.parent/'grants/workflow.json',expected);atomic_write_json(path,m)
    return m

def call(facade,grant,prefix,action,scope,params,workspace):
    request={'protocol_version':1,'request_id':'host-'+uuid4().hex,'task_id':workspace['workspace_id'],'operation':prefix+'.'+action,'scope':scope,'params':params,'idempotency_key':workspace['workspace_id']+'-'+prefix}
    result=facade.execute(request,grant,operator=action=='decide')
    if not result['ok']:raise ValueError(json.dumps(result.get('error',result),ensure_ascii=False))
    return result['data']

def note_facade(path):
    m=load(path)
    if not m['pdf']:raise ValueError('No PDF note in this workspace')
    api=capability_module('pdf-note');grant=api.PdfNoteGrant.from_dict(json.loads((Path(path).parent/'grants/pdf-note.json').read_text(encoding='utf-8')))
    return m,api.PdfNoteFacade(m['data_dir'],grant),grant,{'kind':'note','stage_id':m['stage_id'],'artifact_id':m['pdf']['artifact_id']}

def proposals(path):
    m,facade,grant,scope=note_facade(path);result=[]
    for pid,p in load_proposal_state(Path(m['data_dir'])).get('proposals',{}).items():
        if p.get('operation')=='research_artifact.publish' and p.get('target')=={'artifact_id':m['pdf']['artifact_id']}:
            result.append(call(facade,grant,'pdf.note','status',scope,{'proposal_id':pid},m))
    return result

def decide(path,proposal_id,reviewed_hash,*,decision='approve'):
    m,facade,grant,scope=note_facade(path)
    result=call(facade,grant,'pdf.note','decide',scope,{'proposal_id':proposal_id,'decision':decision,'reviewed_hash':reviewed_hash},m)
    if decision=='approve' and result['phase']=='committed':
        try:
            from .workspace_knowledge import enqueue
            result['knowledge']=enqueue(path)
        except Exception as error:result['knowledge']={'status':'failed','error_code':str(error)[:300],'note_saved':True}
    return result

def export(path,*,reviewed_hash=None):
    path=safe(path,file=True);m=load(path)
    if not m['pdf']:raise ValueError('No PDF note in this workspace')
    folder=path.parent;api=capability_module('note-project');target_id='local-note'
    target={'stage_id':m['stage_id'],'artifact_id':m['pdf']['artifact_id'],'root':str(folder),'subdir':'notes','path':'notes/'+m['pdf']['artifact_id']+'.md','policy':'local-markdown-v1'}
    grant=api.ProjectionGrant.from_dict({'principal':'interactive-user','targets':{target_id:target}});facade=api.ProjectionFacade(m['data_dir']);scope={'kind':'note_projection','stage_id':m['stage_id'],'artifact_id':m['pdf']['artifact_id'],'target_id':target_id}
    plan_path=folder/'export-plan.json'
    if reviewed_hash is None:
        if plan_path.exists():
            prior=json.loads(plan_path.read_text(encoding='utf-8'))
            return call(facade,grant,'note.project','status',scope,{'proposal_id':prior['proposal']['proposal_id']},m)
        context=call(facade,grant,'note.project','context',scope,{'mode':'update'},m)
        result=call(facade,grant,'note.project','propose',scope,{'mode':'update','expected_fingerprint':context['expected_fingerprint'],'expected_target_hash':context['expected_target_hash']},m)
        atomic_write_json(plan_path,result);return result
    plan=json.loads(safe(plan_path,file=True).read_text(encoding='utf-8'))
    if reviewed_hash!=plan['proposal_hash']:raise ValueError('Exact reviewed export hash required')
    return call(facade,grant,'note.project','decide',scope,{'proposal_id':plan['proposal']['proposal_id'],'decision':'approve','reviewed_hash':reviewed_hash},m)

def _plain(value,name,maximum):
    if not isinstance(value,str) or not value.strip() or value!=value.strip() or len(value)>maximum:
        raise ValueError('Invalid '+name)
    return value

def preview_project(data,*,project_name,title,goal,home=HOME):
    """Host preview only. The stage stays absent until commit_project sees the exact hash."""
    data=safe(data)
    project_name,title,goal=_plain(project_name,'project name',200),_plain(title,'title',200),_plain(goal,'goal',4000)
    stage_id='stage-'+uuid4().hex
    request_id='goal-'+uuid4().hex
    api=capability_module('project-write')
    grant={'principal':'interactive-user','create_stage_ids':[stage_id],'update_stage_ids':[],'paper_ids':[],
           'goal_requests':{stage_id:{'request_id':request_id,'goal':goal}}}
    authority=api.WriteGrant.from_dict(grant)
    facade=api.ProjectWriteFacade(data)
    scope={'kind':'project_stage','stage_id':stage_id}
    def invoke(action,params,key):
        request={'protocol_version':1,'request_id':'host-'+uuid4().hex,'task_id':request_id,'operation':'project.write.'+action,'scope':scope,'params':params,'idempotency_key':key}
        result=facade.execute(request,authority,operator=action=='decide')
        if not result['ok']:raise ValueError(json.dumps(result.get('error',result),ensure_ascii=False))
        return result['data']
    context=invoke('context',{'mode':'create'},request_id+'-context')
    if context.get('authorized_goal')!=goal or context.get('creation_basis')!='user_goal':
        raise ValueError('Host goal was not authorized')
    proposed=invoke('propose',{'mode':'create','stage_id':stage_id,'changes':{'project_name':project_name,'title':title,'goal':goal},'expected_fingerprint':context['expected_fingerprint']},request_id+'-propose')
    if ScholarFlowStore(data).get_project_stage(stage_id) is not None:
        raise ValueError('Project preview must not create the stage')
    home=safe(home,exists=False);pending=home/'pending-projects';pending.mkdir(parents=True,exist_ok=True)
    record={'version':1,'request_id':request_id,'stage_id':stage_id,'data_dir':str(Path(data).resolve()),'grant':grant,'proposal_id':proposed['proposal']['proposal_id'],'proposal_hash':proposed['proposal_hash'],'changes':proposed['proposal']['normalized_changes']}
    atomic_write_json(pending/(request_id+'.json'),record)
    return {'request_id':request_id,'stage_id':stage_id,'proposal_hash':record['proposal_hash'],'changes':record['changes'],'created':False}

def commit_project(data,request_id,reviewed_hash,home=HOME):
    data=safe(data)
    if not isinstance(request_id,str) or not re.fullmatch(r'goal-[a-f0-9]{32}',request_id):
        raise ValueError('Explicit host goal request required')
    if not isinstance(reviewed_hash,str) or not reviewed_hash.strip():
        raise ValueError('Exact reviewed project hash required')
    path=safe(home,exists=False)/'pending-projects'/(request_id+'.json')
    record=json.loads(safe(path,file=True).read_text(encoding='utf-8'))
    if record.get('version')!=1 or record.get('request_id')!=request_id or Path(record['data_dir'])!=Path(data).resolve():
        raise ValueError('Pending project does not belong to this data directory')
    if reviewed_hash!=record['proposal_hash']:
        raise ValueError('Exact reviewed project hash required')
    changes=record['changes']
    if changes.get('evidence_refs') or changes.get('goal')!=record['grant']['goal_requests'][record['stage_id']]['goal']:
        raise ValueError('Pending project is not a goal-only create')
    api=capability_module('project-write')
    authority=api.WriteGrant.from_dict(record['grant'])
    facade=api.ProjectWriteFacade(data)
    scope={'kind':'project_stage','stage_id':record['stage_id']}
    request={'protocol_version':1,'request_id':'host-'+uuid4().hex,'task_id':request_id,'operation':'project.write.decide','scope':scope,'params':{'proposal_id':record['proposal_id'],'decision':'approve','reviewed_hash':reviewed_hash},'idempotency_key':request_id+'-decide'}
    result=facade.execute(request,authority,operator=True)
    if not result['ok'] or result.get('status')!='committed':
        raise ValueError(json.dumps(result.get('error',result),ensure_ascii=False))
    stage=ScholarFlowStore(data).get_project_stage(record['stage_id'])
    if stage is None or stage.goal!=changes['goal'] or stage.evidence_refs:
        raise ValueError('Created project does not match the reviewed goal')
    return {'created':True,'stage_id':stage.stage_id,'project_name':stage.project_name,'title':stage.title,'goal':stage.goal,'evidence_refs':list(stage.evidence_refs),'creation_origin':stage.creation_origin}
