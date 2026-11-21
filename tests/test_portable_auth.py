import json
import tempfile
import unittest
from pathlib import Path

from scholarflow import portable_auth


class PortableAuthTests(unittest.TestCase):
    def setUp(self): self.temp = tempfile.TemporaryDirectory(); self.home = Path(self.temp.name) / "agent"
    def tearDown(self): self.temp.cleanup()

    def test_store_and_status_never_return_the_key_and_preserve_other_providers(self):
        path = portable_auth.auth_file(self.home); path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"other": {"type": "api_key", "key": "synthetic-other"}}), encoding="utf-8")
        key = "synthetic-test-key-12345"
        result = portable_auth.set_deepseek(self.home, key)
        self.assertNotIn(key, json.dumps(result))
        self.assertNotIn(key, json.dumps(portable_auth.status(self.home)))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["other"]["key"], "synthetic-other")
        self.assertTrue(portable_auth.status(self.home)["deepseek_local_file"])
        with self.assertRaisesRegex(ValueError, "exists"): portable_auth.set_deepseek(self.home, "synthetic-replacement")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["deepseek"]["key"], key)

    def test_invalid_credentials_fail_without_writes(self):
        with self.assertRaises(ValueError): portable_auth.set_deepseek(self.home, "invalid space")
        self.assertFalse(self.home.exists())


if __name__ == "__main__": unittest.main()
