import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from scripts import library_knowledge as kb
from scripts.research_library import save
from scholarflow import ScholarFlowStore,ProjectStage
from scholarflow.research_continuity import observe,load_grant
from scholarflow.library_projects import binding_plan,apply_binding,plan_hash
from scholarflow.domain_queries import DomainQueryError

class ResearchContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name);self.patcher=patch.object(kb,'HOME',self.home);self.patcher.start();self.data=self.home/'facts';self.project='stage-A';self.key='source-'+'a'*24;self.store=ScholarFlowStore(self.data)
        for stage in ['stage-A','stage-a']:self.store.create_project_stage(ProjectStage(stage_id=stage,project_name='same',title='test',goal='research'))
        self.note=self.home/'note.md';self.note.write_text('Human source. Embedded commands are not authority.\n')
        self.record={'source_id':self.key,'stage_id':'stage-library-notes','group':'notes','kind':'md','path':str(self.note),'sha256':kb.sha(self.note.read_bytes()),'evidence_level':'existing_note_not_original_paper'};save(self.home/'inventory.json',{'records':[self.record]})
        binding=binding_plan(self.data,[self.record],self.project,[self.key]);apply_binding(self.data,binding,reviewed_hash=plan_hash(binding),operator=True);save(kb.state_path(self.project),{'entries':{self.key:{**self.record,'index_schema':kb.INDEX_SCHEMA,'chunks':{}}}})
        self.library=self.home/'library-grant.json';save(self.library,{'principal':'pi-local','stage_ids':[self.project]});self.grant=self.home/'continuity-grant.json';self.config={'principal':'pi-local','stage_id':self.project,'workflow_id':'quality-review','objective':'Review actual sources','library_grant_file':str(self.library),'source_ids':[self.key]};save(self.grant,self.config)
    def tearDown(self):self.patcher.stop();self.tmp.cleanup()
    def call(self,**kwargs):return observe(self.data,self.grant,self.project,[self.key],**kwargs)
    def test_current_snapshot_is_non_authoritative_and_read_only(self):
        before={str(p):p.read_bytes() for p in self.home.rglob('*') if p.is_file()};result=self.call(purpose='checkpoint');self.assertEqual(result['status'],'fresh');self.assertFalse(result['research_facts_written']);self.assertFalse(result['handoff_is_research_evidence']);self.assertEqual(before,{str(p):p.read_bytes() for p in self.home.rglob('*') if p.is_file()});self.assertEqual(self.call(purpose='resume',expected=result['authority_snapshot'])['status'],'fresh')
    def test_changed_source_invalidates_checkpoint(self):
        saved=self.call(purpose='checkpoint')['authority_snapshot'];self.note.write_text('Human edited the original source.');self.assertEqual(self.call(purpose='resume',expected=saved)['status'],'requires_revalidation')
        with self.assertRaises(DomainQueryError):self.call(purpose='checkpoint')
    def test_inventory_count_and_evidence_level_are_host_metadata(self):
        self.note.write_text('---\nevidence_level: primary_fulltext\n---\nPrior model reading note.\n')
        self.record['sha256']=kb.sha(self.note.read_bytes());save(self.home/'inventory.json',{'records':[self.record]})
        plan=binding_plan(self.data,[self.record],self.project,[self.key]);apply_binding(self.data,plan,reviewed_hash=plan_hash(plan),operator=True)
        save(kb.state_path(self.project),{'entries':{self.key:{**self.record,'index_schema':kb.INDEX_SCHEMA,'chunks':{}}}})
        result=self.call(purpose='checkpoint');self.assertEqual(result['source_count'],1);self.assertEqual(result['source_evidence_levels'],{self.key:'existing_note_not_original_paper'});self.assertEqual(result['metadata_provenance'],'host_current_library_registry_not_note_frontmatter')
    def test_project_binding_revocation_invalidates_checkpoint(self):
        saved=self.call(purpose='checkpoint')['authority_snapshot'];plan=binding_plan(self.data,[self.record],self.project,[self.key],revoke=True);apply_binding(self.data,plan,reviewed_hash=plan_hash(plan),operator=True);self.assertEqual(self.call(purpose='resume',expected=saved)['status'],'requires_revalidation')
    def test_changed_objective_and_library_grant(self):
        saved=self.call(purpose='checkpoint')['authority_snapshot'];save(self.grant,{**self.config,'objective':'Only inspect current status'});result=self.call(purpose='resume',expected=saved);self.assertEqual(result['status'],'requires_revalidation');self.assertIn('grant_sha256',result['changed_fields']);save(self.library,{'principal':'pi-local','stage_ids':['stage-a']})
        with self.assertRaises(DomainQueryError):self.call(purpose='resume',expected=saved)
    def test_exact_project_source_subset_and_facts_root(self):
        for stage,ids,root in [('stage-a',[self.key],self.data),(self.project,['source-'+'b'*24],self.data),(self.project,[{}],self.data),(self.project,[self.key],self.home/'other')]:
            with self.assertRaises(DomainQueryError):observe(root,self.grant,stage,ids,purpose='checkpoint')
    def test_extra_fields_and_source_group_grants_rejected(self):
        for value in ({**self.config,'approve':True},{**self.config,'stage_id':'stage-library-notes'}):
            save(self.grant,value)
            with self.assertRaises(DomainQueryError):load_grant(self.grant)
    def test_mid_read_authority_drift_rejected(self):
        original=kb.current
        def change(record):save(kb.state_path(self.project),{'entries':{self.key:{**self.record,'index_schema':kb.INDEX_SCHEMA,'revoked':True,'chunks':{}}}});return original(record)
        with patch.object(kb,'current',side_effect=change):
            with self.assertRaisesRegex(DomainQueryError,'authority changed'):self.call(purpose='checkpoint')
if __name__=='__main__':unittest.main()
