"""Trusted CLI transport over the shared facade. stdout contains only JSON."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    parser.add_argument("--audit-path")
    authorization = parser.add_mutually_exclusive_group(required=True)
    authorization.add_argument("--grant-json")
    authorization.add_argument("--grant-stdin", action="store_true")
    parser.add_argument("--request-json")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    request = None
    try:
        if args.request_json:
            request = json.loads(args.request_json)
        from scholarflow.read_operations import ReadGrant
        from scholarflow.read_operation_registry import build_read_facade
        grant = ReadGrant.from_dict(json.load(sys.stdin) if args.grant_stdin else json.loads(args.grant_json))
        facade = build_read_facade(args.data_dir, args.audit_path, grant.capabilities)
        if args.status:
            print(json.dumps(facade.describe(grant), ensure_ascii=False))
            return 0
        if args.grant_stdin and not args.request_json:
            raise ValueError("A stdin grant requires an explicit request or status mode")
        request = request if args.request_json else json.load(sys.stdin)
        response = facade.execute(request, grant)
    except (ImportError, OSError):
        response = {"protocol_version": 1, "ok": False, "status": "error", "error": {"code": "dependency_unavailable", "message": "Read capability dependency is unavailable", "retryable": True}, "receipt": {"execution": "not_started", "audit": {"persisted": False}}}
    except ValueError:
        response = {"protocol_version": 1, "ok": False, "status": "error", "error": {"code": "invalid_argument", "message": "Invalid trusted transport configuration or JSON input", "retryable": False}, "receipt": {"execution": "not_started", "audit": {"persisted": False}}}
    if "request_id" not in response:
        identity = request if isinstance(request, dict) else {}
        response.update({key: identity.get(key) for key in ("request_id", "task_id", "operation", "scope")})
        response["source_versions"] = []
        response["receipt"].update({key: response[key] for key in ("request_id", "task_id", "operation")})
        response["receipt"].update(read_only=True, outcome="error")
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
