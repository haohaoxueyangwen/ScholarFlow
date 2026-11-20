"""Current-source observations for non-authoritative Pi-native checkpoints.

No checkpoint store, text model, domain write or session parser lives here.
"""
import hashlib,json,re
from pathlib import Path
from .domain_queries import DomainQueryError
from .mutations import record_fingerprint
from .knowledge_engine import KnowledgeSourceMap
from .storage import ScholarFlowStore

FIELDS={'principal','stage_id','workflow_id','objective','library_grant_file','source_ids'}
def _fail(message):raise DomainQueryError('permission_denied',message)
def load_grant(path):
    path=Path(path).absolute()
    if path.resolve()!=path or any(p.is_symlink() or getattr(p,'is_junction',lambda:False)() for p in (path,*path.parents)) or not path.is_file() or path.stat().st_nlink!=1 or path.stat().st_size>65536:_fail('Canonical bounded host continuity grant required')
    raw=path.read_bytes()
    try:value=json.loads(raw.decode('utf-8-sig'))
    except (ValueError,UnicodeError):_fail('Invalid continuity grant JSON')
    if not isinstance(value,dict) or set(value)!=FIELDS:_fail('Exact continuity grant fields required')
    if any(not isinstance(value[k],str) or not value[k].strip() or value[k]!=value[k].strip() for k in ('principal','stage_id','workflow_id','objective','library_grant_file')):_fail('Explicit host identity, objective and grant required')
    if len(value['principal'])>160 or len(value['objective'])>2000 or not re.fullmatch(r'stage-[A-Za-z0-9_-]{1,160}',value['stage_id']) or value['stage_id'].startswith('stage-library-') or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',value['workflow_id']):_fail('Exact research project/workflow required')
    ids=value['source_ids']
    if not isinstance(ids,list) or not 1<=len(ids)<=100 or any(not isinstance(k,str) or not re.fullmatch(r'source-[a-f0-9]{24}',k) for k in ids) or len(set(ids))!=len(ids):_fail('Bounded unique source IDs required')
    if not Path(value['library_grant_file']).is_absolute():_fail('Absolute host library grant path required')
    return value,raw

def observe(data_dir,grant_file,stage_id,source_ids,*,purpose,expected=None,control=None):
    from scripts import library_knowledge as kb
    from .pdf_notes import capability_module
    grant,raw=load_grant(grant_file);root=Path(data_dir).absolute()
    if root!=(kb.HOME/'facts').absolute() or root.resolve()!=root:_fail('Continuity must use its host library facts root')
    if stage_id!=grant['stage_id']:_fail('Project is not authorized for this workflow')
    if not isinstance(source_ids,list) or not 1<=len(source_ids)<=20 or any(not isinstance(k,str) or k not in grant['source_ids'] for k in source_ids) or len(set(source_ids))!=len(source_ids):_fail('Checkpoint sources exceed current host grant')
    if purpose not in {'checkpoint','resume'} or (purpose=='resume' and (not isinstance(expected,dict) or set(expected)!={'project_fingerprint','source_versions','membership_fingerprint','grant_sha256','library_grant_sha256'})) or (purpose=='checkpoint' and expected is not None):raise DomainQueryError('invalid_argument','Typed checkpoint/resume observation required')
    library=capability_module('library-read');read_grant,library_raw=library.load_grant(grant['library_grant_file'])
    if read_grant.principal!=grant['principal'] or stage_id not in read_grant.stage_ids:_fail('Current library scope revoked')
    project=ScholarFlowStore(root).get_project_stage(stage_id)
    if project is None:raise DomainQueryError('source_unavailable','Research project no longer exists')
    source_map=KnowledgeSourceMap(root);map_bytes=source_map.path.read_bytes() if source_map.path.exists() else None
    catalog={r['source_id']:r for r in kb.records()};mappings={s.metadata.get('library_source_id'):s for s in source_map.list_sources(project_stage_id=stage_id) if s.source_type=='library_source'}
    state_path=kb.state_path(stage_id);state_bytes=state_path.read_bytes() if state_path.exists() else None;state=json.loads(state_bytes)['entries'] if state_bytes else {}
    versions={};checks={};membership={}
    for key in source_ids:
        if control:control.check()
        record=catalog.get(key);mapping=mappings.get(key);entry=state.get(key)
        current=bool(record and mapping and mapping.content_hash==record['sha256'] and entry and not entry.get('revoked') and entry.get('index_schema')==kb.INDEX_SCHEMA and entry.get('sha256')==record['sha256'] and kb.current(record))
        checks[key]='current' if current else 'changed_revoked_unindexed_or_unbound';versions[key]=record['sha256'] if current else None;membership[key]=mapping.content_hash if mapping else None
    snapshot={'project_fingerprint':record_fingerprint(project.to_dict()),'source_versions':versions,'membership_fingerprint':hashlib.sha256(json.dumps(membership,sort_keys=True).encode()).hexdigest(),'grant_sha256':hashlib.sha256(raw).hexdigest(),'library_grant_sha256':hashlib.sha256(library_raw).hexdigest()}
    if load_grant(grant_file)[1]!=raw or library.load_grant(grant['library_grant_file'])[1]!=library_raw:_fail('Host authority changed during observation')
    latest_project=ScholarFlowStore(root).get_project_stage(stage_id)
    if not latest_project or record_fingerprint(latest_project.to_dict())!=snapshot['project_fingerprint'] or (source_map.path.read_bytes() if source_map.path.exists() else None)!=map_bytes or (state_path.read_bytes() if state_path.exists() else None)!=state_bytes:raise DomainQueryError('source_version_conflict','Project/source authority changed during observation')
    changed=[key for key,value in snapshot.items() if expected is not None and expected.get(key)!=value]
    fresh=all(v=='current' for v in checks.values()) and not changed
    if purpose=='checkpoint' and not fresh:raise DomainQueryError('source_version_conflict','Cannot pin stale/revoked evidence as a fresh checkpoint')
    return {'status':'fresh' if fresh else 'requires_revalidation','project_id':stage_id,'workflow_id':grant['workflow_id'],'objective':grant['objective'],'principal':grant['principal'],'authority_snapshot':snapshot,'source_checks':checks,'source_count':len(source_ids),'source_evidence_levels':{key:catalog[key].get('evidence_level') if key in catalog else None for key in source_ids},'metadata_provenance':'host_current_library_registry_not_note_frontmatter','changed_fields':changed,'research_facts_written':False,'text_generation_calls':0,'handoff_is_research_evidence':False}
