"""Deterministic library inventory/staging; models are invoked only through Pi."""
import argparse
from datetime import datetime,timezone
import hashlib,json,os,re,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
HOME=Path(os.environ.get('SCHOLARFLOW_LIBRARY_HOME',str(ROOT/'.local/research-library'))).absolute()
if HOME.resolve()!=HOME or any(p.is_symlink() or getattr(p,'is_junction',lambda:False)() for p in (HOME,*HOME.parents)):
    raise ValueError('Canonical host-owned library home required')
VAULT=Path(os.environ.get('SCHOLARFLOW_VAULT_DIR',str(HOME/'unconfigured-vault')))
SOURCES=[]  # Configure and review source roots explicitly.

def sha(raw):return 'sha256:'+hashlib.sha256(raw).hexdigest()
def save(path,value):
    from scholarflow.persistence import atomic_write_text
    path.parent.mkdir(parents=True,exist_ok=True);atomic_write_text(path,json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def safe_files(root,*,prune_code=False):
    if not root.is_dir():return
    for directory,dirs,files in os.walk(root,followlinks=False):
        if prune_code and any(name in files or name in dirs for name in ('.git','pyproject.toml','package.json','CMakeLists.txt','Cargo.toml','SKILL.md')):
            dirs[:]=[];continue
        dirs[:]=[d for d in dirs if not d.startswith('.') and not (Path(directory)/d).is_symlink() and not getattr(Path(directory)/d,'is_junction',lambda:False)()]
        if prune_code:dirs[:]=[d for d in dirs if d.casefold() not in {'results','outputs','checkpoints','weights','node_modules','__pycache__','venv','benchmark'}]
        for name in sorted(files):
            p=Path(directory)/name
            if name.startswith('.') or p.is_symlink() or getattr(p,'is_junction',lambda:False)() or p.stat().st_nlink!=1:continue
            yield p
def sensitive(text):
    # Quarantine plausible credentials, never print them or silently index redactions.
    return bool(re.search(r'\bsk-[A-Za-z0-9_-]{20,}|(?i:Bearer)\s+[A-Za-z0-9_.-]{24,}|(?i:api[_-]?key|password|client_secret)\s*[:=]\s*[\"\']?[A-Za-z0-9_-]{20,}',text))
def inventory():
    raise ValueError('Source inventory is unavailable in this public preview')
def stage(source_id):
    from scholarflow import ScholarFlowStore,ProjectStage
    from scholarflow.reader_workspace import ReaderWorkspaceStore
    manifest=json.loads((HOME/'inventory.json').read_text(encoding='utf-8'));record=next(r for r in manifest['records'] if r['source_id']==source_id)
    raw=Path(record['path']).read_bytes()
    if sha(raw)!=record['sha256']:raise ValueError('Source changed since inventory')
    data=HOME/'facts';store=ScholarFlowStore(data)
    if store.get_project_stage(record['stage_id']) is None:store.create_project_stage(ProjectStage(stage_id=record['stage_id'],project_name=record['group'],title='Research library',goal='Source-linked reading and two-layer knowledge'))
    if record['kind']!='pdf':return {'record':record,'text':raw.decode('utf-8-sig')}
    os.environ['SCHOLARFLOW_PDF_PARSER']='pymupdf-blocks';os.environ['SCHOLARFLOW_GROBID_BASE_URL']='';os.environ['SCHOLARFLOW_TESSERACT_EXE']=''
    imported=ReaderWorkspaceStore(data).import_pdf(Path(record['path']).name,raw,project_stage_ids=[record['stage_id']])
    if imported['status']!='parsed':raise ValueError('No parsed text: '+imported.get('parse_error','unavailable'))
    directory=data/'_reader/imports'/imported['import_id'];structure=json.loads((directory/'structured.json').read_text(encoding='utf-8'))
    return {'record':record,'imported':imported,'structure':structure,'structure_hash':sha((directory/'structured.json').read_bytes())}
def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['inventory','stage']);parser.add_argument('--source-id');args=parser.parse_args()
    if args.action=='inventory':
        from collections import Counter
        result=inventory();print(json.dumps({'sources':len(result['records']),'groups':dict(Counter(r['group'] for r in result['records'])),'reading':dict(Counter(r['reading_state'] for r in result['records'])),'excluded':len(result['excluded'])},ensure_ascii=False))
    else:print(json.dumps(stage(args.source_id),ensure_ascii=False))
if __name__=='__main__':main()
