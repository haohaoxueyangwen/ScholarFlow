from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest
from unittest.mock import patch

from scholarflow.domain_queries import DomainQueryError
from scholarflow.scientific_document import (
    GrobidScientificPdfParser,
    NativeScientificPdfParser,
    ScientificPdfParserRouter,
    TesseractScientificPdfParser,
    _tesseract_subprocess_environment,
    build_scientific_pdf_parser,
)


class FakePage:
    def __init__(self, text):
        self.text = text

    def extract_text(self, extraction_mode=None):
        del extraction_mode
        return self.text


class FakeReader:
    metadata = {
        "/Title": "Structured Optics Study",
        "/Author": "Ada Liu; Bo Chen",
        "/Subject": "Optics",
    }

    def __init__(self, _path):
        self.pages = [
            FakePage(
                "\n".join(
                    [
                        "Journal Header",
                        "Structured Optics Study",
                        "Abstract",
                        "We test a bounded optical method.",
                        "1 Introduction",
                        "The baseline residual was 0.031 [1, 2].",
                        "Figure 1. Experimental layout.",
                        "Page 1",
                    ]
                )
            ),
            FakePage(
                "\n".join(
                    [
                        "Journal Header",
                        "2 Results",
                        "The improved residual was 0.018.",
                        "Table 1. Comparison of residuals.",
                        "Page 2",
                    ]
                )
            ),
            FakePage(
                "\n".join(
                    [
                        "Journal Header",
                        "References",
                        "[1] A. Liu. Optical baseline. doi:10.1234/example.1",
                        "[2] B. Chen. Improved method.",
                        "(2024).",
                        "[3] C. Wu. Independent validation.",
                        "Page 3",
                    ]
                )
            ),
        ]


class BadMetadataReader(FakeReader):
    metadata = {
        "/Title": "455781_1_En_Print.indd",
        "/Author": "",
        "/Subject": "",
    }


class AuthorYearReader:
    metadata = {
        "/Title": "Author-year Citation Study",
        "/Author": "Ada Liu",
        "/Subject": "Optics",
    }

    def __init__(self, _path):
        self.pages = [FakePage("\n".join([
            "Abstract",
            "We compare prior methods (Ahlers et al., 2024; Morgan & Paganin, 2019).",
            "Results",
            "The first method remains useful (Ahlers et al., 2024).",
        ]))]


class FakeHeaders:
    def get(self, key, default=None):
        if key == "Grobid-Version":
            return "0.9.0-test"
        return default


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.headers = FakeHeaders()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class RecordingGrobidOpener:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        return FakeResponse(self.payload)


class FailingParser:
    name = "failing"

    def parse(self, _path):
        raise DomainQueryError("knowledge_grobid_unavailable", "offline")


class ErrorParser:
    name = "error"

    def __init__(self, code):
        self.code = code

    def parse(self, _path):
        raise DomainQueryError(self.code, "synthetic parser failure")


class ScientificDocumentTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "paper.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nsynthetic-fixture")

    def tearDown(self):
        self.temporary.cleanup()

    def test_native_parser_builds_page_section_caption_and_reference_anchors(self):
        document = NativeScientificPdfParser(reader_factory=FakeReader).parse(
            self.pdf
        )

        self.assertEqual(document.schema_version, 2)
        self.assertEqual(document.title, "Structured Optics Study")
        self.assertEqual(document.authors, ("Ada Liu", "Bo Chen"))
        self.assertEqual(document.page_count, 3)
        self.assertEqual(document.parser_name, "pypdf-native")
        self.assertEqual(len(document.references), 3)
        self.assertEqual(document.references[0].doi, "10.1234/example.1")
        self.assertIn("(2024).", document.references[1].raw_text)
        self.assertEqual(len(document.citations), 1)
        self.assertEqual(
            document.citations[0].reference_ids,
            ("ref-0001", "ref-0002"),
        )
        self.assertEqual(document.citations[0].raw_text, "[1, 2]")
        self.assertFalse(
            any("Journal Header" in block.text for block in document.blocks)
        )
        self.assertTrue(
            any(block.kind == "figure_caption" for block in document.blocks)
        )
        self.assertTrue(
            any(block.kind == "table_caption" for block in document.blocks)
        )
        result_block = next(
            block
            for block in document.blocks
            if "improved residual" in block.text
        )
        self.assertEqual(result_block.page_start, 2)
        self.assertEqual(result_block.section_path, ("Results",))

        markdown = document.to_index_markdown()
        self.assertIn("sf-anchor", markdown)
        self.assertIn("page=2", markdown)
        self.assertIn('section="Results"', markdown)
        self.assertIn("citations=ref-0001,ref-0002", markdown)
        self.assertIn("sf-reference id=ref-0001", markdown)
        self.assertNotIn(str(self.root), markdown)

    def test_native_parser_rejects_artifact_metadata_title(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "Doe et al. - 2024 - Reliable Wavefront Reconstruction.pdf"
            path.write_bytes(b"%PDF-test")

            document = NativeScientificPdfParser(
                reader_factory=BadMetadataReader
            ).parse(path)

        self.assertEqual(document.title, "Reliable Wavefront Reconstruction")

    def test_native_parser_falls_back_to_explicit_author_year_mentions(self):
        document = NativeScientificPdfParser(
            reader_factory=AuthorYearReader
        ).parse(self.pdf)

        self.assertEqual(len(document.references), 2)
        self.assertEqual(document.references[0].raw_text, "Ahlers et al. (2024)")
        self.assertEqual(document.references[1].raw_text, "Morgan & Paganin (2019)")
        self.assertEqual(len(document.citations), 2)
        self.assertEqual(
            document.citations[0].reference_ids,
            ("ref-0001", "ref-0002"),
        )
        self.assertIn(
            "bibliography_unstructured:author_year_mentions_only",
            document.warnings,
        )

    def test_grobid_adapter_maps_tei_to_the_same_canonical_model(self):
        tei = b"""<?xml version="1.0" encoding="UTF-8"?>
        <TEI xmlns="http://www.tei-c.org/ns/1.0">
          <teiHeader>
            <fileDesc>
              <titleStmt>
                <title>GROBID Optics Study</title>
                <author><persName><forename>Ada</forename><surname>Liu</surname></persName></author>
              </titleStmt>
            </fileDesc>
          </teiHeader>
          <text>
            <body>
              <div>
                <head coords="1,10,20,30,10">Introduction</head>
                <p coords="1,10,40,80,20">A grounded paragraph <ref type="bibr" target="#b0 #missing">[1]</ref>.</p>
                <figure type="table" coords="2,10,20,80,40">
                  <head>Table 1. Results</head>
                </figure>
              </div>
            </body>
            <back>
              <listBibl>
                <biblStruct xml:id="b0" coords="2,10,70,80,10">
                  <analytic><title>Prior work</title></analytic>
                  <idno type="DOI">10.5555/prior</idno>
                </biblStruct>
              </listBibl>
            </back>
          </text>
        </TEI>"""
        opener = RecordingGrobidOpener(tei)
        document = GrobidScientificPdfParser(
            opener=opener,
            timeout_seconds=10,
        ).parse(self.pdf)

        self.assertEqual(document.title, "GROBID Optics Study")
        self.assertEqual(document.authors, ("Ada Liu",))
        self.assertEqual(document.parser_version, "0.9.0-test")
        self.assertEqual(document.page_count, 2)
        self.assertEqual(document.references[0].doi, "10.5555/prior")
        self.assertEqual(len(document.citations), 1)
        self.assertEqual(
            document.citations[0].reference_ids,
            ("ref-0001",),
        )
        self.assertEqual(document.citations[0].raw_text, "[1]")
        self.assertIn(
            "unresolved_citation_target:missing",
            document.warnings,
        )
        self.assertEqual(
            document.citations[0].block_id,
            next(
                block.block_id
                for block in document.blocks
                if "grounded paragraph" in block.text
            ),
        )
        self.assertTrue(
            any(block.kind == "table_caption" for block in document.blocks)
        )
        request, timeout = opener.requests[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8070/api/processFulltextDocument",
        )
        self.assertEqual(timeout, 10)
        self.assertIn(b'name="teiCoordinates"', request.data)
        self.assertIn(b'application/pdf', request.data)

    def test_auto_mode_falls_back_but_strict_grobid_mode_does_not(self):
        native = NativeScientificPdfParser(reader_factory=FakeReader)
        automatic = ScientificPdfParserRouter(
            mode="auto",
            native=native,
            grobid=FailingParser(),
        ).parse(self.pdf)
        self.assertIn(
            "grobid_fallback:knowledge_grobid_unavailable",
            automatic.warnings,
        )

        with self.assertRaises(DomainQueryError) as caught:
            ScientificPdfParserRouter(
                mode="grobid",
                native=native,
                grobid=FailingParser(),
            ).parse(self.pdf)
        self.assertEqual(
            caught.exception.code,
            "knowledge_grobid_unavailable",
        )

    def test_tesseract_parser_builds_the_canonical_page_model(self):
        rendered = []
        recognized = []

        def render(path, dpi, max_pages):
            rendered.append((path, dpi, max_pages))
            return [b"page-one", b"page-two"]

        def recognize(image, languages, timeout):
            recognized.append((image, languages, timeout))
            if image == b"page-one":
                return "OCR Study\n1 Introduction\nA scanned observation."
            return "2 Results\nThe measured residual was 0.021."

        document = TesseractScientificPdfParser(
            executable="unused-in-test",
            languages="eng+chi_sim",
            dpi=250,
            max_pages=25,
            page_timeout_seconds=12,
            renderer=render,
            ocr_runner=recognize,
        ).parse(self.pdf)

        self.assertEqual(document.parser_name, "tesseract-ocr")
        self.assertEqual(document.parser_version, "injected")
        self.assertEqual(document.page_count, 2)
        self.assertEqual(document.metadata["ocr_languages"], "eng+chi_sim")
        self.assertEqual(rendered, [(self.pdf, 250, 25)])
        self.assertEqual(len(recognized), 2)
        result = next(
            block for block in document.blocks if "measured residual" in block.text
        )
        self.assertEqual(result.page_start, 2)
        self.assertEqual(result.section_path, ("Results",))

    def test_isolated_conda_tesseract_gets_runtime_and_tessdata_paths(self):
        with TemporaryDirectory() as root:
            prefix = Path(root)
            executable = prefix / "Library" / "bin" / "tesseract.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            tessdata = prefix / "share" / "tessdata"
            tessdata.mkdir(parents=True)
            with patch.dict(
                os.environ,
                {"PATH": "existing-path"},
                clear=False,
            ):
                environment = _tesseract_subprocess_environment(str(executable))

        self.assertEqual(environment["TESSDATA_PREFIX"], str(tessdata))
        self.assertTrue(environment["PATH"].startswith(str(executable.parent)))
        self.assertIn(str(prefix / "Scripts"), environment["PATH"])
        self.assertTrue(environment["PATH"].endswith("existing-path"))

    def test_auto_mode_uses_ocr_only_when_native_has_no_text(self):
        ocr = NativeScientificPdfParser(reader_factory=FakeReader)
        document = ScientificPdfParserRouter(
            mode="auto",
            native=ErrorParser("knowledge_pdf_text_unavailable"),
            ocr=ocr,
        ).parse(self.pdf)

        self.assertIn(
            "native_fallback:knowledge_pdf_text_unavailable",
            document.warnings,
        )

        with self.assertRaises(DomainQueryError) as caught:
            ScientificPdfParserRouter(
                mode="auto",
                native=ErrorParser("knowledge_pdf_parse_failed"),
                ocr=ocr,
            ).parse(self.pdf)
        self.assertEqual(caught.exception.code, "knowledge_pdf_parse_failed")

    def test_explicit_ocr_mode_requires_configuration(self):
        with self.assertRaises(DomainQueryError) as caught:
            ScientificPdfParserRouter(mode="ocr").parse(self.pdf)
        self.assertEqual(
            caught.exception.code,
            "knowledge_pdf_ocr_unconfigured",
        )

    def test_remote_grobid_is_blocked_without_https_and_opt_in(self):
        with self.assertRaises(DomainQueryError) as caught:
            GrobidScientificPdfParser("http://grobid.example.test")
        self.assertEqual(
            caught.exception.code,
            "knowledge_grobid_remote_blocked",
        )

    def test_environment_configuration_rejects_invalid_timeout(self):
        with patch.dict(
            os.environ,
            {
                "SCHOLARFLOW_GROBID_BASE_URL": "http://127.0.0.1:8070",
                "SCHOLARFLOW_GROBID_TIMEOUT_SECONDS": "not-a-number",
            },
            clear=False,
        ):
            with self.assertRaises(DomainQueryError) as caught:
                build_scientific_pdf_parser()
        self.assertEqual(
            caught.exception.code,
            "knowledge_pdf_parser_configuration_error",
        )


if __name__ == "__main__":
    unittest.main()
