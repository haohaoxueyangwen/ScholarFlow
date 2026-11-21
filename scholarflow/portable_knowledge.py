"""Explicit local embedding configuration for a portable installation."""
import json
import re
from pathlib import Path

from .persistence import atomic_write_json
from .source_inventory import SourceInventory

MODEL_SHA256 = "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439"
def valid_endpoint(value: str) -> bool:
    match = re.fullmatch(r"http://127\.0\.0\.1:(\d{1,5})/v1/embeddings", value) if isinstance(value, str) else None
    return bool(match and 1 <= int(match.group(1)) <= 65535)


def configured_backend(value: dict) -> str | None:
    """Recognize the exact pre-portable config without rewriting it."""
    backend = value.get("backend")
    if backend is None and value.get("endpoint") == "http://127.0.0.1:8082/v1/embeddings" and value.get("parity_status") == "verified":
        backend = "gpu"
    elif backend is None and value.get("endpoint") == "http://127.0.0.1:8081/v1/embeddings":
        backend = "cpu"
    if not valid_endpoint(value.get("endpoint")) or value.get("model_sha256") != MODEL_SHA256 or backend not in {"cpu", "gpu"}:
        return None
    return None if backend == "gpu" and value.get("parity_status") != "verified" else backend


def configure(library_home: Path, endpoint: str, *, model_sha256: str, backend: str = "cpu", parity_verified: bool = False, replace: bool = False) -> dict:
    home = SourceInventory(library_home).home
    if not valid_endpoint(endpoint) or not isinstance(model_sha256, str) or model_sha256.lower() != MODEL_SHA256 or backend not in {"cpu", "gpu"}:
        raise ValueError("The reviewed loopback endpoint and exact embedding model SHA-256 are required")
    if backend == "gpu" and not parity_verified:
        raise ValueError("GPU embedding requires explicit parity verification")
    path = home / "embedding-runtime.json"
    if path.exists() and not replace:
        raise ValueError("Embedding configuration already exists; explicit replacement required")
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or (path.exists() and path.stat().st_nlink != 1):
        raise ValueError("Unsafe embedding configuration path")
    config = {"endpoint": endpoint, "model_sha256": MODEL_SHA256, "backend": backend, "parity_status": "verified" if parity_verified else "not_asserted", "service_management": "external"}
    atomic_write_json(path, config)
    return {**config, "model_server_started": False, "running_model_identity_verified": False}


def status(library_home: Path) -> dict:
    home = SourceInventory(library_home).home
    path = home / "embedding-runtime.json"
    if not path.exists(): return {"configured": False, "model_server_started": False}
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or path.stat().st_nlink != 1:
        raise ValueError("Unsafe embedding configuration path")
    value = json.loads(path.read_text(encoding="utf-8"))
    backend = configured_backend(value)
    if not backend:
        raise ValueError("Embedding configuration is not compatible with this portable runtime")
    return {"configured": True, "endpoint": value["endpoint"], "backend": backend, "model_sha256": MODEL_SHA256, "model_server_started": False, "service_health_checked": False, "running_model_identity_verified": False}
