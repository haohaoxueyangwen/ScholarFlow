"""Pinned, optional Nature Skills installation for the Pi runtime.

No upstream executable is run. The reviewed upstream Git tree is copied into a
new local version directory and verified before activation and on every use.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from .persistence import atomic_write_json, file_lock

UPSTREAM = "https://github.com/Yuan1z0825/nature-skills.git"
PIN = "1ea82ffff20f40077bf84b74182f55eeaf3d111d"
EXPECTED_FILES = 91
EXPECTED_TREE = "dcc03c04f50aa126020436f079abdedf7592d470306f53fa6b403039a9ec571b"
MAX_ARCHIVE = 30_000_000


class NaturePackageError(ValueError):
    pass


def tree_hash(entries: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _alias(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _canonical(path: Path) -> Path:
    path = Path(path).absolute()
    if path.resolve() != path or any(_alias(item) for item in (path, *path.parents)):
        raise NaturePackageError("A canonical local directory is required")
    return path


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"}


def _archive(checkout: Path) -> dict[str, bytes]:
    checkout = _canonical(checkout)
    if not checkout.is_dir():
        raise NaturePackageError("Pinned upstream checkout is unavailable")
    try:
        # Only these tracked paths are copied; uncommitted and untracked files are ignored.
        result = subprocess.run(["git", "-c", "safe.directory=" + checkout.as_posix(), "-C", str(checkout), "archive", "--format=tar", PIN, "--", "LICENSE", "skills/nature-writing", "skills/nature-shared"],
                                env=_git_env(), capture_output=True, timeout=55, check=True)
        if len(result.stdout) > MAX_ARCHIVE:
            raise NaturePackageError("Upstream archive exceeds the size budget")
        entries: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            for member in archive:
                if member.isdir():
                    continue
                path = PurePosixPath(member.name)
                if not member.isfile() or path.is_absolute() or ".." in path.parts or member.size > 5_000_000 or member.name in entries:
                    raise NaturePackageError("Unsafe upstream archive entry")
                if member.name != "LICENSE" and not (member.name.startswith("skills/nature-writing/") or member.name.startswith("skills/nature-shared/")):
                    raise NaturePackageError("Unexpected upstream path")
                stream = archive.extractfile(member)
                if stream is None:
                    raise NaturePackageError("Unreadable upstream entry")
                entries[member.name] = stream.read().replace(b"\r\n", b"\n")
        hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in entries.items()}
        if len(hashes) != EXPECTED_FILES or tree_hash(hashes) != EXPECTED_TREE:
            raise NaturePackageError("Upstream content differs from the reviewed pin")
        return entries
    except (OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        raise NaturePackageError("Could not read the pinned upstream Git tree") from error


class NatureSkillPackage:
    def __init__(self, home: Path):
        self.home = _canonical(home)
        self.base = self.home / "skills" / "nature-skills"
        self.versions = self.base / "versions"
        self.active_file = self.base / "active.json"

    def install(self, checkout: Path) -> str:
        entries = _archive(checkout)
        self.versions.mkdir(parents=True, exist_ok=True)
        if _canonical(self.versions) != self.versions:
            raise NaturePackageError("Unsafe skill destination")
        version = "v-" + PIN[:12] + "-" + secrets.token_hex(6)
        target = self.versions / version
        target.mkdir()
        for name, data in sorted(entries.items()):
            path = target / Path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as output:
                output.write(data)
        atomic_write_json(target / "manifest.json", {"schema": 1, "commit": PIN, "tree_sha256": EXPECTED_TREE,
                          "files": {name: hashlib.sha256(data).hexdigest() for name, data in entries.items()}, "upstream": UPSTREAM})
        self._validate(version)
        return version

    def _validate(self, version: str) -> dict:
        if not isinstance(version, str) or not re.fullmatch(r"v-[0-9a-f]{12}-[0-9a-f]{12}", version):
            raise NaturePackageError("Invalid installed version")
        target = _canonical(self.versions / version)
        try:
            manifest_path = target / "manifest.json"
            if _alias(manifest_path) or manifest_path.stat().st_nlink != 1:
                raise NaturePackageError("Unsafe skill manifest")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected = manifest.get("files")
            if manifest.get("schema") != 1 or manifest.get("commit") != PIN or manifest.get("tree_sha256") != EXPECTED_TREE or not isinstance(expected, dict) or len(expected) != EXPECTED_FILES or tree_hash(expected) != EXPECTED_TREE:
                raise NaturePackageError("Skill manifest differs from the reviewed version")
            found = {}
            for current, dirs, files in os.walk(target, followlinks=False):
                directory = Path(current)
                if any(_alias(directory / name) for name in dirs):
                    raise NaturePackageError("Skill directory contains an alias")
                for name in files:
                    file = directory / name
                    relative = file.relative_to(target).as_posix()
                    if relative == "manifest.json":
                        continue
                    if _alias(file) or file.stat().st_nlink != 1 or file.stat().st_size > 5_000_000:
                        raise NaturePackageError("Unsafe installed skill file")
                    found[relative] = hashlib.sha256(file.read_bytes()).hexdigest()
            if found != expected:
                raise NaturePackageError("Installed Nature Skill content has changed")
            return {"active": version, "directory": str(target), "commit": PIN, "skill_paths": [str(target / "skills/nature-writing/SKILL.md")],
                    "resources": [{"path": str(target / Path(name)), "sha256": digest} for name, digest in sorted(expected.items())]}
        except (OSError, ValueError, TypeError) as error:
            if isinstance(error, NaturePackageError): raise
            raise NaturePackageError("Installed Nature Skill is unreadable") from error

    def activate(self, version: str, *, replace: bool = False) -> dict:
        self.base.mkdir(parents=True, exist_ok=True)
        with file_lock(self.base / "activation"):
            previous = self.status()
            if previous["active"] and previous["active"] != version and not replace:
                raise NaturePackageError("An active version already exists; explicit replacement is required")
            value = self._validate(version)
            atomic_write_json(self.active_file, {"schema": 1, "version": version})
            value.pop("resources")
            return value

    def status(self, *, include_resources: bool = False) -> dict:
        if not self.active_file.exists(): return {"active": None, "skill_paths": []}
        try:
            if _alias(self.active_file) or self.active_file.stat().st_nlink != 1:
                raise NaturePackageError("Unsafe activation record")
            state = json.loads(self.active_file.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or set(state) != {"schema", "version"} or state["schema"] != 1:
                raise NaturePackageError("Invalid activation record")
            result = self._validate(state["version"])
            if not include_resources: result.pop("resources")
            return result
        except (OSError, ValueError, TypeError) as error:
            if isinstance(error, NaturePackageError): raise
            raise NaturePackageError("Nature activation is unreadable") from error

    def fetch(self) -> Path:
        downloads = self.home / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        _canonical(downloads)
        target = downloads / ("nature-upstream-" + secrets.token_hex(8))
        try:
            subprocess.run(["git", "clone", "--no-checkout", "--", UPSTREAM, str(target)], env=_git_env(), capture_output=True, timeout=55, check=True)
        except (OSError, subprocess.SubprocessError) as error:
            raise NaturePackageError("Could not fetch upstream; no active skill was changed") from error
        return target

    def check_upstream(self) -> dict:
        try:
            result = subprocess.run(["git", "ls-remote", "--heads", UPSTREAM, "refs/heads/main"], env=_git_env(), capture_output=True, text=True, timeout=30, check=True)
        except (OSError, subprocess.SubprocessError) as error:
            raise NaturePackageError("Could not check the upstream revision; active skill was not changed") from error
        fields = result.stdout.strip().split()
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{40}", fields[0]) or fields[1] != "refs/heads/main":
            raise NaturePackageError("Unexpected upstream revision response")
        return {"reviewed_commit": PIN, "upstream_main": fields[0], "update_available": fields[0] != PIN, "active_version_changed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Install and validate the optional pinned Nature Skill")
    parser.add_argument("action", choices=["status", "install", "activate", "check"])
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--version")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--resources", action="store_true", help="include verified read-only resource paths")
    args = parser.parse_args()
    try:
        manager = NatureSkillPackage(args.home)
        if args.action == "status": result = manager.status(include_resources=args.resources)
        elif args.action == "activate": result = manager.activate(args.version, replace=args.replace)
        elif args.action == "check": result = manager.check_upstream()
        else:
            if bool(args.checkout) == bool(args.fetch): raise NaturePackageError("Choose exactly one: --checkout or --fetch")
            result = {"installed": manager.install(manager.fetch() if args.fetch else args.checkout), "activated": False}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except NaturePackageError as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__": raise SystemExit(main())
