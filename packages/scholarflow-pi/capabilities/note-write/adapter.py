"""Local note CLI. Operator approval is not a model tool argument."""
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--grant-file", type=Path, required=True)
    parser.add_argument("--request-json")
    parser.add_argument("--operator", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    request = None
    try:
        from operations import NoteWriteFacade, NoteWriteGrant
        request = json.loads(args.request_json) if args.request_json else None
        grant = NoteWriteGrant.from_dict(json.loads(args.grant_file.read_text(encoding="utf-8-sig")))
        facade = NoteWriteFacade(args.data_dir)
        if args.status:
            print(json.dumps(facade.describe(grant), ensure_ascii=False))
            return 0
        request = request if args.request_json else json.load(sys.stdin)
        response = facade.execute(request, grant, operator=args.operator)
    except (ValueError, OSError, ImportError):
        identity = request if isinstance(request, dict) else {}
        response = {"protocol_version": 1, **{key: identity.get(key) for key in ("request_id", "task_id", "operation", "scope")},
            "ok": False, "status": "error", "source_versions": [],
            "error": {"code": "invalid_argument", "message": "Invalid note-write configuration or JSON", "retryable": False},
            "receipt": {"execution": "not_started", "domain_write": "not_attempted"}}
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
