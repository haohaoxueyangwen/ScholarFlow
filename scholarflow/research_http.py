"""Loopback HTTP for the CLI research host. It does not serve the old Workbench."""
from __future__ import annotations

import json
import re
import secrets
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from .domain_queries import DomainQueryError
from .web_research import WebResearchService
from . import research_workspace as rw


def open_research_host(data_dir=None, home=None):
    """Use the same facts directory and workspace home as scripts/research.ps1."""
    return WebResearchService(data_dir or rw.DEFAULT_DATA, home=home or rw.HOME)


class ResearchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, service: WebResearchService, port: int = 0, *, allowed_origins=None, instance_id=""):
        self.service = service
        if not isinstance(instance_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{0,128}", instance_id):
            raise ValueError("Invalid research instance identity")
        self.instance_id = instance_id
        origins = allowed_origins if allowed_origins is not None else (
            "http://127.0.0.1:3000", "http://localhost:3000"
        )
        self.allowed_origins = frozenset(origins)
        for origin in self.allowed_origins:
            match = re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):(\d{1,5})", origin)
            if not match or not 1 <= int(match.group(1)) <= 65535:
                raise ValueError("Research origins must be explicit loopback HTTP origins")
        self.sessions = set()
        self.sessions_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), ResearchHandler)


class ResearchHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ResearchHTTPServer

    def log_message(self, format, *args):
        return

    def _send(self, status, payload, *, cookie=None):
        origin = self.headers.get("Origin") or ""
        if isinstance(payload, dict) and payload.get("__pdf__"):
            body = payload["body"]
            self.send_response(status)
            self._cors(origin)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            encoded_filename = quote(str(payload["filename"]), safe="")
            self.send_header("Content-Disposition", f"inline; filename=\"document.pdf\"; filename*=UTF-8''{encoded_filename}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors(origin)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _cors(self, origin):
        if origin in self.server.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Stage-Id, X-Pages, X-Filename, X-Provider, X-ScholarFlow-Intent")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self):
        if not self._host_allowed() or not self._origin_allowed(required=True):
            self._forbidden()
            return
        self.send_response(204)
        self._cors(self.headers.get("Origin") or "")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _host_allowed(self):
        host = self.headers.get("Host") or ""
        return host in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    def _origin_allowed(self, *, required=False):
        origin = self.headers.get("Origin") or ""
        return origin in self.server.allowed_origins if origin else not required

    def _session_id(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie") or "")
        except CookieError:
            return None
        morsel = cookie.get("scholarflow_research_session")
        return morsel.value if morsel else None

    def _has_session(self):
        with self.server.sessions_lock:
            return self._session_id() in self.server.sessions

    def _forbidden(self):
        # Do not parse a rejected request body as the next keep-alive request.
        self._discard_unread_body()
        self.close_connection = True
        self._send(403, {"error": {"code": "permission_denied", "message": "需要可信的本机网页会话"}})

    def _discard_unread_body(self):
        # Windows may reset a response if a rejected POST closes with unread
        # bytes. Bound both work and time; never parse an untrusted body.
        if self.command != "POST" or self.headers.get("Transfer-Encoding"):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return
        if length <= 0:
            return
        previous = self.connection.gettimeout()
        try:
            self.connection.settimeout(0.25)
            self.rfile.read(min(length, 65537))
        except OSError:
            pass
        finally:
            self.connection.settimeout(previous)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length < 0 or length > 1_000_000:
            raise DomainQueryError("invalid_argument", "请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DomainQueryError("invalid_argument", "请求不是 JSON") from error
        if not isinstance(value, dict):
            raise DomainQueryError("invalid_argument", "请求必须是对象")
        return value

    def _dispatch(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        service = self.server.service
        if self.command == "GET" and path == "/health":
            return 200, {"status": "ok", "service": "scholarflow-research", "instance_id": self.server.instance_id}
        if self.command == "GET" and path == "/projects":
            return 200, service.projects()
        if self.command == "POST" and path == "/projects/preview":
            return 200, service.preview_project(self._read_json())
        if self.command == "POST" and path == "/projects/commit":
            return 200, service.commit_project(self._read_json())
        if self.command == "POST" and path == "/literature/search":
            return 200, service.search_literature(self._read_json())
        if self.command == "POST" and path == "/literature/open":
            return 201, service.open_literature(self._read_json())
        if self.command == "GET" and path == "/workspaces":
            stage = (query.get("stage_id") or [None])[0]
            return 200, service.list(stage)
        if self.command == "POST" and path == "/workspaces":
            return 201, service.open_pdf(self._read_json())
        if self.command == "POST" and path == "/workspaces/project":
            return 201, service.create_project_workspace(self._read_json())
        if self.command == "POST" and path == "/workspaces/upload":
            stage = self.headers.get("X-Stage-Id") or ""
            pages = self.headers.get("X-Pages") or "1"
            filename = self.headers.get("X-Filename") or "upload.pdf"
            provider = self.headers.get("X-Provider")
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > 80_000_000:
                raise DomainQueryError("invalid_argument", "请上传不超过 80 MB 的 PDF")
            return 201, service.upload_pdf(stage, filename, self.rfile.read(length), pages=pages, provider=provider)
        if path.startswith("/workspaces/") and path.endswith("/marks"):
            workspace_id = path.removeprefix("/workspaces/").removesuffix("/marks").strip("/")
            if self.command == "GET":
                return 200, service.marks(workspace_id, (query.get("stage_id") or [None])[0])
            if self.command == "POST":
                return 201, service.save_mark(workspace_id, self._read_json())
        if self.command == "POST" and path.startswith("/workspaces/") and path.endswith("/translate"):
            workspace_id = path.removeprefix("/workspaces/").removesuffix("/translate").strip("/")
            return 200, service.translate(workspace_id, self._read_json())
        if self.command == "POST" and path.startswith("/workspaces/") and path.endswith("/turns"):
            workspace_id = path.removeprefix("/workspaces/").removesuffix("/turns").strip("/")
            return 202, service.prompt(workspace_id, self._read_json())
        if self.command == "POST" and path.startswith("/workspaces/") and path.endswith("/approve"):
            workspace_id = path.removeprefix("/workspaces/").removesuffix("/approve").strip("/")
            return 200, service.decide(workspace_id, self._read_json())
        if self.command == "POST" and path.startswith("/workspaces/") and path.endswith("/revise"):
            workspace_id = path.removeprefix("/workspaces/").removesuffix("/revise").strip("/")
            return 200, service.revise(workspace_id, self._read_json())
        if self.command == "POST" and path.startswith("/workspaces/") and path.endswith("/index"):
            workspace_id = path.removeprefix("/workspaces/").removesuffix("/index").strip("/")
            return 202, service.index(workspace_id, self._read_json())
        if self.command == "GET" and path.startswith("/workspaces/"):
            stage = (query.get("stage_id") or [None])[0]
            return 200, service.get(path.removeprefix("/workspaces/"), stage)
        if self.command == "GET" and path.startswith("/imports/") and path.endswith("/pdf"):
            import_id = path.removeprefix("/imports/").removesuffix("/pdf").strip("/")
            filename, payload = service.read_pdf(import_id)
            return 200, {"__pdf__": True, "filename": filename, "body": payload}
        raise DomainQueryError("not_found", "未知的研究接口")

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if self.client_address[0] != "127.0.0.1" or not self._host_allowed() or not self._origin_allowed():
            self._forbidden()
            return
        if self.command == "GET" and path == "/session":
            if not self._origin_allowed(required=True):
                self._forbidden()
                return
            if self._has_session():
                self._send(200, {"status": "ready", "reused": True})
                return
            with self.server.sessions_lock:
                if len(self.server.sessions) >= 1024:
                    self._send(503, {"error": {"code": "session_limit", "message": "本机会话数量已达上限"}})
                    return
                token = secrets.token_urlsafe(32)
                self.server.sessions.add(token)
            self._send(200, {"status": "ready"}, cookie=f"scholarflow_research_session={token}; Path=/; HttpOnly; SameSite=Strict")
            return
        if path != "/health" or self.command != "GET":
            if not self._has_session() or (self.command == "POST" and (
                not self._origin_allowed(required=True) or self.headers.get("X-ScholarFlow-Intent") != "confirm"
            )):
                self._forbidden()
                return
        if self.command == "POST":
            content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            expected = "application/pdf" if path == "/workspaces/upload" else "application/json"
            if content_type != expected:
                self._discard_unread_body()
                self.close_connection = True
                self._send(400, {"error": {"code": "invalid_argument", "message": "请求内容类型不正确"}})
                return
        try:
            status, payload = self._dispatch()
        except DomainQueryError as error:
            status = 404 if error.code == "not_found" else 400
            payload = {"error": {"code": error.code, "message": str(error)}}
        except Exception:
            status, payload = 500, {"error": {"code": "internal", "message": "研究服务未能完成这次操作"}}
        self._send(status, payload)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--home", default=None)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--web-origin", action="append", dest="web_origins", default=None,
                        help="Trusted loopback web origin; may be specified more than once")
    parser.add_argument("--instance-id", default="")
    args = parser.parse_args()
    service = open_research_host(args.data_dir, args.home)
    server = ResearchHTTPServer(service, port=args.port, allowed_origins=args.web_origins, instance_id=args.instance_id)
    print(json.dumps({"status": "ready", "url": f"http://127.0.0.1:{server.server_port}", "data_dir": str(service.data)}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
