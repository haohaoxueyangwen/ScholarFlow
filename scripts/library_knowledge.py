"""Pinned LightRAG data adapter. No text-generation endpoint is ever invoked."""
import argparse,asyncio,hashlib,json,sys,urllib.request,os,secrets
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.research_library import HOME,sha,save
INDEX_SCHEMA='exact-utf8-source-v1'
EMBEDDING_REQUESTS=0
EMBEDDING_LIMIT=10000
EMBEDDING_MODEL_SHA256='06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439'

def facts_root():
    data=Path(os.environ.get('SCHOLARFLOW_LIBRARY_FACTS_DIR',str(HOME/'facts'))).absolute()
    if data.resolve()!=data or any(p.is_symlink() or getattr(p,'is_junction',lambda:False)() for p in (data,*data.parents)) or not (data==HOME/'facts' or HOME==data/'_research_knowledge'):
        raise ValueError('Library facts root is not bound to this index home')
    return data


def embedding_endpoint():
    config=HOME/'embedding-runtime.json'
    value=json.loads(config.read_text(encoding='utf-8')) if config.exists() else {}
    endpoint=value.get('endpoint','http://127.0.0.1:8081/v1/embeddings')
    if endpoint not in {'http://127.0.0.1:8081/v1/embeddings','http://127.0.0.1:8082/v1/embeddings'}:raise ValueError('Only reviewed loopback embedding endpoints are allowed')
    if endpoint.endswith(':8082/v1/embeddings') and (value.get('model_sha256')!=EMBEDDING_MODEL_SHA256 or value.get('parity_status')!='verified'):raise ValueError('GPU embedding qualification missing')
    return endpoint


def cache_path(text):
    key=hashlib.sha256((EMBEDDING_MODEL_SHA256+'\0'+text).encode()).hexdigest()
    return HOME/'embedding-cache'/EMBEDDING_MODEL_SHA256/key[:2]/(key+'.npy')


def cache_vector(text,vector):
    import numpy as np
    vector=np.asarray(vector,dtype=np.float32)
    if vector.shape!=(1024,) or not np.isfinite(vector).all():raise ValueError('Invalid cached embedding')
    path=cache_path(text);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.'+secrets.token_hex(6)+'.tmp')
    try:
        with temporary.open('wb') as output:np.save(output,vector,allow_pickle=False)
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


async def forbidden_llm(*args,**kwargs):
    raise RuntimeError('LightRAG text generation/extraction disabled: use the Pi server-model session')


async def embedding(texts,**kwargs):
    global EMBEDDING_REQUESTS
    import numpy as np
    def request():
        global EMBEDDING_REQUESTS
        endpoint=embedding_endpoint();found={}
        for index,text in enumerate(texts):
            path=cache_path(text)
            if path.exists():
                value=np.load(path,allow_pickle=False)
                if value.shape!=(1024,) or not np.isfinite(value).all():raise ValueError('Invalid cached embedding')
                found[index]=value
        missing=[index for index in range(len(texts)) if index not in found]
        if not missing:return np.asarray([found[i] for i in range(len(texts))],dtype=np.float32)
        EMBEDDING_REQUESTS+=1
        if EMBEDDING_REQUESTS>EMBEDDING_LIMIT:raise RuntimeError('Frozen embedding request budget exceeded')
        payload={'model':'qwen3-embedding-0.6b','input':[texts[i] for i in missing],'encoding_format':'float'}
        req=urllib.request.Request(endpoint,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=300) as response:data=json.load(response)
        rows=sorted(data['data'],key=lambda r:r['index'])
        if [r['index'] for r in rows]!=list(range(len(missing))):raise ValueError('Embedding row identity mismatch')
        result=np.asarray([r['embedding'] for r in rows],dtype=np.float32)
        if result.shape!=(len(missing),1024) or not np.isfinite(result).all():raise ValueError('Embedding protocol/dimension mismatch')
        for index,value in zip(missing,result):cache_vector(texts[index],value);found[index]=value
        return np.asarray([found[i] for i in range(len(texts))],dtype=np.float32)
    return await asyncio.to_thread(request)


def records():
    result=[]
    for name in ('inventory.json','managed-sources.json'):
        path=HOME/name
        if path.exists():result.extend(json.loads(path.read_text(encoding='utf-8'))['records'])
    if len({r['source_id'] for r in result})!=len(result):raise ValueError('Duplicate library source identity')
    return result
