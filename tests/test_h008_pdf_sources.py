import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scholarflow import ScholarFlowStore, ProjectStage
from scholarflow.domain_queries import DomainQueryError
from scholarflow.reader_workspace import ReaderWorkspaceStore
from scholarflow.scientific_document import ScientificDocument, ScientificBlock


class PdfSourceBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ScholarFlowStore(self.root)
        for letter in ("a", "b"):
            self.store.create_project_stage(ProjectStage(stage_id="stage-" + letter, project_name="同名项目", title=letter, goal="Synthetic"))
        payload = b"%PDF-1.7\nsynthetic staged input\n%%EOF"
        self.content_hash = "sha256:" + hashlib.sha256(payload).hexdigest()
        document = ScientificDocument("synthetic-doc", "Synthetic evidence", self.content_hash, 3, "fixture", "1", (
            ScientificBlock("b1", "paragraph", "Page one source evidence.", 1, 1, ("Introduction",)),
            ScientificBlock("b2", "paragraph", "SECRET PAGE TWO", 2, 2, ("Private heading",)),
            ScientificBlock("b3", "paragraph", "中文片段第三页。", 3, 3),
            ScientificBlock("cross", "paragraph", "cross page", 1, 2),
            ScientificBlock("unanchored", "paragraph", "UNANCHORED SECRET", None, None)))
        with patch("scholarflow.reader_workspace.parse_scientific_pdf", return_value=document):
            self.imported = ReaderWorkspaceStore(self.root).import_pdf("synthetic.pdf", payload, project_stage_ids=["stage-a"])
        self.directory = self.root / "_reader/imports" / self.imported["import_id"]
        self.value = {"principal": "pi-local", "documents": {"doc-a": {"stage_id": "stage-a", "import_id": self.imported["import_id"],
                      "content_hash": self.content_hash, "structure_hash": self.hash(self.directory / "structured.json"), "page_ranges": [[1, 1], [3, 3]]}}}

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def hash(path):
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def api(self, **kwargs):
        from scholarflow.pdf_reading import PdfReadGrant, PdfSourceService
        return PdfSourceService(self.root, **kwargs), PdfReadGrant.from_dict(self.value)

    def assertCode(self, code, callback):
        with self.assertRaises(DomainQueryError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_outline_is_scoped_paginated_no_path_and_no_write(self):
        api, grant = self.api()
        before = {str(p): self.hash(p) for p in self.root.rglob("*") if p.is_file()}
        first = api.inspect("stage-a", "doc-a", grant, limit=1)
        second = api.inspect("stage-a", "doc-a", grant, offset=first["next_offset"])
        self.assertEqual([first["items"][0]["block_id"], second["items"][0]["block_id"]], ["b1", "b3"])
        self.assertNotIn("SECRET", json.dumps(first))
        self.assertNotIn(str(self.root), json.dumps(first))
        self.assertEqual({str(p): self.hash(p) for p in self.root.rglob("*") if p.is_file()}, before)

    def test_exact_unicode_slice_and_source_versions(self):
        api, grant = self.api()
        result = api.fragment("stage-a", "doc-a", grant, block_id="b3", char_start=2, char_end=4)
        self.assertEqual(result["text"], "片段")
        self.assertFalse(result["complete_block"])
        self.assertEqual(result["source"]["content_hash"], self.content_hash)
        self.assertEqual(result["source"]["parser"], {"name": "fixture", "version": "1"})
        self.assertEqual(result["anchor"]["page_start"], 3)

    def test_batch_fragments_match_individual_reads_and_recheck_snapshot(self):
        api, grant = self.api()
        selections = [{"block_id": "b1", "char_start": 0, "char_end": 4}, {"block_id": "b3", "char_start": 2, "char_end": 4}]
        expected = [api.fragment("stage-a", "doc-a", grant, **item) for item in selections]
        with patch.object(api, "_load", wraps=api._load) as load:
            result = api.fragments("stage-a", "doc-a", grant, selections)
        self.assertEqual(result, expected)
        self.assertEqual(load.call_count, 2)
        self.assertCode("permission_denied", lambda: api.fragments("stage-a", "doc-a", grant, [{"block_id": "b2"}]))

    def test_batch_does_not_return_if_source_changes_during_extraction(self):
        api, grant = self.api()
        original_load = api._load
        def drifting(*args):
            result = original_load(*args)
            path = self.directory / "document.pdf"
            path.write_bytes(path.read_bytes() + b"changed")
            return result
        with patch.object(api, "_load", side_effect=drifting):
            self.assertCode("source_version_conflict", lambda: api.fragments("stage-a", "doc-a", grant, [{"block_id": "b1"}]))

    def test_cross_project_unknown_and_ungranted_blocks_fail_before_content(self):
        api, grant = self.api()
        self.assertCode("permission_denied", lambda: api.inspect("stage-b", "doc-a", grant))
        self.assertCode("permission_denied", lambda: api.inspect("stage-a", "../doc-a", grant))
        self.assertCode("permission_denied", lambda: api.inspect("stage-a", [], grant))
        for block in ("b2", "cross", "unanchored", "does-not-exist"):
            self.assertCode("permission_denied", lambda: api.fragment("stage-a", "doc-a", grant, block_id=block))

    def test_source_bytes_and_structure_hash_changes_are_not_hidden(self):
        api, grant = self.api()
        path = self.directory / "document.pdf"
        before = path.read_bytes()
        path.write_bytes(before + b" changed")
        self.assertCode("source_version_conflict", lambda: api.inspect("stage-a", "doc-a", grant))
        path.write_bytes(before)
        structure = self.directory / "structured.json"
        structure.write_bytes(structure.read_bytes() + b" ")
        self.assertCode("source_version_conflict", lambda: api.inspect("stage-a", "doc-a", grant))

    def test_stale_or_removed_project_link_and_missing_source_stop(self):
        api, grant = self.api()
        path = self.directory / "metadata.json"
        original = json.loads(path.read_text())
        for change in ({"status": "parsed_stale"}, {"project_stage_ids": ["stage-b"]}):
            path.write_text(json.dumps(original | change), encoding="utf-8")
            self.assertCode("source_unavailable", lambda: api.inspect("stage-a", "doc-a", grant))
        path.write_text(json.dumps(original), encoding="utf-8")
        (self.directory / "document.pdf").rename(self.directory / "saved.pdf")
        self.assertCode("source_unavailable", lambda: api.inspect("stage-a", "doc-a", grant))

    def test_invalid_grants_and_boolean_ranges_are_rejected(self):
        from scholarflow.pdf_reading import PdfReadGrant
        for change in ({"page_ranges": [[True, 1]]}, {"page_ranges": [[2, 1]]}, {"page_ranges": [[1, 2], [2, 3]]}, {"import_id": "../../secret"}, {"structure_hash": "unpinned"}):
            item = self.value["documents"]["doc-a"] | change
            self.assertCode("permission_denied", lambda: PdfReadGrant.from_dict({"principal": "pi-local", "documents": {"doc-a": item}}))

    def test_explicit_output_budget_and_invalid_offsets_do_not_truncate(self):
        api, grant = self.api()
        self.assertCode("invalid_argument", lambda: api.fragment("stage-a", "doc-a", grant, block_id="b1", char_start=True))
        self.assertCode("invalid_argument", lambda: api.inspect("stage-a", "doc-a", grant, offset=-1))
        with patch("scholarflow.pdf_reading.MAX_FRAGMENT_BYTES", 5):
            self.assertCode("resource_limit", lambda: api.fragment("stage-a", "doc-a", grant, block_id="b1"))
            self.assertEqual(api.fragment("stage-a", "doc-a", grant, block_id="b1", char_start=0, char_end=4)["text"], "Page")

    def test_mid_read_grant_revocation_stops_return(self):
        count = [0]
        def guard():
            count[0] += 1
            if count[0] == 2:
                raise DomainQueryError("permission_denied", "revoked")
        api, grant = self.api(grant_guard=guard)
        self.assertCode("permission_denied", lambda: api.inspect("stage-a", "doc-a", grant))

    def test_fragment_does_not_leak_inherited_heading_from_ungranted_pages(self):
        path = self.directory / "structured.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["blocks"][2]["section_path"] = ["SECRET HEADING FROM PAGE TWO"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        metadata_path = self.directory / "metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["structured_digest"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        self.value["documents"]["doc-a"]["structure_hash"] = self.hash(path)
        api, grant = self.api()
        result = api.fragment("stage-a", "doc-a", grant, block_id="b3")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_hardlinked_cache_is_not_a_trusted_local_source(self):
        import os
        path = self.directory / "structured.json"
        os.link(path, self.root / "alias.json")
        api, grant = self.api()
        self.assertCode("permission_denied", lambda: api.inspect("stage-a", "doc-a", grant))

    def test_cache_metadata_drift_requires_consistent_parse(self):
        path = self.directory / "metadata.json"
        metadata = json.loads(path.read_text())
        metadata["structured_digest"] = "0" * 64
        path.write_text(json.dumps(metadata))
        api, grant = self.api()
        self.assertCode("source_version_conflict", lambda: api.inspect("stage-a", "doc-a", grant))


if __name__ == "__main__":
    unittest.main()
