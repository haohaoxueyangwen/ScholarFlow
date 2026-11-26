import tempfile,unittest
from pathlib import Path
from scholarflow.scientific_document import PyMuPdfBlockParser
from scholarflow.domain_queries import DomainQueryError

class PyMuPdfBlockTests(unittest.TestCase):
    def test_distinct_columns_keep_independent_boxes_and_stable_anchors(self):
        import fitz
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'columns.pdf';doc=fitz.open();page=doc.new_page();page.insert_text((40,80),'Left column body.');page.insert_text((330,160),'Right column body.');doc.save(path);doc.close()
            first=PyMuPdfBlockParser().parse(path);second=PyMuPdfBlockParser().parse(path)
            self.assertEqual(len(first.blocks),2);self.assertEqual([b.text for b in first.blocks],['Left column body.','Right column body.']);self.assertEqual(first.to_dict(),second.to_dict());self.assertIn('layout_order_requires_review',first.warnings);self.assertTrue(all(b.metadata['bbox'] and b.page_start==1 for b in first.blocks))
    def test_blank_pdf_does_not_invent_text(self):
        import fitz
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'blank.pdf';doc=fitz.open();doc.new_page();doc.save(path);doc.close()
            with self.assertRaises(DomainQueryError) as caught:PyMuPdfBlockParser().parse(path)
            self.assertEqual(caught.exception.code,'knowledge_pdf_text_unavailable')

if __name__=='__main__':unittest.main()
