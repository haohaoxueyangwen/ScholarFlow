import tempfile,unittest
from pathlib import Path
from scholarflow import ProjectStage,ScholarFlowStore
from scholarflow.library_projects import binding_plan,apply_binding,plan_hash
from scholarflow.knowledge_engine import KnowledgeSourceMap
from scholarflow.domain_queries import DomainQueryError

class ProjectBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'facts';store=ScholarFlowStore(self.root)
        for key in ('stage-A','stage-a'):store.create_project_stage(ProjectStage(stage_id=key,project_name='same name',title='Research',goal='test'))
        self.catalog=[{'source_id':'source-1','group':'notes','path':'note.md','sha256':'sha256:'+'a'*64,'evidence_level':'existing_note_not_original_paper'}]
    def tearDown(self):self.tmp.cleanup()
    def test_exact_case_project_membership_and_no_model_commit(self):
        plan=binding_plan(self.root,self.catalog,'stage-A',['source-1'])
        with self.assertRaises(DomainQueryError):apply_binding(self.root,plan,reviewed_hash=plan_hash(plan))
        self.assertFalse(apply_binding(self.root,plan,reviewed_hash=plan_hash(plan),operator=True)['idempotent_replay'])
        self.assertTrue(apply_binding(self.root,plan,reviewed_hash=plan_hash(plan),operator=True)['idempotent_replay'])
        mappings=KnowledgeSourceMap(self.root);self.assertEqual(len(mappings.list_sources(project_stage_id='stage-A')),1);self.assertEqual(mappings.list_sources(project_stage_id='stage-a'),[])
        revoke=binding_plan(self.root,self.catalog,'stage-A',['source-1'],revoke=True);apply_binding(self.root,revoke,reviewed_hash=plan_hash(revoke),operator=True)
        self.assertEqual(mappings.list_sources(project_stage_id='stage-A'),[])
    def test_stale_plan_does_not_overwrite_another_project_binding(self):
        first=binding_plan(self.root,self.catalog,'stage-A',['source-1']);other=binding_plan(self.root,self.catalog,'stage-a',['source-1'])
        apply_binding(self.root,first,reviewed_hash=plan_hash(first),operator=True)
        with self.assertRaises(DomainQueryError):apply_binding(self.root,other,reviewed_hash=plan_hash(other),operator=True)
if __name__=='__main__':unittest.main()
