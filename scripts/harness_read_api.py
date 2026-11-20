"""Opt-in local read API, not the production Workbench service."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scholarflow.read_operation_registry import build_read_facade
from scholarflow.read_operations import ReadGrant
from scholarflow.read_http import create_read_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--grant-file", type=Path, required=True)
    parser.add_argument("--audit-path")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("Invalid local port")
    grant = ReadGrant.from_dict(json.loads(args.grant_file.read_text(encoding="utf-8")))
    facade = build_read_facade(args.data_dir, args.audit_path, grant.capabilities)
    server = create_read_server(facade, grant, os.environ.get("SCHOLARFLOW_READ_API_TOKEN", ""), args.port)
    print(json.dumps({"host": "127.0.0.1", "port": server.server_port, "read_only": True, "capabilities": sorted(grant.capabilities)}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
