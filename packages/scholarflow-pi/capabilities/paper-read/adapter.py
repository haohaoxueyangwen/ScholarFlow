"""Capability-owned JSON transport. No imports from the central Pi dispatcher."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from scholarflow.domain_queries import DomainQueryError, ScholarFlowQueryService
from scholarflow.storage import ScholarFlowStore


def dispatch(data_dir: str, operation: str, params: dict) -> dict:
    if not Path(data_dir).is_dir():
        raise DomainQueryError("dependency_unavailable", "Existing data directory required; no directory was created")
    if not isinstance(params, dict):
        raise DomainQueryError("invalid_argument", "params must be an object")
    fields = {"search": {"query", "status", "limit", "offset", "author", "year_from", "year_to"}, "get": {"paper_id"}}
    if operation not in fields or set(params) - fields[operation]:
        raise DomainQueryError("invalid_argument", "Unsupported operation or parameter")
    for key, value in params.items():
        if key in {"limit", "offset", "year_from", "year_to"}:
            if type(value) is not int or (key == "limit" and not 1 <= value <= 100) or (key == "offset" and value < 0) or (key in {"year_from", "year_to"} and not 1000 <= value <= 2100):
                raise DomainQueryError("invalid_argument", f"Invalid integer: {key}")
        elif not isinstance(value, str):
            raise DomainQueryError("invalid_argument", f"Invalid string: {key}")
    if operation == "get" and not params.get("paper_id", "").strip():
        raise DomainQueryError("invalid_argument", "paper_id is required")
    service = ScholarFlowQueryService(ScholarFlowStore(data_dir))
    return service.search_papers(**params) if operation == "search" else service.get_paper(params["paper_id"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--params", required=True)
    parser.add_argument("--request-id", required=True)
    args = parser.parse_args()
    payload = {"protocol_version": 1, "operation": f"paper.{args.operation}", "request_id": args.request_id, "scope": "local-catalog-read"}
    try:
        payload.update(ok=True, data=dispatch(args.data_dir, args.operation, json.loads(args.params)))
    except (DomainQueryError, json.JSONDecodeError) as error:
        payload.update(ok=False, error={"code": getattr(error, "code", "invalid_argument"), "message": str(error)})
    except Exception:
        payload.update(ok=False, error={"code": "dependency_unavailable", "message": "Local paper storage could not be read"})
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
