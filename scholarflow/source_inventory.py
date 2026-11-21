"""Explicit, project-scoped source discovery; never invokes a model or writes sources."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterable

from .persistence import atomic_write_json, file_lock
from .storage import ScholarFlowStore


class InventoryError(ValueError):
    """A source registry or scan cannot be safely completed."""


CONFIG_SCHEMA = "scholarflow-source-roots-v1"
INVENTORY_SCHEMA = "scholarflow-configured-inventory-v1"
KINDS = {"md", "pdf"}
SKIP_DIRECTORIES = {".git", ".obsidian", "node_modules", "__pycache__", "venv", ".venv", "results", "outputs", "output", "checkpoints", "weights", "benchmark", "build", "dist"}
CODE_MARKERS = {".git", "pyproject.toml", "package.json", "cmakelists.txt", "cargo.toml", "skill.md"}
SENSITIVE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}|(?i:Bearer)\s+[A-Za-z0-9_.-]{24,}|(?i:api[_-]?key|password|client_secret)\s*[:=]\s*['\"]?[A-Za-z0-9_-]{20,}")


def _is_alias(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _directory(path: Path) -> Path:
    path = Path(path).absolute()
    try:
        if any(_is_alias(part) for part in (path, *path.parents)) or path.resolve(strict=True) != path or not path.is_dir():
            raise InventoryError("A canonical, existing source directory is required")
        if path == path.parent:
            raise InventoryError("A whole drive cannot be registered as a source")
    except OSError as error:
        raise InventoryError("Source directory is unavailable") from error
    return path


def _read_source(path: Path, root: Path, limit: int) -> bytes:
    if any(_is_alias(part) for part in (path, *path.parents) if part == root or root in part.parents):
        raise InventoryError("Source path traverses an alias")
    before = path.lstat()
    if _is_alias(path) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise InventoryError("Source file is not a regular, single-link file")
    if not path.resolve(strict=True).is_relative_to(root):
        raise InventoryError("Source path escapes its authorized root")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or opened.st_nlink != 1:
            raise InventoryError("Source changed while opening")
        data = stream.read(limit + 1)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or after.st_nlink != 1 or not path.resolve(strict=True).is_relative_to(root):
        raise InventoryError("Source changed during inventory")
    if len(data) > limit:
        raise InventoryError("Source exceeds its file-size budget")
    return data


def _valid_project(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"stage-[A-Za-z0-9_-]{1,160}", value)) and not value.startswith("stage-library-")


class SourceInventory:
    """Manage explicitly registered roots and a separate derived inventory."""

    def __init__(self, home: Path):
        self.home = Path(home).absolute()
        if any(_is_alias(part) for part in (self.home, *self.home.parents)) or self.home.resolve() != self.home:
            raise InventoryError("A canonical library home is required")
        self.config_path = self.home / "source-roots.json"
        self.manifest_path = self.home / "configured-inventory.json"

    def _config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {"schema_version": CONFIG_SCHEMA, "roots": []}
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise InventoryError("Source configuration is unreadable") from error
        if not isinstance(config, dict) or set(config) != {"schema_version", "roots"} or config["schema_version"] != CONFIG_SCHEMA or not isinstance(config["roots"], list) or len(config["roots"]) > 100:
            raise InventoryError("Invalid source configuration")
        ids, paths = set(), []
        for item in config["roots"]:
            if not isinstance(item, dict) or set(item) != {"id", "path", "project_stage_id", "kinds", "auto_scan"}:
                raise InventoryError("Invalid source root entry")
            if not isinstance(item["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item["id"]) or item["id"].casefold() in ids:
                raise InventoryError("Duplicate or invalid source id")
            if not _valid_project(item["project_stage_id"]) or not isinstance(item["path"], str) or not Path(item["path"]).is_absolute() or type(item["auto_scan"]) is not bool:
                raise InventoryError("Invalid source scope")
            kinds = item["kinds"]
            if not isinstance(kinds, list) or not kinds or any(not isinstance(kind, str) or kind not in KINDS for kind in kinds) or len(set(kinds)) != len(kinds):
                raise InventoryError("Only explicit PDF and Markdown source types are supported")
            root = Path(os.path.normcase(item["path"]))
            home = Path(os.path.normcase(str(self.home)))
            if root == home or root in home.parents or home in root.parents or any(root == old or root in old.parents or old in root.parents for old in paths):
                raise InventoryError("Source roots and library data must not overlap")
            ids.add(item["id"].casefold()); paths.append(root)
        return config

    def _check_project(self, project: str) -> None:
        if not _valid_project(project) or not (self.home / "facts").is_dir():
            raise InventoryError("An existing exact research project is required")
        if ScholarFlowStore(self.home / "facts").get_project_stage(project) is None:
            raise InventoryError("An existing exact research project is required")

    def register(self, root: Path, *, project: str, source_id: str, kinds: Iterable[str], auto_scan: bool = False) -> dict[str, Any]:
        directory = _directory(root)
        home = Path(os.path.normcase(str(self.home)))
        normalized = Path(os.path.normcase(str(directory)))
        if normalized == home or normalized in home.parents or home in normalized.parents:
            raise InventoryError("Source roots and library data must not overlap")
        self._check_project(project)
        kinds = sorted(set(kinds))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", source_id or "") or not kinds or not set(kinds) <= KINDS or type(auto_scan) is not bool:
            raise InventoryError("Valid source id, types and automatic-scan choice are required")
        entry = {"id": source_id, "path": str(directory), "project_stage_id": project, "kinds": kinds, "auto_scan": auto_scan}
        self.home.mkdir(parents=True, exist_ok=True)
        with file_lock(self.home / "source-registry"):
            config = self._config()
            for previous in config["roots"]:
                if previous == entry:
                    return {"status": "already_registered", "root": entry}
                existing = Path(previous["path"])
                if previous["id"].casefold() == source_id.casefold() or directory == existing or directory in existing.parents or existing in directory.parents:
                    raise InventoryError("Source ids and authorized roots must not overlap")
            config["roots"].append(entry)
            config["roots"].sort(key=lambda item: item["id"])
            atomic_write_json(self.config_path, config)
        return {"status": "registered", "root": entry}

    @staticmethod
    def for_project(records: Iterable[dict[str, Any]], project: str) -> list[dict[str, Any]]:
        if not _valid_project(project):
            raise InventoryError("An exact project is required")
        return [item for item in records if project in item.get("project_stage_ids", [])]

    def authorized_records(self, records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Recheck current grants before using a previously saved manifest."""
        roots = {item["id"]: item for item in self._config()["roots"]}
        result = []
        for record in records:
            entry = roots.get(record.get("group"))
            if not entry or record.get("project_stage_ids") != [entry["project_stage_id"]]:
                continue
            self._check_project(entry["project_stage_id"])
            root = _directory(Path(entry["path"]))
            relative = record.get("relative_path")
            if not isinstance(relative, str):
                continue
            path = Path(relative)
            if path.is_absolute() or not path.parts or ".." in path.parts:
                continue
            expected_id = "source-" + hashlib.sha256((entry["id"] + "\0" + path.as_posix()).encode("utf-8")).hexdigest()[:24]
            if (record.get("inventory_mode") != "configured" or record.get("source_id") != expected_id or record.get("stage_id") != "stage-library-" + entry["id"]
                    or record.get("path") != str(root / path) or record.get("kind") not in entry["kinds"]
                    or path.suffix.lower().lstrip(".") != record.get("kind") or not isinstance(record.get("sha256"), str)
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", record["sha256"])):
                continue
            result.append(record)
        return result

    def scan(self, *, max_files: int = 5000, max_total_bytes: int = 1_000_000_000, auto_only: bool = False) -> dict[str, Any]:
        if not isinstance(max_files, int) or not 1 <= max_files <= 5000 or not isinstance(max_total_bytes, int) or not 1 <= max_total_bytes <= 1_000_000_000:
            raise InventoryError("Invalid inventory budget")
        if type(auto_only) is not bool:
            raise InventoryError("Invalid automatic-scan choice")
        if not self.home.is_dir():
            raise InventoryError("No source roots are configured")
        with file_lock(self.home / "source-registry"):
            config = self._config()
            if not config["roots"]:
                raise InventoryError("No source roots are configured")
            previous: dict[str, Any] = {}
            if self.manifest_path.exists():
                try:
                    previous = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                    if previous.get("schema_version") != INVENTORY_SCHEMA or not isinstance(previous.get("records"), list):
                        raise ValueError("invalid inventory")
                except (OSError, ValueError, AttributeError) as error:
                    raise InventoryError("Existing source inventory is unreadable") from error
            selected = [entry for entry in config["roots"] if not auto_only or entry["auto_scan"]]
            if not selected:
                return {"status": "no_automatic_roots", "records": [], "counts": {}, "model_calls": 0, "source_files_modified": False}
            selected_ids = {entry["id"] for entry in selected}
            records = [dict(item) for item in self.authorized_records([item for item in previous.get("records", []) if item.get("group") not in selected_ids])] if auto_only else []
            excluded = 0
            visited = 0
            visited_dirs = 0
            consumed = 0
            seen: dict[tuple[str, str, str], str] = {(item["project_stage_ids"][0], item["kind"], item["sha256"]): item["source_id"] for item in records if not item.get("duplicate_of")}
            try:
                for entry in selected:
                    root = _directory(Path(entry["path"]))
                    self._check_project(entry["project_stage_id"])
                    if any(child.name.casefold() in CODE_MARKERS for child in root.iterdir()):
                        raise InventoryError("Source root is a code or skill directory")
                    def onerror(error: OSError) -> None:
                        raise error
                    for current, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
                        visited_dirs += 1
                        if visited_dirs > 10000:
                            raise InventoryError("Inventory directory budget exceeded")
                        directory = Path(current)
                        kept = []
                        for name in sorted(dirs):
                            path = directory / name
                            if name.startswith(".") or name.casefold() in SKIP_DIRECTORIES or _is_alias(path):
                                excluded += 1
                            else:
                                kept.append(name)
                        dirs[:] = kept
                        if directory != root and any(name.casefold() in CODE_MARKERS for name in (*dirs, *files)):
                            excluded += 1; dirs[:] = []; continue
                        for name in sorted(files):
                            visited += 1
                            if visited > max_files:
                                raise InventoryError("Inventory file budget exceeded")
                            path = directory / name
                            kind = path.suffix.lower().lstrip(".")
                            if name.startswith(".") or kind not in entry["kinds"] or _is_alias(path):
                                excluded += 1; continue
                            info = path.lstat()
                            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                                excluded += 1; continue
                            limit = 80_000_000 if kind == "pdf" else 2_000_000
                            if info.st_size > limit:
                                excluded += 1; continue
                            raw = _read_source(path, root, limit)
                            consumed += len(raw)
                            if consumed > max_total_bytes:
                                raise InventoryError("Inventory byte budget exceeded")
                            if kind == "md":
                                try:
                                    content = raw.decode("utf-8-sig")
                                except UnicodeDecodeError:
                                    excluded += 1; continue
                                if SENSITIVE.search(content):
                                    excluded += 1; continue
                            relative = path.relative_to(root).as_posix()
                            source = "source-" + hashlib.sha256((entry["id"] + "\0" + relative).encode("utf-8")).hexdigest()[:24]
                            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
                            duplicate_key = (entry["project_stage_id"], kind, digest)
                            duplicate = seen.get(duplicate_key); seen.setdefault(duplicate_key, source)
                            records.append({"source_id": source, "group": entry["id"], "stage_id": "stage-library-" + entry["id"], "inventory_mode": "configured",
                                "project_stage_ids": [entry["project_stage_id"]], "path": str(path), "relative_path": relative,
                                "kind": kind, "bytes": len(raw), "sha256": digest, "duplicate_of": duplicate,
                                "reading_state": "pending_pi_read" if kind == "pdf" else "existing_note",
                                "evidence_level": "original_pdf" if kind == "pdf" else "existing_note_not_original_paper"})
            except OSError as error:
                raise InventoryError("Source inventory could not be completed; previous inventory was retained") from error
            records.sort(key=lambda item: item["source_id"])
            # Recompute after partial automatic scans: a removed primary must
            # not leave a retained source pointing at a missing duplicate.
            seen = {}
            for item in records:
                key = (item["project_stage_ids"][0], item["kind"], item["sha256"])
                item["duplicate_of"] = seen.get(key)
                seen.setdefault(key, item["source_id"])
            old = {item["source_id"]: item for item in previous.get("records", [])}
            new = {item["source_id"]: item for item in records}
            counts = {"added": len(new.keys() - old.keys()), "missing": len(old.keys() - new.keys()),
                      "changed": sum(old[key] != new[key] for key in new.keys() & old.keys()),
                      "unchanged": sum(old[key] == new[key] for key in new.keys() & old.keys()), "excluded": excluded}
            payload = {"schema_version": INVENTORY_SCHEMA, "source_mode": "configured", "roots": config["roots"], "records": records}
            if payload != previous:
                atomic_write_json(self.manifest_path, payload)
            return {**payload, "counts": counts, "model_calls": 0, "source_files_modified": False}
