"""Build a source-only Windows preview from an already audited export manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build(source: Path, destination: Path) -> dict:
    source = source.absolute()
    destination = destination.absolute()
    export_root = ROOT / ".tmp/public-export"
    output_root = ROOT / ".tmp/releases"
    if source.parent != export_root or destination.parent != output_root or not re.fullmatch(r"[A-Za-z0-9_.-]+\.zip", destination.name):
        raise ValueError("Use a direct audited export and a new .tmp/releases ZIP")
    if any(path.is_symlink() or getattr(path, "is_junction", lambda: False)() for path in (source, export_root, output_root)):
        raise ValueError("Aliased source or output is not allowed")
    if destination.exists(): raise FileExistsError("Release already exists; nothing was overwritten")
    manifest_path = source / "PUBLIC_EXPORT_MANIFEST.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("schema") != "scholarflow-public-snapshot-v1" or not isinstance(manifest.get("files"), list):
        raise ValueError("Invalid public export manifest")
    files = {}; seen = set()
    for item in manifest["files"]:
        name = item["path"]; relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or name.casefold() in seen:
            raise ValueError("Unsafe or duplicate export path")
        seen.add(name.casefold())
        path = source.joinpath(*relative.parts)
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or path.stat().st_nlink != 1 or not path.resolve().is_relative_to(source):
            raise ValueError("Unsafe export file")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("Export changed after audit: " + name)
        files[name] = data
    files["PUBLIC_EXPORT_MANIFEST.json"] = manifest_bytes
    output_root.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as raw:
        with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted(files.items()):
                info = zipfile.ZipInfo("ScholarFlow/" + name, date_time=(2024, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, data)
    return {"path": str(destination), "files": len(files), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "includes_dependencies": False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.destination), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__": raise SystemExit(main())
