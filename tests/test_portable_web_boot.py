import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("portable_agent_cli", ROOT / "scripts/scholarflow-agent.py")
cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)


@unittest.skipUnless(os.name == "nt", "The optional desktop launcher is Windows-only")
class PortableWebBootTests(unittest.TestCase):
    def setUp(self): self.temp = tempfile.TemporaryDirectory(); self.home = Path(self.temp.name) / "agent"
    def tearDown(self): self.temp.cleanup()

    def invoke(self, *extra):
        args = cli.build_parser().parse_args(["--home", str(self.home), *extra])
        with patch.dict(os.environ, {}, clear=False), patch.object(cli.subprocess, "call", return_value=0) as call:
            result = cli.run(args)
            self.assertEqual(result, 0)
            self.assertEqual(call.call_count, 1)

    def test_fresh_web_start_creates_empty_data_root_only_when_started(self):
        self.invoke("web", "--probe-only")
        self.assertFalse(self.home.exists())
        self.invoke("web", "--no-browser")
        facts = self.home / "library/facts"
        self.assertTrue(facts.is_dir())
        self.assertEqual(list(facts.iterdir()), [])

    def test_explicit_missing_legacy_library_is_not_created(self):
        missing = Path(self.temp.name) / "do-not-create"
        args = cli.build_parser().parse_args(["--home", str(self.home), "--library-home", str(missing), "web", "--no-browser"])
        with patch.dict(os.environ, {}, clear=False), patch.object(cli.subprocess, "call") as call:
            with self.assertRaisesRegex(ValueError, "must already contain"): cli.run(args)
            call.assert_not_called()
        self.assertFalse(missing.exists())


if __name__ == "__main__": unittest.main()