def scope_root(scope):return HOME/'indexes'/scope if scope.startswith('stage-library-') else HOME/'project-indexes'/hashlib.sha256(scope.encode()).hexdigest()[:32]
def state_path(stage):return scope_root(stage)/'active-sources.json'
def workspace_for(scope):return 'source_library' if scope=='stage-library-example_vault-notes' else 'sf_'+hashlib.sha256(scope.encode()).hexdigest()[:24]
def validate_stage(stage):
    import re
    if not isinstance(stage,str) or not re.fullmatch(r'stage-[A-Za-z0-9_-]{1,160}',stage):raise ValueError('Unknown library scope')
    if stage.startswith('stage-library-'):
        if stage not in {r['stage_id'] for r in records()}:raise ValueError('Unknown library source group')
    else:
        from scholarflow.storage import ScholarFlowStore
        if ScholarFlowStore(facts_root()).get_project_stage(stage) is None:raise ValueError('Unknown research project')


def scoped_records(scope):
    catalog=records()
    if scope.startswith('stage-library-'):return [r for r in catalog if r['stage_id']==scope]
    from scholarflow.knowledge_engine import KnowledgeSourceMap
    mappings=KnowledgeSourceMap(facts_root()).list_sources(project_stage_id=scope)
    allowed={s.metadata.get('library_source_id'):s for s in mappings if s.source_type=='library_source'}
    return [r for r in catalog if r['source_id'] in allowed and allowed[r['source_id']].content_hash==r['sha256']]


def binding_hash(scope):
    path=facts_root()/'_knowledge/source-map.json'
    return sha(path.read_bytes()) if not scope.startswith('stage-library-') and path.exists() else None


def sync_source_metadata(record):
    from scholarflow.knowledge_engine import KnowledgeSource,KnowledgeSourceMap
    from scholarflow.knowledge_ingestion import KnowledgeIngestionManifest
    from scholarflow.domain_queries import DomainQueryError
    source_map=KnowledgeSourceMap(facts_root());path='library/'+record['source_id']+'.md'
    try:prior=source_map.resolve(path);metadata=dict(prior.metadata)
    except DomainQueryError:metadata={}
    metadata.update(library_source_id=record['source_id'],source_group=record['group'],evidence_level=record['evidence_level'])
    title=record.get('title') or Path(record['path']).name
    source_map.register(KnowledgeSource(engine_file_path=path,source_ref='library:'+record['source_id'],source_type='library_source',title=title,content_hash=record['sha256'],source_version=INDEX_SCHEMA,metadata=metadata))
    manifest=KnowledgeIngestionManifest(facts_root());manifest.set('library:'+record['source_id'],{'source_ref':'library:'+record['source_id'],'source_type':'library_source','title':title,'scope':'library:'+record['group'],'source_version':INDEX_SCHEMA,'content_hash':record['sha256'],'status':'indexed','document_id':record['source_id'],'metadata':metadata})
def current(record):
    path=Path(record['path'])
    if not path.is_file() or path.is_symlink() or getattr(path,'is_junction',lambda:False)() or path.stat().st_nlink!=1 or path.stat().st_size>(80_000_000 if record['kind']=='pdf' else 2_000_000):return False
    if sha(path.read_bytes())!=record['sha256']:return False
    if record.get('origin_artifact'):
        from scholarflow.workspace_knowledge import managed_current
        if not managed_current(record,HOME):return False
    origin=record.get('origin_evidence')
    if origin:
        original=next((r for r in records() if r['source_id']==origin.get('original_source_id')),None)
        if not original or original['kind']!='pdf' or original.get('origin_evidence') or original['sha256']!=origin.get('original_pdf_hash') or not current(original):return False
    return True


async def rag_for(stage):
    # Keep the deterministic tokenizer vocabulary local across Windows users
    # and restarts; no model API is used to obtain this static dependency.
    os.environ.setdefault('TIKTOKEN_CACHE_DIR',str(ROOT/'.tmp/lightrag-native/tokenizer-cache'))
    from lightrag import LightRAG
    from lightrag.utils import EmbeddingFunc
    validate_stage(stage)
    rag=LightRAG(working_dir=str(scope_root(stage)/'storage'),workspace=workspace_for(stage),
        llm_model_func=forbidden_llm,embedding_func=EmbeddingFunc(embedding_dim=1024,max_token_size=2048,model_name='qwen3-embedding-0.6b-local-cpu',func=embedding),
        embedding_batch_num=4,embedding_func_max_async=1,vector_db_storage_cls_kwargs={'cosine_better_than_threshold':0.2})
    await rag.initialize_storages();return rag


