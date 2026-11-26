"""Host-authorized derived indexing of a reviewed workspace note, not an agent."""
import asyncio
import hashlib
import json
import time
from pathlib import Path

from . import research_workspace as rw
from .knowledge_jobs import SyncJobStore
from .mutations import record_fingerprint
from .persistence import atomic_write_json, atomic_write_text, file_lock
from .source_notes import source_version_state
from .storage import ScholarFlowStore


def home_for(workspace):
    data = rw.safe(workspace['data_dir'])
    return rw.library_home(data)


def note_for(path):
    m = rw.load(path)
    if not m['pdf']:
        raise ValueError('No approved PDF note selected')
    note = ScholarFlowStore(m['data_dir']).get_research_artifact(m['pdf']['artifact_id'])
    if not note or note.project_stage_id != m['stage_id'] or note.status == 'archived':
        raise ValueError('Saved current-project note required')
    record = note.to_dict()
    if record['source_snapshot'].get('version') != 2 or not source_version_state(ScholarFlowStore(m['data_dir']), record['source_snapshot'])['matches_current']:
        raise ValueError('Note PDF provenance is no longer current')
    committed = [p for p in rw.proposals(path) if p['phase'] == 'committed']
    if not any(all(record.get(k) == v for k, v in p['proposal']['normalized_changes'].items()) for p in committed):
        raise ValueError('Exact saved note has no committed operator approval')
    return m, record


def render(record, *, version=1):
    if type(version) is not int or version not in (1, 2):
        raise ValueError('Unknown managed-note rendering version')
    body = '# ' + record['title'] + '\n\n' + record['content'] + '\n\nDerived note; not full-paper evidence.\n'
    if version == 1:
        anchors = [{'source': s['source'], 'anchor': s['anchor']} for s in record['source_snapshot']['sources']]
        return body + 'PDF anchors: ' + json.dumps(anchors, ensure_ascii=False, separators=(',', ':')) + '\n'
    # V2 retains PDF anchors in origin_artifact metadata, outside indexed text.
    return body


def enqueue(path):
    path = rw.safe(path, file=True)
    m, record = note_for(path)
    home = home_for(m)
    home.mkdir(parents=True,exist_ok=True)
    fp = record_fingerprint(record)
    source_id = 'source-' + hashlib.sha256((m['stage_id']+'\0'+record['artifact_id']).encode()).hexdigest()[:24]
    key = 'ksp_' + hashlib.sha256((str(path)+'\0'+fp).encode()).hexdigest()[:24]
    jobs = SyncJobStore(Path(m['data_dir']))
    with file_lock(home/'managed-note-queue', timeout_seconds=1):
        if jobs.path(key).exists():
            return jobs.get(key)
        job = {'schema_version': 1, 'plan_id': key, 'source': 'reviewed-workspace-note', 'created_at': time.time(),
               'status': 'queued', 'attempts': 0, 'confirmed': True, 'allow_deletes': False,
               'fingerprint': fp, 'workspace': str(path), 'stage_id': m['stage_id'], 'artifact_id': record['artifact_id'],
               'source_id': source_id, 'results': [], 'embedding_requests': 0}
        jobs.save(job)
    return job


def managed_current(record, home):
    """Revalidate authoritative note, original PDF and current host grants."""
    try:
        origin = record['origin_artifact']
        m, note = note_for(origin['workspace'])
        expected = Path(home)/'managed-sources'/record['source_id']/(origin['fingerprint'].removeprefix('sha256:')+'.md')
        return (home_for(m) == Path(home) and rw.safe(record['path'], file=True) == expected
                and note['artifact_id'] == origin['artifact_id'] and m['stage_id'] == record['stage_id'] == origin['project_stage_id']
                and record_fingerprint(note) == origin['fingerprint'] and rw.sha(render(note, version=origin.get('render_version', 1)).encode()) == record['sha256'])
    except (ValueError, OSError, KeyError, TypeError):
        return False


