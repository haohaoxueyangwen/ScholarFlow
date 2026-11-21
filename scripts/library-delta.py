"""Explicit incremental discovery, reviewed application, durable derived-only jobs."""
import argparse,asyncio,json,secrets,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import library_knowledge as kb
from scripts.research_library import HOME,inventory,save
from scripts.run_library_support import contract_hash
from scholarflow.library_delta import build_delta
from scholarflow.library_projects import binding_plan,apply_binding,plan_hash
from scholarflow.knowledge_jobs import SyncJobStore
from scholarflow.persistence import file_lock
from scholarflow.storage import ScholarFlowStore
from scholarflow.mutations import record_fingerprint

def entries(path):return json.loads(path.read_text(encoding='utf-8'))['entries'] if path.exists() else {}
def plan(project):
    kb.validate_stage(project)
    if project.startswith('stage-library-'):raise ValueError('An actual research project is required')
    with file_lock(HOME/'index-execution',timeout_seconds=1):
        prior=kb.records();snapshot=inventory();catalog=snapshot['records'];groups={}
        configured=snapshot.get('source_mode')=='configured'
        if configured:
            from scholarflow.source_inventory import SourceInventory
            catalog=SourceInventory.for_project(catalog,project)
        for path in (HOME/'indexes').glob('stage-library-*/active-sources.json'):groups.update(entries(path))
        # Event-driven notes are absent from external file discovery by design.
        projected={k:v for k,v in entries(kb.state_path(project)).items() if not v.get('origin_artifact') and (v.get('inventory_mode')=='configured')==configured};delta=build_delta(catalog,groups,projected)
        catalog_by_id={r['source_id']:r for r in catalog};selected=[catalog_by_id[k] for k in delta['upsert']]
        if sum(r['bytes'] for r in selected)>15_000_000 or sum(len(kb.source_chunks(r)) for r in selected)>10000:raise ValueError('Delta exceeds frozen H026 total budget')
        stage=ScholarFlowStore(HOME/'facts').get_project_stage(project)
        frozen={'project':project,'project_fingerprint':record_fingerprint(stage.to_dict()),'delta':delta,'records':selected,'revoked_records':[projected[k] for k in delta['revoke']],'binding_hash':kb.binding_hash(project),'previous_inventory_hash':contract_hash(prior),'model_sha256':kb.EMBEDDING_MODEL_SHA256}
        job={'schema_version':1,'plan_id':'ksp_'+secrets.token_urlsafe(18),'source':'library-incremental-delta','created_at':time.time(),'expires_at':time.time()+86400,'status':'planned','attempts':0,'confirmed':False,'allow_deletes':False,'fingerprint':contract_hash(frozen),'plan':frozen,'results':[],'completed_sources':[],'embedding_requests':0}
        SyncJobStore(HOME/'facts').save(job);save(HOME/'projects/library-research/delta-plan.json',job)
    return {'plan_id':job['plan_id'],'fingerprint':job['fingerprint'],'upsert':len(delta['upsert']),'revoke':len(delta['revoke']),'physical_deletes':False}

async def apply(key,reviewed,operator):
    jobs=SyncJobStore(HOME/'facts');job=jobs.get(key);frozen=job['plan'];project=frozen['project']
    if not operator or reviewed!=job['fingerprint'] or contract_hash(frozen)!=reviewed:raise ValueError('Exact delta review required')
    if job['status']=='applied':return job['receipt']|{'idempotent_replay':True}
    if job['attempts']>=2 or job['expires_at']<time.time():raise ValueError('Expired/exhausted delta plan')
    if kb.binding_hash(project)!=job.get('expected_binding_hash',frozen['binding_hash']):raise ValueError('Bindings changed outside this job')
    stage=ScholarFlowStore(HOME/'facts').get_project_stage(project)
    if not stage or record_fingerprint(stage.to_dict())!=frozen['project_fingerprint']:raise ValueError('Project changed after delta review')
    current={r['source_id']:r for r in kb.records()}
    if any(k not in current or current[k]['sha256']!=v or not kb.current(current[k]) for k,v in frozen['delta']['versions'].items()):raise ValueError('Source inventory changed after delta review')
    with file_lock(HOME/'delta-runner',timeout_seconds=1):
        job.update(status='running',attempts=job['attempts']+1,confirmed=True);jobs.save(job)
        kb.EMBEDDING_REQUESTS=0;kb.EMBEDDING_LIMIT=max(0,10000-job['embedding_requests'])
        try:
            for record in frozen['records']:
                key_source=record['source_id']
                if key_source in job['completed_sources']:continue
                await kb.ingest([key_source],1000,expected_versions={key_source:record['sha256']})
                link=binding_plan(HOME/'facts',kb.records(),project,[key_source]);apply_binding(HOME/'facts',link,reviewed_hash=plan_hash(link),operator=True)
                result=await kb.ingest([key_source],1000,scope_id=project,expected_versions={key_source:record['sha256']})
                job['results'].extend(result['results']);job['completed_sources'].append(key_source);job['expected_binding_hash']=kb.binding_hash(project);job['inflight_embedding_requests']=kb.EMBEDDING_REQUESTS;jobs.save(job)
            if frozen['revoked_records'] and not job.get('revocations_applied'):
                link=binding_plan(HOME/'facts',frozen['revoked_records'],project,frozen['delta']['revoke'],revoke=True);apply_binding(HOME/'facts',link,reviewed_hash=plan_hash(link),operator=True)
                with file_lock(HOME/'index-execution',timeout_seconds=1):
                    path=kb.state_path(project);state=json.loads(path.read_text(encoding='utf-8'))
                    for source in frozen['delta']['revoke']:state['entries'][source]['revoked']=True
                    save(path,state)
                job['revocations_applied']=True;job['expected_binding_hash']=kb.binding_hash(project);jobs.save(job)
            job['embedding_requests']+=kb.EMBEDDING_REQUESTS;job['inflight_embedding_requests']=0;job['status']='applied';job['receipt']={'upsert':len(job['completed_sources']),'revoke':len(frozen['delta']['revoke']),'embedding_requests':job['embedding_requests'],'text_generation_calls':0,'physical_deletes':False,'finished_at':time.time()};jobs.save(job);return job['receipt']
        except Exception as error:
            job['embedding_requests']+=kb.EMBEDDING_REQUESTS;job.update(status='failed',error_type=type(error).__name__,error_code=str(error)[:300]);jobs.save(job);raise

def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['plan','apply']);parser.add_argument('--project',default='stage-scholarflow-library-research');parser.add_argument('--plan-id');parser.add_argument('--reviewed-hash');parser.add_argument('--operator',action='store_true');args=parser.parse_args()
    print(json.dumps(plan(args.project) if args.action=='plan' else asyncio.run(apply(args.plan_id,args.reviewed_hash,args.operator)),ensure_ascii=False))
if __name__=='__main__':main()
