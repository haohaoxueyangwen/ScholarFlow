import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows process ownership contract")
class WindowsProcessParentTests(unittest.TestCase):
    def test_python_venv_launcher_parent_is_identified_without_wmi(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "env"
            subprocess.run([sys.executable, "-m", "venv", str(env)], check=True, capture_output=True, timeout=60)
            python = env / "Scripts/python.exe"
            child = subprocess.Popen([str(python), "-u", "-c", "import os,time;print(os.getpid(),flush=True);time.sleep(30)"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                interpreter = int(child.stdout.readline().strip())
                cmd = ". '" + str(ROOT / "scripts/windows-process-parent.ps1").replace("'", "''") + "'; Get-ScholarFlowParentProcessId -ProcessId " + str(interpreter)
                result = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=20, check=True)
                self.assertEqual(int(result.stdout.strip()), child.pid if interpreter != child.pid else os.getpid())
            finally:
                child.terminate(); child.wait(timeout=10)
                child.stdout.close(); child.stderr.close()


if __name__ == "__main__": unittest.main()
