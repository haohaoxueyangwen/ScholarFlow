import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scholarflow import nature_skill_package as nature


class NaturePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "upstream"
        self.repo.mkdir()
        for name, content in {"LICENSE": "test license\n", "skills/nature-writing/SKILL.md": "---\nname: nature-writing\n---\n", "skills/nature-writing/manifest.yaml": "test: yes\n", "skills/nature-shared/SKILL.md": "---\nname: nature-shared\n---\n", "skills/nature-shared/manifest.yaml": "test: yes\n"}.items():
            target = self.repo / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(content, encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "LICENSE", "skills"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "synthetic"], check=True)
        commit = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        entries = {p.relative_to(self.repo).as_posix(): hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest() for p in self.repo.glob("**/*") if p.is_file() and ".git" not in p.parts}
        self.patches = [patch.object(nature, "PIN", commit), patch.object(nature, "EXPECTED_FILES", len(entries)), patch.object(nature, "EXPECTED_TREE", nature.tree_hash(entries))]
        for item in self.patches: item.start()
        self.manager = nature.NatureSkillPackage(self.root / "agent-home")

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def test_install_and_activation_keep_full_shared_resources(self):
        version = self.manager.install(self.repo)
        self.assertIsNone(self.manager.status()["active"])
        active = self.manager.activate(version)
        self.assertTrue(Path(active["skill_paths"][0]).is_file())
        self.assertTrue((Path(active["directory"]) / "skills/nature-shared/manifest.yaml").is_file())
        self.assertTrue((Path(active["directory"]) / "LICENSE").is_file())

    def test_tampering_fails_closed_and_never_removes_original(self):
        version = self.manager.install(self.repo)
        active = self.manager.activate(version)
        Path(active["skill_paths"][0]).write_text("changed", encoding="utf-8")
        with self.assertRaises(nature.NaturePackageError): self.manager.status()
        self.assertTrue((self.repo / "skills/nature-writing/SKILL.md").exists())

    def test_active_version_is_not_silently_replaced(self):
        first = self.manager.install(self.repo); second = self.manager.install(self.repo)
        self.manager.activate(first)
        with self.assertRaises(nature.NaturePackageError): self.manager.activate(second)
        self.assertEqual(self.manager.status()["active"], first)
        self.manager.activate(second, replace=True)
        self.assertTrue((self.manager.versions / first).exists())

    def test_worktree_edits_are_not_used_for_pinned_install(self):
        (self.repo / "skills/nature-writing/SKILL.md").write_text("uncommitted", encoding="utf-8")
        version = self.manager.install(self.repo)
        result = self.manager.activate(version)
        self.assertNotEqual(Path(result["skill_paths"][0]).read_text(encoding="utf-8"), "uncommitted")

    def test_update_check_never_switches_active_version(self):
        version = self.manager.install(self.repo); self.manager.activate(version)
        newer = "a" * 40
        with patch.object(nature.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=newer + "\trefs/heads/main\n", stderr="")):
            result = self.manager.check_upstream()
        self.assertTrue(result["update_available"])
        self.assertFalse(result["active_version_changed"])
        self.assertEqual(self.manager.status()["active"], version)


if __name__ == "__main__": unittest.main()
