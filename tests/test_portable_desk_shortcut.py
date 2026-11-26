"""Windows shortcut creation uses only a disposable desktop fixture."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/install-reading-desk-shortcut.ps1"


@unittest.skipUnless(os.name == "nt", "Windows shortcut integration")
class PortableDeskShortcutTest(unittest.TestCase):
    def test_explicit_home_idempotency_and_unrelated_shortcut_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            desktop = base / "Desktop"
            desktop.mkdir()
            home = base / "AgentHome"
            other = base / "OtherHome"
            for item in (home, other):
                (item / "bin").mkdir(parents=True)
                (item / "bin/scholarflow.cmd").write_text("@exit /b 0\n", encoding="utf-8")

            def invoke(selected):
                return subprocess.run(["powershell.exe", "-NoProfile", "-File", str(SCRIPT),
                                       "-AgentHome", str(selected), "-DesktopPath", str(desktop)],
                                      capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)

            first = invoke(home)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue(json.loads(first.stdout)["changed"])
            second = invoke(home)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertFalse(json.loads(second.stdout)["changed"])
            conflict = invoke(other)
            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn("nothing overwritten", conflict.stderr)
            self.assertEqual(invoke(home).returncode, 0)


if __name__ == "__main__":
    unittest.main()
