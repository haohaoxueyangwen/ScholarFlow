import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import build_portable_agent as builder


class PortableBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.export = self.root / ".tmp/public-export/candidate"; self.export.mkdir(parents=True)
        self.destination = self.root / ".tmp/releases/preview.zip"
        self.text = b"synthetic source\n"; (self.export / "README.md").write_bytes(self.text)
        manifest = {"schema": "scholarflow-public-snapshot-v1", "files": [{"path": "README.md", "sha256": hashlib.sha256(self.text).hexdigest()}]}
        (self.export / "PUBLIC_EXPORT_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.patch = patch.object(builder, "ROOT", self.root); self.patch.start()

    def tearDown(self): self.patch.stop(); self.temp.cleanup()

    def test_bundle_only_contains_reviewed_manifest_files(self):
        (self.export / "private.txt").write_text("must not package", encoding="utf-8")
        result = builder.build(self.export, self.destination)
        self.assertFalse(result["includes_dependencies"])
        with zipfile.ZipFile(self.destination) as archive:
            self.assertEqual(set(archive.namelist()), {"ScholarFlow/README.md", "ScholarFlow/PUBLIC_EXPORT_MANIFEST.json"})
            self.assertEqual(archive.read("ScholarFlow/README.md"), self.text)
        with self.assertRaises(FileExistsError): builder.build(self.export, self.destination)

    def test_changed_source_is_not_packaged(self):
        (self.export / "README.md").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"): builder.build(self.export, self.destination)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__": unittest.main()