def source_chunks(record):
    if not current(record):raise ValueError('Source changed; refresh inventory before indexing')
    if record['kind']=='pdf':
        from scripts.research_library import stage
        staged=stage(record['source_id']);blocks=[b for b in staged['structure']['blocks'] if b.get('page_start') and b.get('page_end') and b.get('text','').strip()]
        pieces=[(b['text'],{'block_id':b['block_id'],'page_start':b['page_start'],'page_end':b['page_end'],'parser':staged['structure']['parser'],'structure_hash':staged['structure_hash']}) for b in blocks]
    else:pieces=[(Path(record['path']).read_bytes().decode('utf-8-sig'),{'note_file':Path(record['path']).name})]
    chunks=[]
    for text,anchor in pieces:
        for start in range(0,len(text),1200):
            fragment=text[start:start+1200]
            if not fragment.strip():continue
            ref='sf:'+record['source_id']+':'+record['sha256'].removeprefix('sha256:')+':'+str(len(chunks))
            content='[source '+ref+']\n'+fragment
            chunks.append({'content':content,'source_id':ref,'file_path':ref,'anchor':{**anchor,'char_start':start,'char_end':start+len(fragment)},'text':fragment})
    return chunks


async def ingest(source_ids,max_chunks=1000,scope_id=None,expected_versions=None,progress=None):
    from scholarflow.persistence import file_lock
    with file_lock(HOME/'index-execution',timeout_seconds=1):
        return await _ingest(source_ids,max_chunks,scope_id,expected_versions,progress)


async def _ingest(source_ids,max_chunks,scope_id,expected_versions,progress):
    from lightrag.utils import compute_mdhash_id,sanitize_text_for_encoding
    selected=[r for r in records() if r['source_id'] in source_ids]
    if len(selected)!=len(set(source_ids)) or not 1<=len(selected)<=50:raise ValueError('Bounded exact source IDs required')
    if type(max_chunks)is not int or not 1<=max_chunks<=1000:raise ValueError('Chunk budget must be 1..1000')
    if expected_versions is not None and expected_versions!={r['source_id']:r['sha256'] for r in selected}:raise ValueError('Frozen source versions changed')
    if scope_id:
        validate_stage(scope_id)
        if set(source_ids)-{r['source_id'] for r in scoped_records(scope_id)}:raise ValueError('Source is not explicitly bound to this project')
    result=[];consumed=0;rags={}
    try:
      for record in selected:
        target=scope_id or record['stage_id'];path=state_path(target);state=json.loads(path.read_text()) if path.exists() else {'entries':{}}
        old=state['entries'].get(record['source_id'])
        if target not in rags:rags[target]=await rag_for(target)
        rag=rags[target]
        if old and not old.get('revoked') and old.get('index_schema')==INDEX_SCHEMA and old['sha256']==record['sha256'] and current(record):
            ids=[v['chunk_id'] for v in old['chunks'].values()];stored=await rag.text_chunks.get_by_ids(ids);vectors=await rag.chunks_vdb.get_by_ids(ids)
            if len(stored)==len(ids) and len(vectors)==len(ids) and all(vectors) and all(row and sha(row['content'].encode())==meta['index_content_hash'] for row,meta in zip(stored,old['chunks'].values())):
                sync_source_metadata(record);item={'source_id':record['source_id'],'status':'unchanged'};result.append(item)
                if progress:progress(item)
                continue
        chunks=source_chunks(record)
        if not chunks or consumed+len(chunks)>max_chunks:raise ValueError('Batch chunk budget/empty text; split scope explicitly')
        consumed+=len(chunks)
        # LightRAG canonicalizes HTML entities/whitespace. Keep that index
        # representation separate from exact source text and character anchors.
        # Feed raw content to the backend, which sanitizes exactly once.
        # html.unescape is not idempotent for nested entities such as &amp;lt;.
        for c in chunks:c['index_content']=sanitize_text_for_encoding(c['content'])
        try:
            await rag.ainsert_custom_kg({'chunks':[{k:c[k] for k in ('content','source_id','file_path')} for c in chunks],'entities':[],'relationships':[]},full_doc_id=record['source_id']+'@'+record['sha256'])
            ids=[compute_mdhash_id(c['index_content'],prefix='chunk-') for c in chunks]
            stored=await rag.text_chunks.get_by_ids(ids)
            vectors=await rag.chunks_vdb.get_by_ids(ids)
            if len(vectors)!=len(ids) or not all(vectors) or len(stored)!=len(chunks) or any(not row or row.get('content')!=chunk['index_content'] for row,chunk in zip(stored,chunks)):raise RuntimeError('LightRAG insertion postcondition failed; not marking complete')
            if not current(record):raise ValueError('Source changed during ingestion; active version unchanged')
            state['entries'][record['source_id']]={**record,'index_schema':INDEX_SCHEMA,'chunks':{c['file_path']:{'chunk_id':i,'anchor':c['anchor'],'text_hash':sha(c['text'].encode()),'index_content_hash':sha(c['index_content'].encode())} for i,c in zip(ids,chunks)},'index_backend':'LightRAG 1.5.4','normalization':'backend sanitize once; exact source retained separately','text_generation_calls':0}
            save(path,state);result.append({'source_id':record['source_id'],'status':'indexed','chunks':len(chunks)})
            sync_source_metadata(record)
            if progress:progress(result[-1])
        except Exception:raise
    finally:
        for rag in rags.values():await rag.finalize_storages()
    return {'results':result,'chunks_processed':consumed,'text_generation_calls':0}


