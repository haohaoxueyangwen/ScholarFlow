"""Loopback-only, test-only cap around an isolated embedding process."""
import argparse
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class BudgetProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, upstream_port, *, max_requests, receipt_path=None):
        if address[0] != "127.0.0.1" or not 1 <= upstream_port <= 65535 or not 1 <= max_requests <= 16:
            raise ValueError("Only a bounded loopback test proxy is supported")
        self.upstream = f"http://127.0.0.1:{upstream_port}"
        self.limit = max_requests
        self.forwarded = 0
        self.denied = 0
        self.lock = threading.Lock()
        self.receipt_path = Path(receipt_path) if receipt_path else None
        if self.receipt_path and self.receipt_path.exists():
            raise ValueError("Use a new test budget receipt path")
        super().__init__(address, BudgetHandler)
        try:
            self.write_receipt()
        except Exception:
            self.server_close()
            raise

    def write_receipt(self):
        if not self.receipt_path:
            return
        payload = {"forwarded": self.forwarded, "denied": self.denied, "limit": self.limit}
        fd, name = tempfile.mkstemp(prefix=".budget-", suffix=".tmp", dir=self.receipt_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(payload, output)
            os.replace(name, self.receipt_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


class BudgetHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, value):
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/__budget":
            with self.server.lock:
                payload = {"forwarded": self.server.forwarded, "denied": self.server.denied, "limit": self.server.limit}
            self.send_json(200, payload)
            return
        if self.path == "/health":
            try:
                with urllib.request.urlopen(self.server.upstream + "/health", timeout=3) as response:
                    payload = json.load(response)
                self.send_json(200 if payload.get("status") == "ok" else 503, payload)
            except (OSError, ValueError):
                self.send_json(503, {"status": "unavailable"})
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/embeddings":
            self.send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if not 0 < length <= 1_000_000:
            self.send_json(400, {"error": "invalid test request size"})
            return
        raw = self.rfile.read(length)
        with self.server.lock:
            if self.server.forwarded >= self.server.limit:
                self.server.denied += 1
                self.server.write_receipt()
                self.send_json(429, {"error": "test embedding budget exhausted"})
                return
            self.server.forwarded += 1
            self.server.write_receipt()
        request = urllib.request.Request(self.server.upstream + "/v1/embeddings", data=raw, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                body = response.read()
                status = response.status
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (OSError, ValueError):
            self.send_json(502, {"error": "isolated embedding unavailable"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream-port", type=int, required=True)
    parser.add_argument("--max-requests", type=int, default=16)
    parser.add_argument("--receipt", default=None)
    args = parser.parse_args()
    server = BudgetProxy(("127.0.0.1", args.port), args.upstream_port, max_requests=args.max_requests, receipt_path=args.receipt)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
