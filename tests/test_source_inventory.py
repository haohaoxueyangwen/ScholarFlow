import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scholarflow import ProjectStage, ScholarFlowStore
from scholarflow.source_inventory import InventoryError, SourceInventory


class ConfiguredSourceInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "library"
        self.facts = self.home / "facts"
        self.a = self.root / "source-a"
        self.b = self.root / "source-b"
        self.a.mkdir(); self.b.mkdir()
        self.store = ScholarFlowStore(self.facts)
        for stage in ("stage-a", "stage-b"):
            self.store.create_project_stage(ProjectStage(stage_id=stage, project_name=stage, title="Synthetic", goal="Test only"))
        self.inventory = SourceInventory(self.home)

    def tearDown(self):
        self.temp.cleanup()

    def register(self, root=None, project="stage-a", source_id="local-a", kinds=("md", "pdf")):
        return self.inventory.register(root or self.a, project=project, source_id=source_id, kinds=kinds)

    def test_unconfigured_inventory_fails_without_publishing_an_empty_catalog(self):
        with self.assertRaisesRegex(InventoryError, "(?i)no source"):
            self.inventory.scan()
        self.assertFalse((self.home / "configured-inventory.json").exists())

    def test_only_an_existing_exact_project_and_directory_can_be_authorized(self):
        with self.assertRaisesRegex(InventoryError, "project"):
            self.register(project="stage-unknown")
        with self.assertRaises(InventoryError):
            self.register(root=self.root / "missing")
        self.assertFalse((self.home / "source-roots.json").exists())

    def test_scan_is_stable_and_never_writes_the_source_files(self):
        note = self.a / "note.md"; note.write_text("A synthetic note.\n", encoding="utf-8")
        pdf = self.a / "paper.pdf"; pdf.write_bytes(b"%PDF-1.4\nsynthetic")
        before = {p: p.read_bytes() for p in (note, pdf)}
        self.register()
        first = self.inventory.scan(); second = self.inventory.scan()
        self.assertEqual(first["counts"]["added"], 2)
        self.assertEqual(second["counts"]["unchanged"], 2)
        self.assertEqual([r["source_id"] for r in first["records"]], [r["source_id"] for r in second["records"]])
        self.assertEqual(before, {p: p.read_bytes() for p in (note, pdf)})
        self.assertNotIn("A synthetic note.", json.dumps(second))
        self.assertTrue(all(r["project_stage_ids"] == ["stage-a"] for r in second["records"]))

    def test_new_changed_missing_and_excluded_are_distinct(self):
        note = self.a / "note.md"; note.write_text("v1", encoding="utf-8")
        self.register(kinds=("md",)); self.inventory.scan()
        note.write_text("v2", encoding="utf-8")
        changed = self.inventory.scan(); self.assertEqual(changed["counts"]["changed"], 1)
        note.unlink()
        missing = self.inventory.scan(); self.assertEqual(missing["counts"]["missing"], 1)
        self.assertEqual(missing["records"], [])

    def test_cross_project_identical_text_does_not_share_or_hide_a_source(self):
        (self.a / "same.md").write_text("same", encoding="utf-8")
        (self.b / "same.md").write_text("same", encoding="utf-8")
        self.register(); self.register(self.b, project="stage-b", source_id="local-b")
        records = self.inventory.scan()["records"]
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]["source_id"], records[1]["source_id"])
        self.assertTrue(all(r["duplicate_of"] is None for r in records))
        self.assertEqual(len(self.inventory.for_project(records, "stage-a")), 1)
        self.assertEqual(len(self.inventory.for_project(records, "stage-b")), 1)

    def test_hidden_code_results_hardlinks_and_secret_like_notes_are_excluded(self):
        (self.a / "safe.md").write_text("ordinary note", encoding="utf-8")
        (self.a / ".hidden.md").write_text("hidden", encoding="utf-8")
        (self.a / "secret.md").write_text("api_key='" + "synthetic" + "A" * 30 + "'", encoding="utf-8")
        code = self.a / "code"; code.mkdir(); (code / "package.json").write_text("{}", encoding="utf-8"); (code / "code.pdf").write_bytes(b"%PDF-code")
        output = self.a / "results"; output.mkdir(); (output / "plot.pdf").write_bytes(b"%PDF-output")
        target = self.root / "hardlink-target.md"; target.write_text("hardlink", encoding="utf-8")
        try:
            os.link(target, self.a / "hardlink.md")
        except OSError:
            pass
        self.register(); result = self.inventory.scan()
        self.assertEqual([r["relative_path"] for r in result["records"]], ["safe.md"])
        self.assertGreaterEqual(result["counts"]["excluded"], 1)
        self.assertNotIn("synthetic" + "A" * 30, json.dumps(result))

    def test_unavailable_root_and_read_error_keep_previous_manifest(self):
        (self.a / "safe.md").write_text("a", encoding="utf-8")
        self.register(); self.inventory.scan(); manifest = self.home / "configured-inventory.json"; before = manifest.read_bytes()
        with patch("scholarflow.source_inventory._read_source", side_effect=OSError("synthetic failure")):
            with self.assertRaises(InventoryError): self.inventory.scan()
        self.assertEqual(manifest.read_bytes(), before)
        self.a.rename(self.root / "source-a-unavailable")
        with self.assertRaises(InventoryError): self.inventory.scan()
        self.assertEqual(manifest.read_bytes(), before)

    def test_file_and_byte_budget_failure_never_replaces_previous_inventory(self):
        (self.a / "one.md").write_text("one", encoding="utf-8")
        self.register(); self.inventory.scan(); manifest = self.home / "configured-inventory.json"; before = manifest.read_bytes()
        (self.a / "two.md").write_text("two", encoding="utf-8")
        with self.assertRaisesRegex(InventoryError, "budget"):
            self.inventory.scan(max_files=1)
        self.assertEqual(manifest.read_bytes(), before)

    def test_removed_authorization_immediately_hides_old_manifest(self):
        (self.a / "one.md").write_text("test", encoding="utf-8")
        self.register(); records = self.inventory.scan()["records"]
        self.assertEqual(len(self.inventory.authorized_records(records)), 1)
        config = json.loads(self.inventory.config_path.read_text(encoding="utf-8"))
        config["roots"] = []
        self.inventory.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(self.inventory.authorized_records(records), [])

    def test_tampered_manifest_cannot_change_project_or_escape_root(self):
        (self.a / "one.md").write_text("test", encoding="utf-8")
        self.register(); record = self.inventory.scan()["records"][0]
        for change in ({"project_stage_ids": ["stage-b"]}, {"path": str(self.b / "one.md")}, {"relative_path": "../source-b/one.md"}):
            self.assertEqual(self.inventory.authorized_records([{**record, **change}]), [])

    def test_registry_rejects_overlaps_and_case_insensitive_ids(self):
        self.register()
        with self.assertRaises(InventoryError): self.register(self.b, source_id="LOCAL-A")
        with self.assertRaises(InventoryError): self.register(self.root, source_id="parent")
        with self.assertRaises(InventoryError): self.register(self.home / "facts", source_id="facts")
        config = json.loads(self.inventory.config_path.read_text(encoding="utf-8"))
        config["roots"].append({**config["roots"][0], "id": "another", "path": str(self.root)})
        self.inventory.config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(InventoryError): self.inventory.scan()

    def test_automatic_scan_only_refreshes_opted_in_roots(self):
        first = self.a / "one.md"; second = self.b / "two.md"
        first.write_text("a1", encoding="utf-8"); second.write_text("b1", encoding="utf-8")
        self.inventory.register(self.a, project="stage-a", source_id="a", kinds=["md"], auto_scan=True)
        self.inventory.register(self.b, project="stage-b", source_id="b", kinds=["md"], auto_scan=False)
        old = {r["group"]: r["sha256"] for r in self.inventory.scan()["records"]}
        first.write_text("a2", encoding="utf-8"); second.write_text("b2", encoding="utf-8")
        result = self.inventory.scan(auto_only=True)
        new = {r["group"]: r["sha256"] for r in result["records"]}
        self.assertNotEqual(new["a"], old["a"])
        self.assertEqual(new["b"], old["b"])

    def test_partial_scan_promotes_surviving_duplicate_if_primary_disappears(self):
        (self.a / "same.md").write_text("same", encoding="utf-8")
        (self.b / "same.md").write_text("same", encoding="utf-8")
        self.inventory.register(self.a, project="stage-a", source_id="a", kinds=["md"])
        self.inventory.register(self.b, project="stage-a", source_id="b", kinds=["md"])
        first = self.inventory.scan()["records"]
        primary = next(item for item in first if item["duplicate_of"] is None)
        config = json.loads(self.inventory.config_path.read_text(encoding="utf-8"))
        for item in config["roots"]: item["auto_scan"] = item["id"] == primary["group"]
        self.inventory.config_path.write_text(json.dumps(config), encoding="utf-8")
        Path(primary["path"]).unlink()
        remaining = self.inventory.scan(auto_only=True)["records"]
        self.assertEqual(len(remaining), 1)
        self.assertIsNone(remaining[0]["duplicate_of"])


if __name__ == "__main__":
    unittest.main()
