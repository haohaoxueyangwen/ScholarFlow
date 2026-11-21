import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from scripts import library_knowledge as kb


class LibraryUnicodeStateTests(unittest.TestCase):
    def test_query_reads_utf8_state_without_relying_on_windows_locale(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / "active-sources.json"
            state.write_text(json.dumps({"entries": {}, "label": "中文"}, ensure_ascii=False), encoding="utf-8")
            real_read = Path.read_text
            def read_with_explicit_encoding(path, *args, **kwargs):
                if path == state and kwargs.get("encoding") != "utf-8": raise AssertionError("state requires explicit UTF-8")
                return real_read(path, *args, **kwargs)
            rag = SimpleNamespace(aquery_data=AsyncMock(return_value={"data": {"chunks": []}}), finalize_storages=AsyncMock())
            fake_lightrag = SimpleNamespace(QueryParam=lambda **kwargs: kwargs)
            with patch.dict(sys.modules, {"lightrag": fake_lightrag}), patch.object(Path, "read_text", read_with_explicit_encoding), patch.object(kb, "HOME", home), patch.object(kb, "validate_stage"), patch.object(kb, "state_path", return_value=state), patch.object(kb, "scoped_records", return_value=[{"source_id": "fixture"}]), patch.object(kb, "binding_hash", return_value=None), patch.object(kb, "rag_for", new=AsyncMock(return_value=rag)):
                result = asyncio.run(kb.query("stage-test", "synthetic query"))
            self.assertEqual(result["status"], "no_current_evidence")


if __name__ == "__main__": unittest.main()
