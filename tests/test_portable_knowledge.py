import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scholarflow import portable_knowledge as portable
from scholarflow.web_research import WebResearchService
from scripts import library_knowledge as kb


class PortableKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.home = Path(self.temp.name) / "library"

    def tearDown(self): self.temp.cleanup()

    def test_requires_exact_loopback_and_known_model_without_starting_service(self):
        self.assertFalse(portable.status(self.home)["configured"])
        for endpoint, digest in (("http://192.0.2.1:8081/v1/embeddings", portable.MODEL_SHA256), ("http://127.0.0.1:8081/v1/embeddings", "wrong")):
            with self.assertRaises(ValueError): portable.configure(self.home, endpoint, model_sha256=digest)
        with self.assertRaisesRegex(ValueError, "parity"):
            portable.configure(self.home, "http://127.0.0.1:8082/v1/embeddings", model_sha256=portable.MODEL_SHA256, backend="gpu")
        result = portable.configure(self.home, "http://127.0.0.1:8081/v1/embeddings", model_sha256=portable.MODEL_SHA256)
        self.assertFalse(result["model_server_started"])
        self.assertFalse(portable.status(self.home)["service_health_checked"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            portable.configure(self.home, "http://127.0.0.1:8081/v1/embeddings", model_sha256=portable.MODEL_SHA256)

    def test_portable_runtime_will_not_assume_an_unknown_listener(self):
        with patch.object(kb, "HOME", self.home), patch.dict(os.environ, {"SCHOLARFLOW_PORTABLE_KNOWLEDGE": "1"}):
            with self.assertRaisesRegex(ValueError, "explicitly"): kb.embedding_endpoint()
            portable.configure(self.home, "http://127.0.0.1:8081/v1/embeddings", model_sha256=portable.MODEL_SHA256)
            self.assertEqual(kb.embedding_endpoint(), "http://127.0.0.1:8081/v1/embeddings")

    def test_preexisting_reviewed_gpu_configuration_is_read_without_rewriting(self):
        import json
        self.home.mkdir()
        path = self.home / "embedding-runtime.json"
        original = {"endpoint": "http://127.0.0.1:8082/v1/embeddings", "model_sha256": portable.MODEL_SHA256, "parity_status": "verified", "runtime": "legacy"}
        path.write_text(json.dumps(original), encoding="utf-8")
        before = path.read_bytes()
        self.assertEqual(portable.status(self.home)["backend"], "gpu")
        with patch.object(kb, "HOME", self.home), patch.dict(os.environ, {"SCHOLARFLOW_PORTABLE_KNOWLEDGE": "1"}):
            self.assertEqual(kb.embedding_endpoint(), original["endpoint"])
        self.assertEqual(path.read_bytes(), before)

    def test_web_worker_uses_portable_python_without_launching_a_server(self):
        service = object.__new__(WebResearchService)
        with patch.dict(os.environ, {"SCHOLARFLOW_PORTABLE_KNOWLEDGE": "1"}), patch("scholarflow.web_research.rw.load", return_value={}), patch("scholarflow.web_research.wk.home_for", return_value=self.home), patch("scholarflow.web_research.subprocess.run", return_value=SimpleNamespace(returncode=0, stderr=b"")) as execute:
            with self.assertRaisesRegex(RuntimeError, "Embedding"): service._index_commands(Path("fixture"))
            execute.assert_not_called()
            portable.configure(self.home, "http://127.0.0.1:8081/v1/embeddings", model_sha256=portable.MODEL_SHA256)
            service._index_commands(Path("fixture"))
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(execute.call_args.args[0][0], sys.executable)


if __name__ == "__main__": unittest.main()
