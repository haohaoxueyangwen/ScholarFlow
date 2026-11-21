import asyncio
import hashlib
import http.client
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

import test_research_workspace as fixtures
from scholarflow import ProjectStage,ScholarFlowStore,research_workspace as rw,workspace_knowledge as wk
from scholarflow.domain_queries import DomainQueryError
from scholarflow.reader_workspace import ReaderWorkspaceStore
from scholarflow.persistence import atomic_write_json
from scholarflow.web_research import WebResearchService, pi_worker_environment
from scholarflow.workbench_api import build_server
from scripts import library_knowledge as kb

class WebResearchTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.WorkspaceTests();self.fixture.setUp()
        self.env=patch.dict(os.environ,{'SCHOLARFLOW_HOME':str(self.fixture.root/'synthetic-agent-home')})
        self.env.start();os.environ.pop('SCHOLARFLOW_NATURE_HOME',None)
        self.data=self.fixture.root/'workbench-data';self.store=ScholarFlowStore(self.data)
        for stage in ('stage-A','stage-a'):
            self.store.create_project_stage(ProjectStage(stage_id=stage,project_name='Same name',title='Synthetic',goal='Isolated test'))
        self.reader=ReaderWorkspaceStore(self.data)
        self.imported=self.reader.import_pdf('original-human-name.pdf',self.fixture.pdf.read_bytes(),project_stage_ids=['stage-A'])
        self.service=WebResearchService(self.data)
    def tearDown(self):self.env.stop();self.fixture.tearDown()
    def create(self):return self.service.create({'stage_id':'stage-A','import_id':self.imported['import_id'],'pages':'1','provider':'deepseek'})
    def test_web_worker_discovers_the_same_active_nature_home_as_cli(self):
        with patch('scholarflow.nature_skill_package.NatureSkillPackage.status',return_value={'active':'v-synthetic'}):
            env=pi_worker_environment()
        self.assertEqual(env['SCHOLARFLOW_NATURE_HOME'],str(self.fixture.root/'synthetic-agent-home'))
        self.assertNotIn('SCHOLARFLOW_NATURE_HOME',os.environ)
    def test_explicit_empty_nature_home_disables_automatic_discovery(self):
        with patch.dict(os.environ,{'SCHOLARFLOW_NATURE_HOME':''}),patch('scholarflow.nature_skill_package.NatureSkillPackage.status') as status:
            self.assertEqual(pi_worker_environment()['SCHOLARFLOW_NATURE_HOME'],'')
            status.assert_not_called()
    def test_invalid_active_nature_stops_before_model_start(self):
        from scholarflow.nature_skill_package import NaturePackageError
        with patch('scholarflow.nature_skill_package.NatureSkillPackage.status',side_effect=NaturePackageError('changed')):
            with self.assertRaisesRegex(RuntimeError,'Nature Skill'):pi_worker_environment()
    def test_upload_requires_explicit_supported_provider_before_writing(self):
        raw=self.fixture.pdf.read_bytes()
        for provider in (None,'','other'):
            with self.subTest(provider=provider):
                with self.assertRaisesRegex(DomainQueryError,'模型'):
                    self.service.upload_pdf('stage-A','paper.pdf',raw,provider=provider)
        self.assertFalse((self.service.home/'uploads').exists())
        result=self.service.upload_pdf('stage-A','paper.pdf',raw,provider='scholarflow-local')
        self.assertEqual(result['provider'],'scholarflow-local')

    def test_open_pdf_rejects_missing_or_malformed_provider(self):
        for provider in (None, [], {}):
            with self.subTest(provider=provider):
                with self.assertRaisesRegex(DomainQueryError,'模型'):
                    self.service.open_pdf({'stage_id':'stage-A','pdf_path':str(self.fixture.pdf),'provider':provider})
        self.assertFalse(self.service.home.exists())

    def test_deepseek_turn_needs_explicit_consent_before_worker(self):
        result=self.create();key=result['workspace_id']
        payload={'stage_id':'stage-A','request_id':'turn-'+'d'*32,'mode':'chat','message':'Synthetic question'}
        with patch('scholarflow.web_research.threading.Thread.start') as start:
            for consent in (None,False,'true'):
                with self.subTest(consent=consent):
                    with self.assertRaisesRegex(DomainQueryError,'确认'):
                        self.service.prompt(key,{**payload,'external_consent':consent})
            start.assert_not_called()
            turn=self.service.prompt(key,{**payload,'external_consent':True})
            self.assertEqual(turn['status'],'queued')
            start.assert_called_once()

    def test_local_model_turn_does_not_require_external_consent(self):
        result=self.service.create({'stage_id':'stage-A','import_id':self.imported['import_id'],'pages':'1','provider':'scholarflow-local'})
        payload={'stage_id':'stage-A','request_id':'turn-'+'e'*32,'mode':'chat','message':'Synthetic question'}
        with patch('scholarflow.web_research.threading.Thread.start') as start:
            turn=self.service.prompt(result['workspace_id'],payload)
            self.assertEqual(turn['status'],'queued')
            start.assert_called_once()
    def test_unavailable_local_qwen_fails_clearly_without_fallback(self):
        result=self.service.create({'stage_id':'stage-A','import_id':self.imported['import_id'],'pages':'1','provider':'scholarflow-local'})
        payload={'stage_id':'stage-A','request_id':'turn-'+'f'*32,'mode':'chat','message':'Synthetic question'}
        failure=SimpleNamespace(returncode=1,stdout='',stderr='connection refused')
        with patch('scholarflow.web_research.node_command',return_value='node'),patch('scholarflow.web_research.subprocess.run',return_value=failure) as runner:
            self.service.prompt(result['workspace_id'],payload)
            for _ in range(100):
                turn=self.service.get(result['workspace_id'],'stage-A')['turns'][0]
                if turn['status']=='failed':break
                time.sleep(.05)
        self.assertEqual(turn['status'],'failed')
        self.assertIn('本地 Qwen',turn['error'])
        self.assertIn('不会',turn['error'])
        self.assertEqual(runner.call_count,1)
    def test_upload_selection_keeps_metadata_and_authorized_project(self):
        metadata=self.reader.import_root/self.imported['import_id']/'metadata.json';before=metadata.read_bytes()
        result=self.create();self.assertEqual(result['pdf']['filename'],'original-human-name.pdf');self.assertEqual(metadata.read_bytes(),before)
        self.assertEqual(result['pdf']['page_ranges'],[[1,1]]);self.assertEqual(result['stage_id'],'stage-A')
        with self.assertRaisesRegex(DomainQueryError,'not linked'):self.service.create({'stage_id':'stage-a','import_id':self.imported['import_id']})
    def test_project_and_workspace_identity_never_cross(self):
        result=self.create();key=result['workspace_id']
        self.assertEqual(len(self.service.list('stage-A')['items']),1);self.assertEqual(self.service.list('stage-a')['items'],[])
        with self.assertRaisesRegex(DomainQueryError,'不属于'):self.service.get(key,'stage-a')
        with self.assertRaises(DomainQueryError):self.service.get('../other','stage-A')
    def test_note_approval_hash_and_legacy_facts_index_layout(self):
        result=self.create();path,_=self.service._path(result['workspace_id'],'stage-A')
        proposal=fixtures.WorkspaceTests().proposal(path)
        with self.assertRaises(DomainQueryError):self.service.decide(result['workspace_id'],{'stage_id':'stage-A','proposal_id':proposal['proposal']['proposal_id'],'reviewed_hash':'wrong'})
        with patch.object(self.service,'index',return_value={'status':'queued'}):
            saved=self.service.decide(result['workspace_id'],{'stage_id':'stage-A','proposal_id':proposal['proposal']['proposal_id'],'reviewed_hash':proposal['proposal_hash']})
        self.assertEqual(saved['phase'],'committed');self.assertEqual(saved['knowledge']['status'],'queued')
        self.assertEqual(wk.home_for(rw.load(path)),self.data/'_research_knowledge')
        with patch.object(kb,'HOME',self.data/'_research_knowledge'),patch.dict(os.environ,{'SCHOLARFLOW_LIBRARY_FACTS_DIR':str(self.data)}),patch.object(kb,'ingest',new=AsyncMock(return_value={'results':[]})):
            job=asyncio.run(wk.process(path));self.assertEqual(job['status'],'indexed')
            records=kb.records();self.assertEqual(len(records),1);self.assertTrue(kb.current(records[0]));self.assertEqual(len(kb.scoped_records('stage-A')),1);self.assertEqual(kb.scoped_records('stage-a'),[])
            self.assertTrue(records[0]['origin_artifact']['pdf_sources'][0]['anchor']['page_start']==1)
    def test_turn_is_idempotent_and_uses_native_worker(self):
        result=self.create();key=result['workspace_id'];turn='turn-'+'a'*32
        response=SimpleNamespace(returncode=0,stdout=json.dumps({'answer':'Synthetic answer','sources':[],'tool_errors':[],'usage':{}}))
        with patch('scholarflow.web_research.node_command',return_value='node'),patch('scholarflow.web_research.subprocess.run',return_value=response) as runner:
            self.service.prompt(key,{'stage_id':'stage-A','request_id':turn,'mode':'chat','message':'Explain the page','external_consent':True})
            for _ in range(100):
                state=self.service.get(key,'stage-A')['turns'][0]
                if state['status']=='completed':break
                time.sleep(.05)
            self.assertEqual(state['status'],'completed');self.assertEqual(state['answer'],'Synthetic answer');self.assertEqual(runner.call_count,1)
            same=self.service.prompt(key,{'stage_id':'stage-A','request_id':turn,'mode':'chat','message':'Explain the page','external_consent':True});self.assertEqual(same['status'],'completed')
            with self.assertRaises(DomainQueryError):self.service.prompt(key,{'stage_id':'stage-A','request_id':turn,'mode':'chat','message':'Different','external_consent':True})
    def test_operator_can_reject_without_saving_or_indexing(self):
        result=self.create();path,_=self.service._path(result['workspace_id'],'stage-A');proposal=fixtures.WorkspaceTests().proposal(path)
        with patch.object(self.service,'index') as index:
            rejected=self.service.decide(result['workspace_id'],{'stage_id':'stage-A','proposal_id':proposal['proposal']['proposal_id'],'reviewed_hash':proposal['proposal_hash'],'decision':'reject'})
        self.assertEqual(rejected['phase'],'rejected');index.assert_not_called()
        self.assertIsNone(self.store.get_research_artifact(rw.load(path)['pdf']['artifact_id']))
    def test_web_revision_keeps_project_scope_and_does_not_index(self):
        result=self.create();path,_=self.service._path(result['workspace_id'],'stage-A');old=fixtures.WorkspaceTests().proposal(path)
        payload={'stage_id':'stage-A','proposal_id':old['proposal']['proposal_id'],'reviewed_hash':old['proposal_hash'],
                 'title':'Narrower note','content':'Only the supported finding.','request_id':'revision-'+'a'*32}
        with patch.object(self.service,'index') as index:
            revised=self.service.revise(result['workspace_id'],payload)
            index.assert_not_called()
        self.assertEqual(revised['phase'],'pending');self.assertFalse(self.store.list_research_artifacts())
        with self.assertRaises(DomainQueryError):self.service.revise(result['workspace_id'],{**payload,'stage_id':'stage-a'})
    def test_index_start_error_reports_committed_note_without_rollback(self):
        result=self.create();path,_=self.service._path(result['workspace_id'],'stage-A');proposal=fixtures.WorkspaceTests().proposal(path)
        with patch.object(self.service,'index',side_effect=RuntimeError('index temporarily offline')):
            saved=self.service.decide(result['workspace_id'],{'stage_id':'stage-A','proposal_id':proposal['proposal']['proposal_id'],'reviewed_hash':proposal['proposal_hash']})
        self.assertEqual(saved['phase'],'committed');self.assertTrue(saved['knowledge']['note_saved']);self.assertEqual(saved['knowledge']['status'],'failed')
        self.assertIsNotNone(self.store.get_research_artifact(rw.load(path)['pdf']['artifact_id']))
        self.assertEqual(self.service.get(result['workspace_id'],'stage-A')['knowledge']['status'],'failed')
    def test_queued_index_without_live_worker_is_recoverable(self):
        result=self.create();path,_=self.service._path(result['workspace_id'],'stage-A');proposal=fixtures.WorkspaceTests().proposal(path)
        with patch.object(self.service,'index',return_value={'status':'queued'}):
            self.service.decide(result['workspace_id'],{'stage_id':'stage-A','proposal_id':proposal['proposal']['proposal_id'],'reviewed_hash':proposal['proposal_hash']})
        self.assertEqual(self.service.get(result['workspace_id'],'stage-A')['knowledge']['status'],'interrupted')
    def test_index_thread_start_failure_is_recorded_and_retry_is_not_blocked(self):
        result=self.create();path,_=self.service._path(result['workspace_id'],'stage-A');proposal=fixtures.WorkspaceTests().proposal(path)
        with patch('scholarflow.web_research.threading.Thread.start',side_effect=RuntimeError('synthetic thread start failure')):
            saved=self.service.decide(result['workspace_id'],{'stage_id':'stage-A','proposal_id':proposal['proposal']['proposal_id'],'reviewed_hash':proposal['proposal_hash']})
        self.assertEqual(saved['phase'],'committed')
        self.assertEqual(self.service.get(result['workspace_id'],'stage-A')['knowledge']['status'],'failed')
        self.assertNotIn(result['workspace_id'],self.service._indexing)

    def test_index_timeout_becomes_failed_and_keeps_the_note(self):
        result = self.create()
        path, _ = self.service._path(result['workspace_id'], 'stage-A')
        proposal = fixtures.WorkspaceTests().proposal(path)
        with patch.object(self.service, 'index', return_value={'status': 'queued'}):
            saved = self.service.decide(result['workspace_id'], {'stage_id': 'stage-A', 'proposal_id': proposal['proposal']['proposal_id'], 'reviewed_hash': proposal['proposal_hash']})
        self.assertEqual(saved['phase'], 'committed')
        from scholarflow.knowledge_jobs import SyncJobStore
        job = SyncJobStore(self.data).records()[0]
        with patch.object(self.service, '_index_commands', side_effect=subprocess.TimeoutExpired('embedding', 120)):
            self.service._run_index(path, result['workspace_id'], job)
        self.assertEqual(self.service.get(result['workspace_id'], 'stage-A')['knowledge']['status'], 'failed')
        self.assertIsNotNone(self.store.get_research_artifact(rw.load(path)['pdf']['artifact_id']))
        self.assertNotIn(result['workspace_id'], self.service._indexing)

    def test_index_never_copies_embedding_configuration_from_another_library(self):
        result=self.create();path,_=self.service._path(result['workspace_id'],'stage-A')
        other=self.fixture.root/'unrelated-default';other.mkdir()
        atomic_write_json(other/'embedding-runtime.json',{'synthetic':'must-not-copy'})
        from scholarflow.knowledge_jobs import SyncJobStore
        proposal=fixtures.WorkspaceTests().proposal(path)
        with patch.object(self.service,'index',return_value={'status':'queued'}):
            self.service.decide(result['workspace_id'],{'stage_id':'stage-A','proposal_id':proposal['proposal']['proposal_id'],'reviewed_hash':proposal['proposal_hash']})
        job=SyncJobStore(self.data).records()[0]
        with patch.object(rw,'DEFAULT_DATA',other/'facts'), patch.object(self.service,'_index_commands',side_effect=RuntimeError('synthetic failure')):
            self.service._run_index(path,result['workspace_id'],job)
        self.assertFalse((wk.home_for(rw.load(path))/'embedding-runtime.json').exists())

    def test_knowledge_question_waits_for_real_index(self):
        result=self.create()
        with self.assertRaisesRegex(DomainQueryError,'尚无'):self.service.prompt(result['workspace_id'],{'stage_id':'stage-A','request_id':'turn-'+'b'*32,'mode':'knowledge','message':'Question'})
    def test_existing_project_knowledge_is_available_before_this_note(self):
        state=rw.library_home(self.data)/'project-indexes'/hashlib.sha256(b'stage-A').hexdigest()[:32]/'active-sources.json'
        atomic_write_json(state,{'entries':{'source-existing':{'revoked':False}}})
        result=self.create()
        self.assertTrue(result['knowledge_available']);self.assertEqual(result['knowledge']['status'],'not_queued')
    def test_http_requires_interactive_local_session(self):
        server=build_server(self.data,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            conn.request('GET','/api/research-loop/projects');r=conn.getresponse();r.read();self.assertEqual(r.status,403)
            conn.request('GET','/api/session',headers={'Origin':'http://127.0.0.1:3000'});r=conn.getresponse();r.read();cookie=r.getheader('Set-Cookie').split(';')[0]
            headers={'Origin':'http://127.0.0.1:3000','Cookie':cookie}
            conn.request('GET','/api/research-loop/projects',headers=headers);r=conn.getresponse();body=json.loads(r.read());self.assertEqual(r.status,200);self.assertEqual(len(body['items']),2)
            payload=json.dumps({'stage_id':'stage-A','import_id':self.imported['import_id'],'pages':'1'})
            conn.request('POST','/api/research-loop/workspaces',body=payload,headers={**headers,'Content-Type':'application/json'});r=conn.getresponse();r.read();self.assertEqual(r.status,403)
            conn.request('POST','/api/research-loop/workspaces',body=payload,headers={**headers,'Content-Type':'application/json','X-ScholarFlow-Intent':'confirm'});r=conn.getresponse();created=json.loads(r.read());self.assertEqual(r.status,201)
            conn.request('GET','/api/research-loop/workspaces/'+created['workspace_id']+'?stage_id=stage-a',headers=headers);r=conn.getresponse();r.read();self.assertNotEqual(r.status,200)
        finally:server.shutdown();server.server_close();thread.join(timeout=5)

if __name__=='__main__':unittest.main()
