import unittest
from scholarflow.library_answers import render_claims
from scholarflow.domain_queries import DomainQueryError

class AnswerRenderingTests(unittest.TestCase):
    def item(self,key='c-a',start=0):
        return {'citation_id':key,'title':'Note','authorship':{'kind':'unknown','model':None},'citation':{'source_id':'source-a','reference_id':'full-ref-'+key,'anchor':{'note_file':'note.md','char_start':start,'char_end':start+100}}}
    def test_two_short_handles_expand_both_exact_references(self):
        result=render_claims({'claims':[{'text':'二手笔记记载参数权衡。','citations':['c-a','c-b']}]},[self.item(),self.item('c-b',100)])
        self.assertEqual(len(result['references']),2);self.assertIn('full-ref-c-b',result['markdown']);self.assertIn('作者来源未知',result['markdown'])
    def test_unknown_missing_and_colliding_handles_fail(self):
        for refs in [[],['c-unknown']]:
            with self.assertRaises(DomainQueryError):render_claims({'claims':[{'text':'claim','citations':refs}]},[self.item()])
        with self.assertRaises(DomainQueryError):render_claims({'claims':[{'text':'claim','citations':['c-a']}]},[self.item(),self.item('c-a',100)])
if __name__=='__main__':unittest.main()
