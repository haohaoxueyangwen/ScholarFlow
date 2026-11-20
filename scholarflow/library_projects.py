"""Host-reviewed library-to-ProjectStage links over the existing SourceMap."""
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
from .domain_queries import DomainQueryError
from .knowledge_engine import KnowledgeSource,KnowledgeSourceMap
from .mutations import record_fingerprint
from .persistence import file_lock
from .storage import ScholarFlowStore

def plan_hash(plan):return sha256(json.dumps(plan,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def binding_plan(data_dir,catalog,project_id,source_ids,*,revoke=False):
    store=ScholarFlowStore(data_dir);project=store.get_project_stage(project_id)
    if not project or project_id.startswith('stage-library-'):raise DomainQueryError('invalid_argument','An actual research ProjectStage is required, not a library group')
    if not isinstance(source_ids,list) or not 1<=len(source_ids)<=5000 or len(set(source_ids))!=len(source_ids):raise DomainQueryError('invalid_argument','Exact bounded unique source IDs required')
    records={r['source_id']:r for r in catalog};source_map=KnowledgeSourceMap(data_dir);existing={s.engine_file_path:s for s in source_map.list_sources()};updates=[];versions={}
    for key in source_ids:
        if key not in records:raise DomainQueryError('not_found','Source not in approved inventory')
        record=records[key];path='library/'+key+'.md';prior=existing.get(path);metadata=dict(prior.metadata) if prior else {}
        projects=set(metadata.get('project_stage_ids',[]))
        if revoke:projects.discard(project_id)
        else:projects.add(project_id)
        metadata.update(project_stage_ids=sorted(projects),library_source_id=key,source_group=record['group'],evidence_level=record['evidence_level'])
        updates.append(KnowledgeSource(path,'library:'+key,'library_source',record.get('title') or Path(record['path']).name,record['sha256'],'exact-utf8-source-v1',metadata,datetime.now(timezone.utc).isoformat()).to_dict());versions[key]=record['sha256']
    return {'schema_version':1,'operation':'revoke_project_sources' if revoke else 'bind_project_sources','project_id':project_id,'project_fingerprint':record_fingerprint(project.to_dict()),'source_versions':versions,'before_map_sha256':sha256(source_map.path.read_bytes()).hexdigest() if source_map.path.exists() else None,'updates':updates}

def apply_binding(data_dir,plan,*,reviewed_hash,operator=False,lock_root=None):
    if not operator or reviewed_hash!=plan_hash(plan):raise DomainQueryError('confirmation_required','Trusted operator review of exact source-link plan required')
    with file_lock(Path(lock_root or Path(data_dir).parent)/'index-execution',timeout_seconds=1):
        store=ScholarFlowStore(data_dir);project=store.get_project_stage(plan['project_id'])
        if not project or record_fingerprint(project.to_dict())!=plan['project_fingerprint']:raise DomainQueryError('version_conflict','Research project changed after review')
        return KnowledgeSourceMap(data_dir).update_sources([KnowledgeSource(**s) for s in plan['updates']],expected_sha256=plan['before_map_sha256'])
