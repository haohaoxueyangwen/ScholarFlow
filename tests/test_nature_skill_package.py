import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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

    def test_new_release_preserves_validation_and_rollback_to_legacy_pin(self):
        version = self.manager.install(self.repo); self.manager.activate(version)
        old_pin, old_files, old_tree = nature.PIN, nature.EXPECTED_FILES, nature.EXPECTED_TREE
        with patch.object(nature, "LEGACY_PIN", old_pin), patch.object(nature, "LEGACY_FILES", old_files), patch.object(nature, "LEGACY_TREE", old_tree), patch.object(nature, "PIN", "b" * 40), patch.object(nature, "EXPECTED_FILES", 999), patch.object(nature, "EXPECTED_TREE", "c" * 64):
            self.assertEqual(self.manager.status()["active"], version)
            self.assertEqual(self.manager.activate(version, replace=True)["commit"], old_pin)

    def test_new_activation_never_overwrites_legacy_client_record(self):
        first = self.manager.install(self.repo); second = self.manager.install(self.repo)
        self.manager.base.mkdir(parents=True, exist_ok=True)
        self.manager.legacy_active_file.write_text(json.dumps({"schema":1,"version":first}), encoding="utf-8")
        before = self.manager.legacy_active_file.read_bytes()
        self.assertEqual(self.manager.status()["active"], first)
        self.manager.activate(second, replace=True)
        self.assertEqual(self.manager.status()["active"], second)
        self.assertEqual(self.manager.legacy_active_file.read_bytes(), before)

    def test_fetch_targets_fixed_revision_not_moving_branch(self):
        with patch.object(nature, "_run_git_owned") as run:
            target=self.manager.fetch()
        self.assertEqual(run.call_count, 2)
        command=run.call_args_list[1].args[0]
        self.assertIn("--depth=1", command);self.assertEqual(command[-1], nature.PIN)
        self.assertEqual(target.parent, self.manager.home / "downloads")

    def test_actual_fixed_revision_fetch_from_isolated_git_remote(self):
        with patch.object(nature,'UPSTREAM',str(self.repo)):
            fetched=self.manager.fetch()
            version=self.manager.install(fetched)
        self.assertTrue((self.manager.versions/version/'skills/nature-writing/SKILL.md').is_file())

    def test_fetch_timeout_terminates_only_owned_process_tree(self):
        process=Mock(pid=43210)
        process.wait.side_effect=[subprocess.TimeoutExpired('git',1), 0]
        with patch.object(nature.subprocess,'Popen',return_value=process) as start, patch.object(nature.subprocess,'run') as run, patch.object(nature.os,'killpg',create=True) as killpg:
            with self.assertRaisesRegex(nature.NaturePackageError,'timed out'):
                nature._run_git_owned(['git','fetch'],timeout=1)
        self.assertEqual(start.call_args.kwargs['stdout'], subprocess.DEVNULL)
        if os.name == 'nt':
            self.assertEqual(run.call_args.args[0],['taskkill','/PID','43210','/T','/F'])
            killpg.assert_not_called()
        else:
            killpg.assert_called_once_with(43210,nature.signal.SIGKILL)


if __name__ == "__main__": unittest.main()
