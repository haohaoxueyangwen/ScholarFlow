import http.client
import json
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fixtures.embedding_budget_proxy import BudgetProxy


class UpstreamHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        self.server.requests += 1
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":[]}')


class EmbeddingProxyTests(unittest.TestCase):
    def test_bounded_proxy_forwards_no_more_than_the_fixed_budget(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        upstream.requests = 0
        proxy = BudgetProxy(("127.0.0.1", 0), upstream.server_port, max_requests=2)
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (upstream, proxy)]
        for thread in threads: thread.start()
        try:
            def request(method, path, body=None):
                c = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
                try:
                    c.request(method, path, body=body, headers={"Content-Type": "application/json"})
                    r = c.getresponse(); return r.status, json.loads(r.read())
                finally: c.close()
            self.assertEqual(request("GET", "/health")[0], 200)
            for expected in (200, 200, 429):
                self.assertEqual(request("POST", "/v1/embeddings", b'{"input":["synthetic"]}')[0], expected)
            self.assertEqual(upstream.requests, 2)
            self.assertEqual(request("GET", "/__budget")[1], {"forwarded": 2, "denied": 1, "limit": 2})
        finally:
            for s in (proxy, upstream): s.shutdown(); s.server_close()
            for thread in threads: thread.join(timeout=5)

    def test_budget_receipt_survives_process_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            from pathlib import Path
            receipt = Path(directory) / "budget.json"
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
            upstream.requests = 0
            proxy = BudgetProxy(("127.0.0.1", 0), upstream.server_port, max_requests=1, receipt_path=receipt)
            threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (upstream, proxy)]
            for thread in threads: thread.start()
            try:
                for _ in range(2):
                    connection = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
                    connection.request("POST", "/v1/embeddings", b'{"input":["synthetic"]}')
                    connection.getresponse().read(); connection.close()
            finally:
                for s in (proxy, upstream): s.shutdown(); s.server_close()
                for thread in threads: thread.join(timeout=5)
            self.assertEqual(json.loads(receipt.read_text(encoding="utf-8")), {"forwarded": 1, "denied": 1, "limit": 1})


if __name__ == "__main__":
    unittest.main()
