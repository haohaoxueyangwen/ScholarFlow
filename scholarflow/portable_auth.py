"""User-local Pi provider credentials; never print or send them here."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .persistence import atomic_write_json, file_lock
from .source_inventory import SourceInventory


def auth_file(home: Path) -> Path:
    directory = SourceInventory(home).home / "pi-auth"
    if directory.resolve() != directory or any(item.is_symlink() or getattr(item, "is_junction", lambda: False)() for item in (directory, *directory.parents)):
        raise ValueError("Unsafe Pi credential directory")
    return directory / "auth.json"


def _load(path: Path) -> dict:
    if not path.exists(): return {}
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or path.stat().st_nlink != 1:
        raise ValueError("Unsafe Pi credential file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("Pi credential file is unreadable") from error
    if not isinstance(value, dict): raise ValueError("Invalid Pi credential file")
    return value


def set_deepseek(home: Path, key: str, *, replace: bool = False) -> dict:
    path = auth_file(home)
    if not isinstance(key, str) or key != key.strip() or not 8 <= len(key) <= 4096 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ValueError("Invalid API key format; no credential was saved")
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.parent / "credentials"):
        values = _load(path)
        if "deepseek" in values and not replace: raise ValueError("DeepSeek credential exists; explicit --replace required")
        values["deepseek"] = {"type": "api_key", "key": key}
        atomic_write_json(path, values)
        if os.name != "nt": os.chmod(path, 0o600)
    return {"provider": "deepseek", "configured": True, "location": str(path), "plaintext_on_disk": True, "connection_tested": False}


def status(home: Path) -> dict:
    path = auth_file(home)
    local = _load(path).get("deepseek", {})
    return {"deepseek_environment": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
            "deepseek_local_file": bool(isinstance(local, dict) and local.get("type") == "api_key" and local.get("key")),
            "connection_tested": False, "plaintext_on_disk": bool(path.exists())}
