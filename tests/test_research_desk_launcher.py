"""Non-destructive launcher preflight against isolated, ephemeral ports."""
import json
import os
import re
import socket
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "open-research-desk.ps1"
NODE_RUNTIME = ROOT / "scripts" / "node_runtime.ps1"


def probe_supported():
    return bool(re.search(r"(?i)\[switch\]\s*\$ProbeOnly", SCRIPT.read_text(encoding="utf-8")))


def bound_socket():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    return sock


class ResearchDeskLauncherTests(unittest.TestCase):
    def test_minimal_and_local_launchers_share_portable_node_discovery(self):
        for name in ("scholarflow-minimal.ps1", "scholarflow-local.ps1"):
            content = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn("node_runtime.ps1", content)
            self.assertIn("Resolve-ScholarFlowNode", content)
            self.assertNotRegex(content, r"(?i)\b[A-Z]:[\\/]+")

    @unittest.skipUnless(os.name == "nt", "PATH ordering fixture is Windows-only")
    def test_pi_runner_finds_compatible_node_from_path_without_local_shortcuts(self):
        listing = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", "@(Get-Command node.exe -All -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source) | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
        )
        parsed = json.loads(listing.stdout or "[]")
        candidates = [parsed] if isinstance(parsed, str) else (parsed or [])
        versions = []
        for candidate in candidates:
            result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=10)
            match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", result.stdout.strip())
            if result.returncode == 0 and match:
                versions.append((tuple(map(int, match.groups())), candidate))
        older = next((path for version, path in versions if version < (22, 19, 0)), None)
        compatible = next((path for version, path in reversed(versions) if version >= (22, 19, 0)), None)
        if not older or not compatible:
            self.skipTest("PATH does not contain both older and compatible Node executables")
        env = dict(os.environ, SCHOLARFLOW_NODE="", PATH=os.pathsep.join([str(Path(older).parent), str(Path(compatible).parent)]))
        runner = ROOT / "packages" / "scholarflow-pi" / "scripts" / "run-with-pinned-node.mjs"
        result = subprocess.run([older, str(runner), "--eval", "process.stdout.write(process.execPath)"], env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.path.normcase(result.stdout.strip()), os.path.normcase(compatible))

    @unittest.skipUnless(os.name == "nt", "PowerShell launcher is Windows-only")
    def test_node_resolver_finds_a_compatible_path_candidate(self):
        # The first PATH entry may be too old. Discover the expected candidate
        # instead of depending on a developer's machine-specific install path.
        listing = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", "@(Get-Command node.exe -All -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source) | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
        )
        parsed = json.loads(listing.stdout or "[]")
        candidates = [parsed] if isinstance(parsed, str) else (parsed or [])
        expected = None
        for candidate in candidates:
            result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=10)
            match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", result.stdout.strip())
            if result.returncode == 0 and match and tuple(map(int, match.groups())) >= (22, 19, 0):
                expected = candidate
                break
        if not expected:
            self.skipTest("No compatible Node executable is available on PATH")
        env = dict(os.environ, SCHOLARFLOW_NODE="")
        command = f". '{NODE_RUNTIME}'; Resolve-ScholarFlowNode"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.path.normcase(result.stdout.strip()), os.path.normcase(expected))

    def test_launcher_has_a_read_only_preflight(self):
        self.assertTrue(probe_supported(), "Launcher must support -ProbeOnly without starting a service")

    @unittest.skipUnless(probe_supported(), "old launcher has no safe preflight")
    def test_free_ports_do_not_start_any_service(self):
        first, second = bound_socket(), bound_socket()
        api_port, web_port = first.getsockname()[1], second.getsockname()[1]
        first.close(); second.close()
        result = subprocess.run(["powershell.exe", "-NoProfile", "-File", str(SCRIPT), "-ProbeOnly", "-ApiPort", str(api_port), "-WebPort", str(web_port)], capture_output=True, text=True, encoding="utf-8", timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "free")
        for port in (api_port, web_port):
            with socket.socket() as check:
                check.settimeout(1)
                self.assertNotEqual(check.connect_ex(("127.0.0.1", port)), 0)

    @unittest.skipUnless(probe_supported(), "old launcher has no safe preflight")
    def test_unrecognized_listener_is_a_conflict_and_is_never_stopped(self):
        with bound_socket() as stranger, bound_socket() as other:
            web_port, api_port = stranger.getsockname()[1], other.getsockname()[1]
            other.close()
            result = subprocess.run(["powershell.exe", "-NoProfile", "-File", str(SCRIPT), "-ProbeOnly", "-ApiPort", str(api_port), "-WebPort", str(web_port)], capture_output=True, text=True, encoding="utf-8", timeout=45)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["status"], "conflict")
            with socket.create_connection(("127.0.0.1", web_port), timeout=2):
                pass


if __name__ == "__main__":
    unittest.main()
