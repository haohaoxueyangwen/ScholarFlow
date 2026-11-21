import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scholarflow import ProjectStage, ScholarFlowStore
from scholarflow.knowledge_jobs import SyncJobStore
from scholarflow.library_projects import apply_binding, binding_plan, plan_hash
from scholarflow.source_inventory import SourceInventory
from scripts import library_knowledge as kb

spec = importlib.util.spec_from_file_location("configured_delta", Path(__file__).parents[1] / "scripts/library-delta.py")
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)


class ConfiguredLibraryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.home = root / "library"
        store = ScholarFlowStore(self.home / "facts")
        for name in ("stage-a", "stage-b"):
            store.create_project_stage(ProjectStage(stage_id=name, project_name=name, title="Synthetic", goal="Test"))
        self.inventory = SourceInventory(self.home)
        for name, stage in (("a", "stage-a"), ("b", "stage-b")):
            folder = root / name
            folder.mkdir()
            (folder / "note.md").write_text("synthetic " + name, encoding="utf-8")
            self.inventory.register(folder, project=stage, source_id=name, kinds=["md"])
        self.inventory.scan()
        self.patches = [patch.object(kb, "HOME", self.home), patch.object(operator, "HOME", self.home),
                        patch.object(operator, "inventory", side_effect=self.inventory.scan)]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def test_delta_only_plans_the_selected_project(self):
        plan = operator.plan("stage-a")
        job = SyncJobStore(self.home / "facts").get(plan["plan_id"])
        self.assertEqual(len(job["plan"]["records"]), 1)
        self.assertEqual(job["plan"]["records"][0]["project_stage_ids"], ["stage-a"])

    def test_wrong_project_binding_cannot_expose_configured_record(self):
        record = next(item for item in kb.records() if item["project_stage_ids"] == ["stage-a"])
        link = binding_plan(self.home / "facts", [record], "stage-b", [record["source_id"]])
        apply_binding(self.home / "facts", link, reviewed_hash=plan_hash(link), operator=True)
        self.assertEqual(kb.scoped_records("stage-b"), [])

    def test_revoked_source_cannot_be_applied_or_retrieved(self):
        plan = operator.plan("stage-a")
        config = json.loads(self.inventory.config_path.read_text(encoding="utf-8"))
        config["roots"] = [item for item in config["roots"] if item["id"] != "a"]
        self.inventory.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual([item["group"] for item in kb.records()], ["b"])
        with patch.object(kb, "ingest", side_effect=AssertionError("must not ingest")):
            with self.assertRaisesRegex(ValueError, "inventory changed"):
                asyncio.run(operator.apply(plan["plan_id"], plan["fingerprint"], True))


if __name__ == "__main__":
    unittest.main()
