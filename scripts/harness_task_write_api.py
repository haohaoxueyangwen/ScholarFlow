"""Optional authenticated loopback task channel, no operator approval route."""
import argparse
import json
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/scholarflow-pi/capabilities/task-write"))
from operations import TaskWriteFacade, TaskWriteGrant
from scholarflow.read_http import create_operation_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--grant-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    def current_grant():
        return TaskWriteGrant.from_dict(json.loads(args.grant_file.read_text(encoding="utf-8-sig")))
    current_grant()  # Fail closed at startup; re-read on every request.
    server = create_operation_server(TaskWriteFacade(args.data_dir), current_grant, os.environ.get("SCHOLARFLOW_TASK_API_TOKEN", ""), args.port,
                                     operation_path="/v1/task-write")
    print(json.dumps({"host": "127.0.0.1", "port": server.server_port, "capability": "task-write", "channel": "agent", "approval_route": False}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
