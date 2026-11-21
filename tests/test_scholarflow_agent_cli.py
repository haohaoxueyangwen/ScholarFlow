import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/scholarflow-agent.py"


class PortableCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "agent"

    def tearDown(self): self.temp.cleanup()

    def command(self, *args, check=True):
        env = {**os.environ, "PYTHONUTF8": "1", "SCHOLARFLOW_NATURE_HOME": "", "SCHOLARFLOW_NODE": ""}
        result = subprocess.run([sys.executable, str(CLI), "--home", str(self.home), *args], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        if check and result.returncode: self.fail(result.stderr)
        return result

    def test_doctor_is_read_only(self):
        doctor = json.loads(self.command("doctor").stdout)
        self.assertFalse(doctor["starts_services"])
        self.assertFalse(self.home.exists())

    def test_create_review_register_scan_and_project_scope(self):
        first = json.loads(self.command("projects", "preview", "--name", "Synthetic", "--title", "Test", "--goal", "Fixture").stdout)
        self.assertFalse(first["created"])
        wrong = self.command("projects", "commit", "--request-id", first["request_id"], "--reviewed-hash", "wrong", check=False)
        self.assertNotEqual(wrong.returncode, 0)
        self.command("projects", "commit", "--request-id", first["request_id"], "--reviewed-hash", first["proposal_hash"])
        self.assertEqual(len(json.loads(self.command("projects", "list").stdout)), 1)
        folder = self.root / "papers"; folder.mkdir(); (folder / "note.md").write_text("synthetic only", encoding="utf-8")
        self.command("sources", "add", "--project", first["stage_id"], "--id", "test", "--path", str(folder), "--kind", "md", "--auto")
        scan = json.loads(self.command("sources", "scan", "--auto-only").stdout)
        self.assertEqual(scan["counts"]["added"], 1)
        self.assertEqual(scan["model_calls"], 0)
        self.assertEqual((folder / "note.md").read_text(encoding="utf-8"), "synthetic only")

    def test_nature_is_optional_until_installed(self):
        result = json.loads(self.command("nature", "status").stdout)
        self.assertIsNone(result["active"])

    def test_auth_cannot_read_or_echo_a_key_from_a_noninteractive_pipe(self):
        result = self.command("auth", "set-deepseek", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("interactive terminal", result.stderr)
        self.assertFalse(self.home.exists())

    def test_existing_library_is_used_only_when_explicitly_selected(self):
        from scholarflow import ProjectStage, ScholarFlowStore
        legacy = self.root / "existing-library"
        store = ScholarFlowStore(legacy / "facts")
        store.create_project_stage(ProjectStage(stage_id="stage-existing", project_name="Existing", title="Fixture", goal="Test"))
        self.assertEqual(json.loads(self.command("projects", "list").stdout), [])
        selected = json.loads(self.command("--library-home", str(legacy), "projects", "list").stdout)
        self.assertEqual([item["stage_id"] for item in selected], ["stage-existing"])

    def test_prepare_synthetic_pdf_workspace_without_model_call(self):
        try: import fitz
        except ImportError: self.skipTest("PyMuPDF not installed")
        project = json.loads(self.command("projects", "preview", "--name", "Synthetic", "--title", "PDF", "--goal", "Fixture").stdout)
        self.command("projects", "commit", "--request-id", project["request_id"], "--reviewed-hash", project["proposal_hash"])
        pdf = self.root / "synthetic.pdf"
        document = fitz.open(); page = document.new_page(); page.insert_text((72, 72), "Synthetic research document for isolated testing."); document.save(pdf); document.close()
        result = json.loads(self.command("workspaces", "prepare", "--project", project["stage_id"], "--provider", "deepseek", "--pdf", str(pdf), "--pages", "1").stdout)
        self.assertEqual(result["model_calls"], 0)
        self.assertTrue(Path(result["workspace"]).is_file())
        self.assertEqual(len(json.loads(self.command("workspaces", "list").stdout)), 1)
        self.assertEqual(json.loads(self.command("workspaces", "proposals", "--workspace", result["workspace"]).stdout), [])


if __name__ == "__main__": unittest.main()
