import asyncio,importlib.util,json,os,unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock
import test_research_workspace as fixtures
from scholarflow import research_workspace as rw,workspace_knowledge as wk
from scholarflow.persistence import atomic_write_json
from scholarflow.knowledge_jobs import SyncJobStore
from scholarflow.library_projects import binding_plan,apply_binding,plan_hash
from scripts import library_knowledge as kb

class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.WorkspaceTests();self.fixture.setUp();self.root=self.fixture.root;self.data=self.fixture.data
        self.path=self.fixture.prepare();self.proposal=self.fixture.proposal(self.path)
    def tearDown(self):self.fixture.tearDown()
    def save(self):return rw.decide(self.path,self.proposal['proposal']['proposal_id'],self.proposal['proposal_hash'])
    def test_pending_not_indexable(self):
        with self.assertRaisesRegex(ValueError,'Saved current-project'):wk.enqueue(self.path)
        self.assertEqual(wk.status(self.path)['status'],'not_queued')
    def test_save_queues_exact_note_and_replay_is_same_job(self):
        saved=self.save();self.assertEqual(saved['knowledge']['status'],'queued')
        a=wk.enqueue(self.path);b=wk.enqueue(self.path)
        self.assertEqual(a['plan_id'],b['plan_id']);self.assertEqual(len(SyncJobStore(self.data).records()),1)
        self.assertFalse((self.root/'managed-sources.json').exists())
    def test_failed_index_does_not_rollback_note_and_retry_works(self):
        self.save()
        with patch.object(kb,'HOME',self.root),patch.object(kb,'ingest',new=AsyncMock(side_effect=RuntimeError('Embedding offline'))):
            with self.assertRaisesRegex(RuntimeError,'offline'):asyncio.run(wk.process(self.path))
        self.assertEqual(wk.status(self.path)['status'],'failed');self.assertEqual(rw.proposals(self.path)[0]['phase'],'committed')
        with patch.object(kb,'HOME',self.root),patch.object(kb,'ingest',new=AsyncMock(return_value={'results':[{'status':'indexed'}]})):
            result=asyncio.run(wk.process(self.path))
        self.assertEqual(result['status'],'indexed');self.assertEqual(result['attempts'],2)
    def test_managed_source_exact_project_freshness_and_archive(self):
        self.save()
        with patch.object(kb,'HOME',self.root),patch.object(kb,'ingest',new=AsyncMock(return_value={'results':[]})):
            asyncio.run(wk.process(self.path));records=kb.records();r=records[0]
            self.assertTrue(kb.current(r));self.assertEqual(len(kb.scoped_records('stage-A')),1);self.assertEqual(kb.scoped_records('stage-a'),[])
            r['origin_artifact']['project_stage_id']='stage-a';self.assertFalse(kb.current(r))
    def test_new_note_index_omits_anchor_json_but_keeps_structured_provenance(self):
        self.save()
        with patch.object(kb,'HOME',self.root),patch.object(kb,'ingest',new=AsyncMock(return_value={'results':[]})):
            asyncio.run(wk.process(self.path));r=kb.records()[0]
            self.assertEqual(r['origin_artifact']['render_version'],2)
            self.assertNotIn('PDF anchors:',Path(r['path']).read_text(encoding='utf-8'))
            self.assertTrue(r['origin_artifact']['pdf_sources'])
            self.assertTrue(kb.current(r))
    def test_legacy_cache_remains_current_and_is_not_overwritten(self):
        self.save();job=wk.enqueue(self.path);_,note=wk.note_for(self.path)
        cache=self.root/'managed-sources'/job['source_id']/(job['fingerprint'].removeprefix('sha256:')+'.md')
        cache.parent.mkdir(parents=True);old=wk.render(note);cache.write_bytes(old.encode('utf-8'))
        with patch.object(kb,'HOME',self.root),patch.object(kb,'ingest',new=AsyncMock(return_value={'results':[]})):
            asyncio.run(wk.process(self.path));r=kb.records()[0]
            self.assertEqual(cache.read_text(encoding='utf-8'),old)
            self.assertEqual(r['origin_artifact']['render_version'],1)
            r['origin_artifact'].pop('render_version')
            self.assertTrue(kb.current(r))
            r['origin_artifact']['render_version']=999
            self.assertFalse(kb.current(r))
    def test_original_pdf_drift_blocks_previously_managed_source(self):
        self.save()
        with patch.object(kb,'HOME',self.root),patch.object(kb,'ingest',new=AsyncMock(return_value={'results':[]})):
            asyncio.run(wk.process(self.path));r=kb.records()[0];self.fixture.pdf.write_bytes(self.fixture.pdf.read_bytes()+b'changed');self.assertFalse(kb.current(r))
    def test_cache_human_edit_not_overwritten(self):
        self.save()
        with patch.object(kb,'HOME',self.root),patch.object(kb,'ingest',new=AsyncMock(return_value={'results':[]})):
            asyncio.run(wk.process(self.path));cache=Path(kb.records()[0]['path']);cache.write_text('Human edit',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'edited'):asyncio.run(wk.process(self.path))
            self.assertEqual(cache.read_text(),'Human edit')
    def test_missing_active_index_is_stale_not_success(self):
        self.save()
        with patch.object(kb,'HOME',self.root),patch.object(kb,'ingest',new=AsyncMock(return_value={'results':[]})):
            asyncio.run(wk.process(self.path))
        self.assertEqual(wk.status(self.path)['status'],'stale')
    def test_three_failed_attempts_stop_without_a_fourth_embedding_call(self):
        self.save();mock=AsyncMock(side_effect=RuntimeError('offline'))
        with patch.object(kb,'HOME',self.root),patch.object(kb,'ingest',new=mock):
            for _ in range(3):
                with self.assertRaisesRegex(RuntimeError,'offline'):asyncio.run(wk.process(self.path))
            with self.assertRaisesRegex(ValueError,'exhausted'):asyncio.run(wk.process(self.path))
        self.assertEqual(mock.call_count,3);self.assertEqual(wk.status(self.path)['status'],'failed')
    def test_archived_note_rejects_old_evidence(self):
        self.save()
        with patch.object(kb,'HOME',self.root),patch.object(kb,'ingest',new=AsyncMock(return_value={'results':[]})):
            asyncio.run(wk.process(self.path));r=kb.records()[0]
            self.fixture.store.update_research_artifact(r['origin_artifact']['artifact_id'],status='archived')
            self.assertFalse(kb.current(r))
    def test_refresh_scope_and_torn_publish_recovery(self):
        m=rw.load(self.path);state=self.root/'project-indexes'/__import__('hashlib').sha256(m['stage_id'].encode()).hexdigest()[:32]/'active-sources.json'
        state.parent.mkdir(parents=True);atomic_write_json(state,{'entries':{'source-x':{'revoked':False}}})
        workflow=self.path.parent/'grants/workflow.json';g=json.loads(workflow.read_text());g['knowledge_configured']=True;atomic_write_json(workflow,g)
        with self.assertRaises(ValueError):rw.load(self.path)
        refreshed=rw.load(self.path,refresh=True);self.assertTrue(refreshed['knowledge_configured']);self.assertEqual(json.loads((self.path.parent/'grants/library-read.json').read_text())['stage_ids'],['stage-A'])
    def test_refresh_does_not_accept_cross_project_grant(self):
        m=rw.load(self.path);state=self.root/'project-indexes'/__import__('hashlib').sha256(m['stage_id'].encode()).hexdigest()[:32]/'active-sources.json';state.parent.mkdir(parents=True);atomic_write_json(state,{'entries':{'source-x':{}}})
        atomic_write_json(self.path.parent/'grants/library-read.json',{'principal':'interactive-user','stage_ids':['stage-a']})
        with self.assertRaisesRegex(ValueError,'Knowledge authority'):rw.load(self.path,refresh=True)
    def test_filesystem_delta_preserves_managed_note(self):
        self.save()
        with patch.object(kb,'HOME',self.root),patch.object(kb,'ingest',new=AsyncMock(return_value={'results':[]})):
            asyncio.run(wk.process(self.path));r=kb.records()[0];state=kb.state_path('stage-A');state.parent.mkdir(parents=True,exist_ok=True);atomic_write_json(state,{'entries':{r['source_id']:r}})
            spec=importlib.util.spec_from_file_location('test_delta_host',Path(__file__).resolve().parents[1]/'scripts/library-delta.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
            with patch.object(mod,'HOME',self.root),patch.object(mod,'inventory',return_value={'records':[]}):result=mod.plan('stage-A')
            self.assertEqual(result['revoke'],0)

if __name__=='__main__':unittest.main()
