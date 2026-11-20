"""Correlatable diagnostics without prompts, query strings or credentials."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import traceback
from .persistence import file_lock


def record_http_error(data_dir: Path, *, request_id: str, method: str, code: str, status: int, exception: Exception | None = None) -> dict:
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "request_id": request_id, "method": method, "code": re.sub(r"[^A-Za-z0-9_.-]", "_", str(code))[:100], "status": int(status)}
    if exception is not None:
        record["exception_type"] = type(exception).__name__
        record["frames"] = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name} for frame in traceback.extract_tb(exception.__traceback__)[-12:]]
    path = Path(data_dir) / "_observability" / "workbench-errors.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path, timeout_seconds=.2):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except (OSError, TimeoutError):
        pass  # Logging must not change a research operation's outcome.
    return record
