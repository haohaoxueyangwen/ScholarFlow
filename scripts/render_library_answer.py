"""Trusted answer rendering: current evidence and scope, no model call."""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.library_knowledge import records,scoped_records,current,source_chunks,evidence_metadata
from scholarflow.library_answers import render_claims

def render(request):
    stage=request['stage_id'];allowed={r['source_id']:r for r in scoped_records(stage)};verified=[]
    for item in request['items']:
        record=allowed.get(item['source_id'])
        if not record or item['stage_id']!=stage or record['sha256']!=item['source_sha256'] or not current(record):raise ValueError('Evidence was revoked/changed before answer rendering')
        chunk=next((c for c in source_chunks(record) if c['file_path']==item['reference_id']),None)
        if not chunk or chunk['text']!=item['text'] or chunk['anchor']!=item['anchor']:raise ValueError('Evidence text/anchor not current')
        verified.append({**item,**evidence_metadata(record,chunk['anchor'],chunk['file_path'])})
    return render_claims(request['answer'],verified)

if __name__=='__main__':
    raw=sys.stdin.buffer.read(262145)
    if len(raw)>262144:raise ValueError('Bounded evidence request required')
    print(json.dumps(render(json.loads(raw.decode('utf-8'))),ensure_ascii=False))
