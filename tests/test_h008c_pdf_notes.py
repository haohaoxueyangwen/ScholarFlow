import json
from pathlib import Path
import unittest

import test_h008_pdf_sources as fixtures
from scholarflow.pdf_notes import capability_module, read_pdf_note_sources
from scholarflow.mutations import record_fingerprint
from scholarflow.source_notes import source_version_state,validate_source_snapshot


class PdfNoteTest(unittest.TestCase):
    hash=staticmethod(fixtures.PdfSourceBoundaryTest.hash)
    def setUp(self):
        fixtures.PdfSourceBoundaryTest.setUp(self)
        self.path=self.root/'pdf-grant.json';self.path.write_text(json.dumps(self.value))
        self.api=capability_module('pdf-note')
        self.grant=self.api.PdfNoteGrant.from_dict({'principal':'pi-local','create_notes':{'artifact-pdf-note':'stage-a'},'update_notes':{},'pdf_grant_file':str(self.path),'pdf_sources':{'stage-a':[{'document_id':'doc-a','block_id':'b3','char_start':0,'char_end':8}]}})
        self.facade=self.api.PdfNoteFacade(self.root,self.grant)
    def tearDown(self):fixtures.PdfSourceBoundaryTest.tearDown(self)
    def request(self,action,params,key=None):
        result={'protocol_version':1,'request_id':'req-'+action,'task_id':'pdf-note-test','operation':'pdf.note.'+action,'scope':{'kind':'note','stage_id':'stage-a','artifact_id':'artifact-pdf-note'},'params':params}
        if key:result['idempotency_key']=key
        return result
    def proposal(self,quotes=None):
        context=self.facade.execute(self.request('context',{'mode':'create'}),self.grant)
        self.assertTrue(context['ok'],context)
        self.snapshot=context['data']['sources']
        params={'mode':'create','artifact_id':'artifact-pdf-note','changes':{'title':'中文PDF note','content':'派生总结；不冒充原文。','status':'draft'},'source_versions':{s['source_id']:s['fingerprint'] for s in self.snapshot},'expected_fingerprint':context['data']['expected_fingerprint']}
        if quotes is not None:params['quotes']=quotes
        return self.facade.execute(self.request('propose',params,'unique'),self.grant)
    def decide(self,proposal):
        return self.facade.execute(self.request('decide',{'proposal_id':proposal['data']['proposal']['proposal_id'],'reviewed_hash':proposal['data']['proposal_hash'],'decision':'approve'}),self.grant,operator=True)
    def test_multi_fragment_freshness_is_batched_and_detects_drift(self):
        from unittest.mock import patch
        from scholarflow.pdf_reading import PdfSourceService
        grant=self.api.PdfNoteGrant.from_dict({'principal':'pi-local','create_notes':{'artifact-pdf-note':'stage-a'},'update_notes':{},'pdf_grant_file':str(self.path),'pdf_sources':{'stage-a':[
            {'document_id':'doc-a','block_id':'b1','char_start':0,'char_end':4},
            {'document_id':'doc-a','block_id':'b1','char_start':5,'char_end':8},
            {'document_id':'doc-a','block_id':'b3','char_start':0,'char_end':8}]}})
        snapshot=read_pdf_note_sources(self.store,'stage-a',grant)
        original=PdfSourceService._load
        with patch.object(PdfSourceService,'_load',autospec=True,side_effect=original) as load:
            state=source_version_state(self.store,snapshot)
        self.assertTrue(state['matches_current'])
        self.assertEqual(load.call_count,2)
        path=self.directory/'document.pdf';path.write_bytes(path.read_bytes()+b' changed')
        self.assertFalse(source_version_state(self.store,snapshot)['matches_current'])
    def test_real_shared_approval_commit_and_restart_source_snapshot(self):
        proposed=self.proposal();self.assertTrue(proposed['ok'],proposed)
        self.assertIsNone(self.store.get_research_artifact('artifact-pdf-note'))
        forbidden=self.facade.execute(self.request('decide',{'proposal_id':proposed['data']['proposal']['proposal_id'],'reviewed_hash':proposed['data']['proposal_hash'],'decision':'approve'}),self.grant)
        self.assertEqual(forbidden['error']['code'],'permission_denied')
        committed=self.decide(proposed);self.assertTrue(committed['ok'],committed)
        record=self.store.get_research_artifact('artifact-pdf-note')
        self.assertEqual(record.source_snapshot['version'],2);self.assertEqual(record.related_papers,[])
        self.assertTrue(record.evidence_refs[0].startswith('pdf:'))
        self.assertTrue(source_version_state(self.store,record.source_snapshot)['matches_current'])
        replay=self.decide(proposed);self.assertTrue(replay['ok'],replay)
        self.assertEqual(record_fingerprint(self.store.get_research_artifact('artifact-pdf-note').to_dict()),record_fingerprint(record.to_dict()))
    def test_quote_forgery_is_rejected_without_note(self):
        context=self.facade.execute(self.request('context',{'mode':'create'}),self.grant)
        source=context['data']['sources'][0]
        result=self.proposal({source['source_id']:'forged quote'})
        self.assertEqual(result['error']['code'],'source_quote_mismatch',result)
        self.assertIsNone(self.store.get_research_artifact('artifact-pdf-note'))
    def test_source_drift_before_approval_denies_commit(self):
        proposed=self.proposal();self.assertTrue(proposed['ok'],proposed)
        path=self.directory/'document.pdf';path.write_bytes(path.read_bytes()+b' changed')
        result=self.decide(proposed)
        self.assertEqual(result['error']['code'],'source_version_conflict',result)
        self.assertIsNone(self.store.get_research_artifact('artifact-pdf-note'))
    def test_authority_revocation_denies_commit(self):
        proposed=self.proposal();self.path.write_text('{}')
        result=self.decide(proposed)
        self.assertEqual(result['error']['code'],'permission_denied',result)
        self.assertIsNone(self.store.get_research_artifact('artifact-pdf-note'))
    def test_cross_project_and_injected_snapshot_are_rejected(self):
        request=self.request('context',{'mode':'create'});request['scope']['stage_id']='stage-b'
        result=self.facade.execute(request,self.grant);self.assertEqual(result['error']['code'],'permission_denied')
    def test_snapshot_text_tamper_is_detected(self):
        proposal=self.proposal();snapshot=proposal['data']['proposal']['normalized_changes']['source_snapshot']
        snapshot['sources'][0]['text']='tampered'
        with self.assertRaises(ValueError):validate_source_snapshot(snapshot)

    def test_empty_list_quotes_are_not_a_valid_mapping(self):
        context=self.facade.execute(self.request('context',{'mode':'create'}),self.grant)['data']
        params={'mode':'create','artifact_id':'artifact-pdf-note','changes':{'title':'test','content':'derived','status':'draft'},'source_versions':{s['source_id']:s['fingerprint'] for s in context['sources']},'expected_fingerprint':context['expected_fingerprint'],'quotes':[]}
        result=self.facade.execute(self.request('propose',params,'bad-quotes'),self.grant)
        self.assertEqual(result['error']['code'],'source_quote_mismatch')

    def test_markdown_note_final_newline_is_normalized_before_review(self):
        context=self.facade.execute(self.request('context',{'mode':'create'}),self.grant)['data']
        content='# Reading\n\nA derived note.\n'
        params={'mode':'create','artifact_id':'artifact-pdf-note','changes':{'title':'Readable','content':content,'status':'draft'},'source_versions':{s['source_id']:s['fingerprint'] for s in context['sources']},'expected_fingerprint':context['expected_fingerprint']}
        result=self.facade.execute(self.request('propose',params,'markdown-newline'),self.grant)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['data']['proposal']['normalized_changes']['content'],content.strip())


if __name__=='__main__':unittest.main()
