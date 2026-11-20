"""Projection CLI. --operator cannot be supplied through model JSON."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")


def main():
    from operations import ProjectionFacade, ProjectionGrant
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--grant-file", type=Path, required=True)
    parser.add_argument("--request-json")
    parser.add_argument("--operator", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    request = None
    try:
        raw_grant=args.grant_file.read_bytes()
        grant = ProjectionGrant.from_dict(json.loads(raw_grant.decode("utf-8-sig")))
        def grant_guard(_target):
            from scholarflow.read_operations import ReadOperationError
            try:
                current=args.grant_file.read_bytes()
            except OSError:
                raise ReadOperationError("permission_denied","Host projection grant revoked")
            if current!=raw_grant:
                raise ReadOperationError("permission_denied","Host projection grant changed; remaining writes stopped")
        facade = ProjectionFacade(args.data_dir,grant_guard=grant_guard)
        if args.status:
            print(json.dumps(facade.describe(grant), ensure_ascii=False))
            return 0
        request = json.loads(args.request_json) if args.request_json else json.load(sys.stdin)
        response = facade.execute(request, grant, operator=args.operator)
    except Exception:
        identity = request if isinstance(request, dict) else {}
        response = {"protocol_version": 1, **{k: identity.get(k) for k in ("request_id", "task_id", "operation", "scope")},
            "ok": False, "status": "error", "source_versions": [], "error": {"code": "invalid_argument", "message": "Invalid projection configuration or request", "retryable": False},
            "receipt": {"execution": "not_started", "domain_write": "not_attempted"}}
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
