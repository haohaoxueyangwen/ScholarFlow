import unittest
from scripts.library_knowledge import evidence_metadata

class EvidenceMetadataTests(unittest.TestCase):
    def test_existing_note_author_is_unknown_and_second_chunk_is_preserved(self):
        result=evidence_metadata({'source_id':'source-a','evidence_level':'existing_note_not_original_paper'}, {'char_start':1200,'char_end':2241}, 'sf:a:1')
        self.assertEqual(result['authorship'],{'kind':'unknown','model':None})
        self.assertEqual(result['citation']['reference_id'],'sf:a:1')
        self.assertEqual(result['citation']['anchor']['char_start'],1200)
    def test_only_recorded_derivation_supports_model_attribution(self):
        record={'source_id':'source-b','evidence_level':'derived_pdf_fragment_note','origin_evidence':{'model':'scholarflow-local/qwen38-vision'}}
        self.assertEqual(evidence_metadata(record,{},'ref')['authorship']['kind'],'recorded_model_derivation')
        record['evidence_level']='existing_note_not_original_paper'
        self.assertEqual(evidence_metadata(record,{},'ref')['authorship']['kind'],'unknown')

if __name__=='__main__':unittest.main()
