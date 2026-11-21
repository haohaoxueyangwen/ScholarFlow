import json,os,tempfile,unittest
import subprocess,sys
from pathlib import Path
from unittest.mock import patch
from scholarflow import ScholarFlowStore,ProjectStage
from scholarflow import research_workspace as rw
from scholarflow.persistence import atomic_write_json
from scholarflow.pdf_notes import capability_module
from scholarflow.pi_lifecycle import node_command

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        import fitz
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.data=self.root/'facts';self.home=self.root/'workspaces';self.store=ScholarFlowStore(self.data)
        for stage in ['stage-A','stage-a','stage-library-notes']:self.store.create_project_stage(ProjectStage(stage_id=stage,project_name='Same name',title='Test',goal='Synthetic only'))
        self.pdf=self.root/'paper.pdf';doc=fitz.open()
        for i in range(2):
            page=doc.new_page();page.insert_text((50,60),'Synthetic research chapter '+str(i+1));page.insert_text((50,100),'A coded aperture controls which measurements are recorded.');page.insert_text((50,150),'This fixture contains no experimental performance claims.')
        doc.save(self.pdf);doc.close();self.env=patch.dict(os.environ,{'SCHOLARFLOW_PDF_PARSER':'pymupdf-blocks','SCHOLARFLOW_GROBID_BASE_URL':''});self.env.start()
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def prepare(self,stage='stage-A',**kwargs):return rw.prepare(self.data,stage,pdf=self.pdf,home=self.home,**kwargs)
    def proposal(self,path):
        m,facade,grant,scope=rw.note_facade(path);context=rw.call(facade,grant,'pdf.note','context',scope,{'mode':'create'},m)
        p={'mode':'create','artifact_id':m['pdf']['artifact_id'],'changes':{'title':'Synthetic derived note','content':'Derived from approved synthetic fragments, not a real experiment.','status':'draft'},'source_versions':{s['source_id']:s['fingerprint'] for s in context['sources']},'expected_fingerprint':context['expected_fingerprint']}
        return rw.call(facade,grant,'pdf.note','propose',scope,p,m)
    def test_exact_project_scope_and_original_preserved(self):
        before=self.pdf.read_bytes();path=self.prepare();m=rw.load(path);self.assertEqual(m['stage_id'],'stage-A');self.assertEqual(m['pdf']['page_ranges'],[[1,1]]);self.assertFalse(m['knowledge_configured']);self.assertEqual(self.pdf.read_bytes(),before);self.assertFalse(self.store.list_research_artifacts())
    def test_catalog_excludes_source_groups_and_keeps_case_identity(self):self.assertEqual({p['stage_id'] for p in rw.projects(self.data)},{'stage-A','stage-a'})
    def test_unknown_and_source_group_projects_rejected(self):
        for stage in ['stage-missing','stage-library-notes']:
            with self.assertRaises(ValueError):self.prepare(stage)
    def test_invalid_or_overlapping_pages_rejected(self):
        for pages in ['0','3','1-2,2','../1','2-1']:
            with self.assertRaises(ValueError):self.prepare(pages=pages)
    def test_source_drift_prevents_resume_and_operator_save(self):
        path=self.prepare();p=self.proposal(path);self.pdf.write_bytes(self.pdf.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'changed'):rw.load(path)
        with self.assertRaises(ValueError):rw.decide(path,p['proposal']['proposal_id'],p['proposal_hash'])
        self.assertFalse(self.store.list_research_artifacts())
    def test_same_pdf_shared_cache_but_new_scopes(self):
        a=self.prepare();b=self.prepare('stage-a');x=rw.load(a);y=rw.load(b);self.assertEqual(x['pdf']['import_id'],y['pdf']['import_id']);self.assertNotEqual(x['workspace_id'],y['workspace_id']);self.assertNotEqual(x['pdf']['artifact_id'],y['pdf']['artifact_id']);rw.load(a)
    def test_cross_workspace_proposal_rejected(self):
        a=self.prepare();b=self.prepare('stage-a');p=self.proposal(a)
        with self.assertRaises(ValueError):rw.decide(b,p['proposal']['proposal_id'],p['proposal_hash'])
    def test_wrong_hash_and_read_only_status(self):
        path=self.prepare();p=self.proposal(path);self.assertEqual(rw.proposals(path)[0]['phase'],'pending')
        with self.assertRaises(ValueError):rw.decide(path,p['proposal']['proposal_id'],'0'*64)
        self.assertFalse(self.store.list_research_artifacts())
    def test_reviewed_edit_creates_new_proposal_then_explicit_approval(self):
        path=self.prepare();old=self.proposal(path);pid=old['proposal']['proposal_id'];request='revision-'+'1'*32
        args={'title':'Corrected note','content':'The original reports an increase, not monotonicity.','request_id':request}
        new=rw.revise(path,pid,old['proposal_hash'],**args)
        self.assertNotEqual(new['proposal']['proposal_id'],pid);self.assertNotEqual(new['proposal_hash'],old['proposal_hash'])
        self.assertEqual(new['proposal']['normalized_changes']['source_snapshot'],old['proposal']['normalized_changes']['source_snapshot'])
        self.assertEqual({p['phase'] for p in rw.proposals(path)},{'pending','rejected'})
        self.assertFalse(self.store.list_research_artifacts())
        self.assertEqual(rw.revise(path,pid,old['proposal_hash'],**args)['proposal']['proposal_id'],new['proposal']['proposal_id'])
        with self.assertRaises(ValueError):rw.revise(path,pid,old['proposal_hash'],**{**args,'content':'Different'})
        with self.assertRaises(ValueError):rw.decide(path,pid,old['proposal_hash'])
        with self.assertRaises(ValueError):rw.decide(path,new['proposal']['proposal_id'],old['proposal_hash'])
        saved=rw.decide(path,new['proposal']['proposal_id'],new['proposal_hash']);self.assertEqual(saved['phase'],'committed')
        self.assertEqual(self.store.get_research_artifact(rw.load(path)['pdf']['artifact_id']).content,args['content'])
    def test_revision_rejects_wrong_hash_cross_workspace_and_source_drift(self):
        path=self.prepare();other=self.prepare('stage-a');old=self.proposal(path);pid=old['proposal']['proposal_id']
        args={'title':'Corrected','content':'Corrected content','request_id':'revision-'+'2'*32}
        with self.assertRaises(ValueError):rw.revise(path,pid,'wrong',**args)
        with self.assertRaises(ValueError):rw.revise(other,pid,old['proposal_hash'],**args)
        self.pdf.write_bytes(self.pdf.read_bytes()+b'changed')
        with self.assertRaises(ValueError):rw.revise(path,pid,old['proposal_hash'],**args)
        self.assertFalse(self.store.list_research_artifacts())
    def test_revision_can_recover_after_rejection_before_new_proposal(self):
        path=self.prepare();old=self.proposal(path);pid=old['proposal']['proposal_id']
        args={'title':'Revised','content':'A narrower claim.','request_id':'revision-'+'3'*32}
        actual=rw.call
        def interrupted(facade,grant,prefix,action,scope,params,workspace,**kwargs):
            if action=='propose':raise ValueError('simulated interruption')
            return actual(facade,grant,prefix,action,scope,params,workspace,**kwargs)
        with patch.object(rw,'call',side_effect=interrupted):
            with self.assertRaisesRegex(ValueError,'interruption'):rw.revise(path,pid,old['proposal_hash'],**args)
        revised=rw.revise(path,pid,old['proposal_hash'],**args)
        self.assertEqual(revised['phase'],'pending');self.assertEqual(len(rw.proposals(path)),2)
    def test_portable_cli_can_revise_without_web_or_model(self):
        path=self.prepare();old=self.proposal(path);source=self.root/'edited.md'
        source.write_text('A narrower, source-supported statement.',encoding='utf-8')
        command=[sys.executable,str(rw.ROOT/'scripts/scholarflow-agent.py'),'--home',str(self.root/'cli-home'),
            'workspaces','revise','--workspace',str(path),'--proposal-id',old['proposal']['proposal_id'],
            '--reviewed-hash',old['proposal_hash'],'--title','Corrected','--content-file',str(source),'--request-id','revision-'+'4'*32]
        result=subprocess.run(command,cwd=rw.ROOT,capture_output=True,text=True,encoding='utf-8',check=True)
        revised=json.loads(result.stdout)
        self.assertEqual(revised['phase'],'pending');self.assertEqual(revised['proposal']['normalized_changes']['content'],source.read_text(encoding='utf-8'))
        self.assertFalse(self.store.list_research_artifacts())
    def test_save_export_and_fresh_process_equivalent_read(self):
        path=self.prepare();p=self.proposal(path);saved=rw.decide(path,p['proposal']['proposal_id'],p['proposal_hash']);self.assertEqual(saved['phase'],'committed');plan=rw.export(path)
        with self.assertRaises(ValueError):rw.export(path,reviewed_hash='0'*64)
        receipt=rw.export(path,reviewed_hash=plan['proposal_hash']);self.assertEqual(receipt['phase'],'committed');m=rw.load(path);out=path.parent/'notes'/(m['pdf']['artifact_id']+'.md');self.assertIn('Synthetic derived note',out.read_text(encoding='utf-8'));self.assertEqual(rw.proposals(path)[0]['phase'],'committed');self.assertFalse(m['knowledge_configured'])
    def test_export_does_not_overwrite_intervening_human_edit(self):
        path=self.prepare();p=self.proposal(path);rw.decide(path,p['proposal']['proposal_id'],p['proposal_hash']);plan=rw.export(path);m=rw.load(path);out=path.parent/'notes'/(m['pdf']['artifact_id']+'.md');out.parent.mkdir(exist_ok=True);out.write_text('Human edit',encoding='utf-8')
        with self.assertRaises(ValueError):rw.export(path,reviewed_hash=plan['proposal_hash'])
        self.assertEqual(out.read_text(encoding='utf-8'),'Human edit')
    def test_modified_workflow_grant_cannot_change_selected_project(self):
        path=self.prepare();grant=path.parent/'grants/workflow.json';g=json.loads(grant.read_text(encoding='utf-8'));g['stage_id']='stage-a';atomic_write_json(grant,g)
        with self.assertRaisesRegex(ValueError,'Workflow authority'):rw.load(path)
    def test_hardlinked_input_rejected(self):
        alias=self.root/'alias.pdf';os.link(self.pdf,alias)
        with self.assertRaisesRegex(ValueError,'hardlinked'):rw.prepare(self.data,'stage-A',pdf=alias,home=self.home)
    def test_cli_uses_utf8_even_without_utf8_environment(self):
        self.store.create_project_stage(ProjectStage(stage_id='stage-zh',project_name='中文项目',title='阅读',goal='测试'))
        script=Path(__file__).resolve().parents[1]/'scripts/research-workspace.py'
        result=subprocess.run([sys.executable,'-s','-B',str(script),'projects','--data-dir',str(self.data)],capture_output=True,encoding='utf-8',env={**os.environ,'PYTHONUTF8':'0'},timeout=30)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(next(p for p in json.loads(result.stdout) if p['stage_id']=='stage-zh')['project_name'],'中文项目')
    def test_goal_preview_stays_uncreated_until_exact_hash(self):
        home=self.root/'project-home'
        preview=rw.preview_project(self.data,project_name='花书学习',title='逐章阅读',goal='只根据已授权页记笔记。',home=home)
        self.assertFalse(preview['created'])
        self.assertIsNone(self.store.get_project_stage(preview['stage_id']))
        self.assertEqual(preview['changes']['evidence_refs'],[])
        with self.assertRaisesRegex(ValueError,'Exact reviewed project hash'):
            rw.commit_project(self.data,preview['request_id'],'0'*64,home=home)
        self.assertIsNone(self.store.get_project_stage(preview['stage_id']))
        created=rw.commit_project(self.data,preview['request_id'],preview['proposal_hash'],home=home)
        self.assertEqual(created['project_name'],'花书学习')
        self.assertEqual(created['evidence_refs'],[])
        self.assertEqual(created['creation_origin']['kind'],'user_goal')
        replay=rw.commit_project(self.data,preview['request_id'],preview['proposal_hash'],home=home)
        self.assertEqual(replay['stage_id'],created['stage_id'])
        self.assertEqual(len(self.store.list_project_stages()),4)
    def test_native_runtime_rejects_modified_endpoint_before_reading_credentials(self):
        node=Path(node_command())
        if not node.is_file():self.skipTest('Node runtime unavailable')
        path=self.prepare();module=Path(__file__).resolve().parents[1]/'packages/scholarflow-pi/harness/research-runtime.mjs'
        code=f"import {{buildResearchRuntime}} from {json.dumps(module.as_uri())};const r=buildResearchRuntime(process.argv[1],{{credentials:false}});console.log(JSON.stringify(r.composition.enabled.map(x=>x.id)));process.exit(0);"
        def invoke():return subprocess.run([str(node),'--input-type=module','-e',code,str(path)],capture_output=True,encoding='utf-8',env={**os.environ,'SCHOLARFLOW_PYTHON':sys.executable},timeout=30)
        result=invoke();self.assertEqual(result.returncode,0,result.stderr);self.assertIn('pdf-note',json.loads(result.stdout));file=path.parent/'pi-state/models.json';value=json.loads(file.read_text());value['providers']['deepseek']['baseUrl']='https://unapproved.invalid';atomic_write_json(file,value)
        rejected=invoke();self.assertNotEqual(rejected.returncode,0);self.assertIn('configuration changed',rejected.stderr)

if __name__=='__main__':unittest.main()