async def query(stage,question,limit=5):
    from lightrag import QueryParam
    validate_stage(stage)
    if not isinstance(question,str) or not question.strip() or len(question)>2000 or type(limit) is not int or not 1<=limit<=10:raise ValueError('Bounded query required')
    path=state_path(stage)
    if not path.exists():return {'items':[],'status':'not_indexed','text_generation_calls':0}
    if not stage.startswith('stage-library-') and not scoped_records(stage):return {'items':[],'status':'no_current_evidence','text_generation_calls':0}
    authority=binding_hash(stage);state=json.loads(path.read_text());rag=await rag_for(stage)
    try:
        result=await rag.aquery_data(question,QueryParam(mode='naive',top_k=50,chunk_top_k=50,max_total_tokens=8000,enable_rerank=False))
        chunks=result.get('data',{}).get('chunks',[]);items=[];active={r['source_id']:r for r in scoped_records(stage)};originals={}
        for chunk in chunks:
            ref=chunk.get('file_path','')
            for source_id,entry in state['entries'].items():
                record=active.get(source_id)
                if entry.get('revoked') or entry.get('index_schema')!=INDEX_SCHEMA or ref not in entry['chunks'] or not record or record['sha256']!=entry['sha256'] or not current(record):continue
                anchor=entry['chunks'][ref];content=chunk['content']
                if sha(content.encode())!=anchor['index_content_hash']:raise ValueError('Retrieved index chunk hash mismatch')
                if source_id not in originals:originals[source_id]={c['file_path']:c for c in source_chunks(record)}
                original=originals[source_id].get(ref)
                if not original or sha(original['text'].encode())!=anchor['text_hash'] or original['anchor']!=anchor['anchor']:raise ValueError('Original source anchor mismatch')
                items.append({'source_id':source_id,'stage_id':stage,'source_sha256':record['sha256'],'title':record.get('title') or Path(record['path']).name,'evidence_level':record['evidence_level'],'origin_evidence':record.get('origin_evidence'),'origin_artifact':record.get('origin_artifact'),'anchor':anchor['anchor'],'text':original['text'],'reference_id':ref,'text_representation':'exact current source; index normalization not quoted',**evidence_metadata(record,anchor['anchor'],ref)})
                break
            if len(items)>=limit:break
        if binding_hash(stage)!=authority:raise ValueError('Project source authority changed during retrieval')
        return {'status':'ok' if items else 'no_current_evidence','items':items,'backend':'LightRAG 1.5.4 semantic vector retrieval','text_generation_calls':0}
    finally:await rag.finalize_storages()


def evidence_metadata(record,anchor,reference):
    origin=record.get('origin_evidence')
    known_model=origin.get('model') if isinstance(origin,dict) and record.get('evidence_level')=='derived_pdf_fragment_note' else None
    return {'citation_id':'c-'+hashlib.sha256(reference.encode()).hexdigest()[:16],'authorship':{'kind':'recorded_model_derivation' if known_model else 'unknown','model':known_model},'citation':{'source_id':record['source_id'],'reference_id':reference,'anchor':anchor},'generation_boundary':'0 generation calls applies only to retrieval; existing-note authorship is unknown unless recorded; current Pi answer is model-generated'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['ingest','query']);parser.add_argument('--source-id',action='append');parser.add_argument('--stage-id');parser.add_argument('--query');parser.add_argument('--limit',type=int,default=5);parser.add_argument('--max-chunks',type=int,default=1000);args=parser.parse_args()
    result=asyncio.run(ingest(args.source_id,args.max_chunks) if args.action=='ingest' else query(args.stage_id,args.query,args.limit));print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