def status(path):
    m = rw.load(path)
    jobs = [j for j in SyncJobStore(Path(m['data_dir'])).records()
            if j.get('source') == 'reviewed-workspace-note' and j.get('workspace') == str(Path(path).absolute())]
    if not jobs:
        return {'status': 'not_queued'}
    job = jobs[0]
    if job['status'] == 'indexed':
        try:
            _, note = note_for(path)
            if record_fingerprint(note) != job['fingerprint']:
                return {**job, 'status': 'stale'}
            state_path=home_for(m)/'project-indexes'/hashlib.sha256(m['stage_id'].encode()).hexdigest()[:32]/'active-sources.json'
            state=json.loads(state_path.read_text(encoding='utf-8'))
            entry=state.get('entries',{}).get(job['source_id'])
            if not entry or entry.get('revoked') or not entry.get('chunks') or not managed_current(entry,home_for(m)):
                return {**job, 'status': 'stale'}
        except (ValueError, OSError, KeyError, TypeError):
            return {**job, 'status': 'stale'}
    return job


async def process(path):
    from scripts import library_knowledge as kb
    from .library_projects import binding_plan, apply_binding, plan_hash
    job = enqueue(path)
    m = rw.load(path)
    home = home_for(m)
    if kb.HOME != home or kb.facts_root() != Path(m['data_dir']):
        raise ValueError('Worker library home differs from selected workspace')
    jobs = SyncJobStore(Path(m['data_dir']))
    with file_lock(home/'managed-note-worker', timeout_seconds=1):
        job = jobs.get(job['plan_id'])
        if job['attempts'] >= 3 and job['status'] != 'indexed':
            raise ValueError('Three indexing attempts exhausted; host review required')
        job.update(status='running', attempts=job['attempts']+1, error_code=None, error_type=None)
        jobs.save(job)
        kb.EMBEDDING_REQUESTS = 0
        kb.EMBEDDING_LIMIT = max(0, 64-job['embedding_requests'])
        try:
            m, note = note_for(path)
            if record_fingerprint(note) != job['fingerprint']:
                raise ValueError('Note changed after enqueue')
            cache = home/'managed-sources'/job['source_id']/(job['fingerprint'].removeprefix('sha256:')+'.md')
            if cache.exists():rw.safe(cache,file=True)
            else:rw.safe(cache,exists=False)
            if cache.exists():
                content = cache.read_text(encoding='utf-8')
                if content == render(note, version=2): render_version = 2
                elif content == render(note, version=1): render_version = 1
                else: raise ValueError('Managed source cache was edited; do not overwrite')
            else:
                render_version = 2
                content = render(note, version=render_version)
            if len(content.encode()) > 76800:
                raise ValueError('Note exceeds the 64-chunk indexing budget')
            cache.parent.mkdir(parents=True, exist_ok=True)
            if not cache.exists():
                atomic_write_text(cache, content)
            record = {'source_id': job['source_id'], 'path': str(cache), 'title': note['title'], 'kind': 'md', 'group': 'saved-research-notes',
                      'stage_id': m['stage_id'], 'sha256': rw.sha(content.encode()), 'bytes': len(content.encode()),
                      'evidence_level': 'derived_pdf_fragment_note',
                      'origin_artifact': {'artifact_id': note['artifact_id'], 'project_stage_id': m['stage_id'],
                                          'fingerprint': job['fingerprint'], 'workspace': str(Path(path).absolute()), 'render_version': render_version,
                                          'pdf_sources': [{'import_id': s['source']['import_id'], 'anchor': s['anchor']}
                                                          for s in note['source_snapshot']['sources']]}}
            if not managed_current(record, home):
                raise ValueError('Managed source freshness check failed')
            catalog_path = home/'managed-sources.json'
            with file_lock(home/'index-execution', timeout_seconds=1):
                catalog = json.loads(catalog_path.read_text(encoding='utf-8')) if catalog_path.exists() else {'records': []}
                catalog['records'] = [r for r in catalog['records'] if r['source_id'] != record['source_id']] + [record]
                atomic_write_json(catalog_path, catalog)
            link = binding_plan(Path(m['data_dir']), kb.records(), m['stage_id'], [record['source_id']])
            apply_binding(Path(m['data_dir']), link, reviewed_hash=plan_hash(link), operator=True, lock_root=home)
            result = await kb.ingest([record['source_id']], 64, scope_id=m['stage_id'], expected_versions={record['source_id']: record['sha256']})
            if not managed_current(record, home):
                raise ValueError('Note changed before completion')
            job.update(status='indexed', results=result['results'], finished_at=time.time(), text_generation_calls=0)
        except Exception as error:
            job.update(status='failed', error_code=str(error)[:300], error_type=type(error).__name__)
            raise
        finally:
            job['embedding_requests'] += kb.EMBEDDING_REQUESTS
            jobs.save(job)
    return job
