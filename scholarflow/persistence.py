"""Small, dependency-free persistence primitives for local ScholarFlow state."""

from __future__ import annotations

import json
import os
import time
import secrets
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterator


class PersistenceLockTimeout(TimeoutError):
    pass


@contextmanager
def file_lock(path: Path, *, timeout_seconds: float = 10.0, stale_seconds: float = 120.0) -> Iterator[None]:
    """Acquire a cross-process lock using an atomically-created directory."""

    lock_dir = Path(f"{path}.lock")
    owner = f"pid={os.getpid()}\ntoken={secrets.token_hex(12)}\n"
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            lock_dir.mkdir(parents=False)
            try:
                (lock_dir / "owner").write_text(owner, encoding="ascii")
            except OSError:
                _remove_lock_dir(lock_dir)
                raise
            break
        except (FileExistsError, PermissionError) as error:
            # Windows may report ERROR_ACCESS_DENIED instead of
            # ERROR_ALREADY_EXISTS while another thread creates or removes the
            # lock directory. Treat it as contention while the parent remains
            # accessible, but preserve a real permission failure at timeout.
            try:
                age = time.time() - lock_dir.stat().st_mtime
                if age > stale_seconds and not _lock_owner_alive(lock_dir):
                    if _remove_lock_dir(lock_dir):
                        continue
            except OSError:
                # The directory may have disappeared or may still be in a
                # transient Windows sharing state. The bounded retry below
                # handles both without spinning forever.
                pass
            if time.monotonic() >= deadline:
                raise PersistenceLockTimeout(f"timed out acquiring persistence lock for '{path}'") from error
            time.sleep(0.01)
    try:
        yield
    finally:
        cleanup_deadline = time.monotonic() + min(1.0, timeout_seconds)
        while True:
            try:
                if (lock_dir / "owner").read_text(encoding="ascii") != owner:
                    break
            except FileNotFoundError:
                break
            if _remove_lock_dir(lock_dir):
                break
            if time.monotonic() >= cleanup_deadline:
                break
            time.sleep(0.01)


def _lock_owner_alive(lock_dir: Path) -> bool:
    """Never steal a slow parser's lock just because it is old."""
    try:
        first = (lock_dir / "owner").read_text(encoding="ascii").splitlines()[0]
        pid = int(first.removeprefix("pid="))
    except FileNotFoundError:
        return False
    except (OSError, ValueError, IndexError):
        return True  # Unknown ownership fails closed.
    if pid == os.getpid():
        return True
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _remove_lock_dir(lock_dir: Path) -> bool:
    """Best-effort lock cleanup that tolerates transient Windows sharing errors."""

    try:
        for child in lock_dir.iterdir():
            child.unlink(missing_ok=True)
        lock_dir.rmdir()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Durably replace a text file without exposing a partially-written file."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            newline="",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
