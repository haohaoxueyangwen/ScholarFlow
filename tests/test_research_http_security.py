"""HTTP boundary tests using synthetic data and an ephemeral loopback server."""
import http.client
import json
import socket
import threading
import unittest
from urllib.parse import quote

from scholarflow.research_http import ResearchHTTPServer


class SyntheticService:
    data = "synthetic"

    def __init__(self):
        self.previews = 0
        self.revisions = 0

    def projects(self):
        return {"items": [{"stage_id": "synthetic-project"}]}

    def create_project_workspace(self, payload):
        return {"received": payload, "pdf": None}

    def preview_project(self, value):
        self.previews += 1
        return {"received": value}

    def revise(self, workspace, value):
        self.revisions += 1
        return {"workspace": workspace, "received": value}

    def read_pdf(self, import_id):
        if import_id != "synthetic-pdf":
            raise ValueError("unknown synthetic fixture")
        return "中文阅读.pdf", b"%PDF-1.4\nsynthetic\n%%EOF"


class ResearchHTTPSecurityTests(unittest.TestCase):
    origin = "http://127.0.0.1:3000"

    def setUp(self):
        self.service = SyntheticService()
        self.server = ResearchHTTPServer(self.service, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, *, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, headers=headers or {}, body=body)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def session(self):
        status, headers, _ = self.request("GET", "/session", headers={"Origin": self.origin})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_health_is_public_but_project_data_requires_session(self):
        status, _, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertNotIn("data_dir", json.loads(body))
        status, _, _ = self.request("GET", "/projects", headers={"Origin": self.origin})
        self.assertEqual(status, 403)

    def test_health_reports_launcher_instance_identity_without_paths(self):
        server = ResearchHTTPServer(self.service, port=0, instance_id="instance-123")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("GET", "/health")
                response = connection.getresponse()
                self.assertEqual(json.loads(response.read()), {"status": "ok", "service": "scholarflow-research", "instance_id": "instance-123"})
            finally:
                connection.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_trusted_session_can_read_and_pdf_style_get_without_origin(self):
        cookie = self.session()
        status, headers, body = self.request("GET", "/projects", headers={"Origin": self.origin, "Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), self.origin)
        self.assertEqual(headers.get("Access-Control-Allow-Credentials"), "true")
        self.assertEqual(json.loads(body)["items"][0]["stage_id"], "synthetic-project")
        status, _, _ = self.request("GET", "/projects", headers={"Cookie": cookie})
        self.assertEqual(status, 200)

    def test_origin_prefix_spoof_cannot_get_session_or_cors(self):
        for origin in ("http://localhost.attacker.example", "http://127.0.0.1.attacker.example", "null", "http://localhost:3001"):
            with self.subTest(origin=origin):
                status, headers, _ = self.request("GET", "/session", headers={"Origin": origin})
                self.assertEqual(status, 403)
                self.assertNotIn("Access-Control-Allow-Origin", headers)
                status, headers, _ = self.request("OPTIONS", "/projects", headers={"Origin": origin})
                self.assertEqual(status, 403)
                self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_missing_origin_cannot_establish_session(self):
        status, _, _ = self.request("GET", "/session")
        self.assertEqual(status, 403)

    def test_untrusted_origin_cannot_use_existing_session(self):
        cookie = self.session()
        status, headers, _ = self.request("GET", "/projects", headers={"Origin": "http://localhost.attacker.example", "Cookie": cookie})
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_host_rebinding_is_rejected(self):
        status, _, _ = self.request("GET", "/health", headers={"Host": f"attacker.example:{self.server.server_port}"})
        self.assertEqual(status, 403)

    def test_mutations_require_origin_session_intent_and_json(self):
        cookie = self.session()
        base = {"Origin": self.origin, "Cookie": cookie, "Content-Type": "application/json", "X-ScholarFlow-Intent": "confirm"}
        body = b'{"project_name":"synthetic"}'
        for missing in ("Origin", "Cookie", "X-ScholarFlow-Intent"):
            headers = dict(base)
            headers.pop(missing)
            status, _, _ = self.request("POST", "/projects/preview", headers=headers, body=body)
            self.assertEqual(status, 403, missing)
        wrong_type = dict(base, **{"Content-Type": "text/plain"})
        status, _, _ = self.request("POST", "/projects/preview", headers=wrong_type, body=body)
        self.assertEqual(status, 400)
        self.assertEqual(self.service.previews, 0)
        status, _, _ = self.request("POST", "/projects/preview", headers=base, body=body)
        self.assertEqual(status, 200)
        self.assertEqual(self.service.previews, 1)

    def test_preflight_only_accepts_trusted_origin(self):
        status, headers, _ = self.request("OPTIONS", "/projects", headers={"Origin": self.origin})
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), self.origin)
        self.assertIn("X-ScholarFlow-Intent", headers.get("Access-Control-Allow-Headers", ""))

    def test_revision_requires_the_same_trusted_session_and_intent(self):
        cookie = self.session()
        base = {"Origin": self.origin, "Cookie": cookie, "Content-Type": "application/json", "X-ScholarFlow-Intent": "confirm"}
        for missing in ("Origin", "Cookie", "X-ScholarFlow-Intent"):
            headers = dict(base); headers.pop(missing)
            status, _, _ = self.request("POST", "/workspaces/example/revise", headers=headers, body=b'{}')
            self.assertEqual(status, 403)
        self.assertEqual(self.service.revisions, 0)
        status, _, body = self.request("POST", "/workspaces/example/revise", headers=base, body=b'{}')
        self.assertEqual(status, 200); self.assertEqual(json.loads(body)["workspace"], "example")
        self.assertEqual(self.service.revisions, 1)

    def test_project_chat_start_requires_operator_intent(self):
        path='/workspaces/project'
        headers={'Cookie':self.session(),'Origin':self.origin,'Content-Type':'application/json'}
        body=b'{"stage_id":"synthetic-project","provider":"scholarflow-local"}'
        status,_,_=self.request('POST',path,headers=headers,body=body)
        self.assertEqual(status,403)
        status,_,payload=self.request('POST',path,headers={**headers,'X-ScholarFlow-Intent':'confirm'},body=body)
        self.assertEqual(status,201)
        self.assertIsNone(json.loads(payload)['pdf'])

    def test_repeated_rejected_posts_return_403_without_dispatch(self):
        for _ in range(30):
            status, _, body = self.request("POST", "/workspaces/example/revise", headers={"Content-Type":"application/json"}, body=b'{"synthetic":true}')
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(body)["error"]["code"], "permission_denied")
        self.assertEqual(self.service.revisions, 0)

    def test_rejected_incomplete_body_does_not_block_indefinitely(self):
        with socket.create_connection(("127.0.0.1",self.server.server_port),timeout=2) as client:
            client.sendall((f"POST /workspaces/example/revise HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\nContent-Length: 100\r\nContent-Type: application/json\r\n\r\n".encode()+b"{"))
            self.assertIn(b"403",client.recv(1024))
        self.assertEqual(self.service.revisions,0)

    def test_chinese_pdf_filename_and_bytes_survive_http_response(self):
        cookie = self.session()
        try:
            status, headers, body = self.request("GET", "/imports/synthetic-pdf/pdf", headers={"Cookie": cookie})
        except http.client.RemoteDisconnected:
            status, headers, body = None, {}, b""
        self.assertEqual(status, 200)
        self.assertEqual(body, b"%PDF-1.4\nsynthetic\n%%EOF")
        self.assertIn("filename*=UTF-8''" + quote("中文阅读.pdf"), headers["Content-Disposition"])


if __name__ == "__main__":
    unittest.main()
