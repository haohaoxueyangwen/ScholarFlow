"""Explicit loopback-only, authenticated transport for the same read facade."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from .read_operations import ReadControl

MAX_BODY = 65536


def create_operation_server(facade, grant, token: str, port: int = 0, *, operation_path="/v1/read"):
    if not isinstance(token, str) or not 32 <= len(token) <= 256 or not token.isascii():
        raise ValueError("Configure a 32..256 character ASCII local API token")
    jobs, lock = {}, threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format, *args):
            pass  # Never log Authorization headers, URLs or source content.

        def reply(self, code, body):
            # Rejections occur before body parsing. Unread POST bytes at close
            # can reset an otherwise valid error response, notably on Windows.
            self.drain_unread_body()
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # A response loss does not undo an executed read/audit.
            self.close_connection = True

        def drain_unread_body(self):
            if getattr(self, "_body_consumed", False):
                return
            self._body_consumed = True
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (ValueError, TypeError):
                return
            if length <= 0 or self.headers.get("Transfer-Encoding"):
                return
            # Never parse/execute an unauthenticated body or wait indefinitely.
            # Very large/malformed streams may still lose their response.
            previous_timeout = self.connection.gettimeout()
            try:
                self.connection.settimeout(0.25)
                self.rfile.read(min(length, MAX_BODY + 1))
            except OSError:
                pass
            finally:
                self.connection.settimeout(previous_timeout)

        def failure(self, code, message, http=400):
            self.reply(http, {"protocol_version": 1, "ok": False, "status": "error", "error": {"code": code, "message": message, "retryable": False},
                              "receipt": {"execution": "not_started", "audit": {"persisted": False}}, "source_versions": []})

        def authorized(self):
            # No browser integration in M2. A browser origin is rejected even
            # with credentials; clients must explicitly use the local API.
            if self.headers.get("Origin") or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                self.failure("permission_denied", "Origin or Host is not permitted", 403)
                return False
            actual = self.headers.get("Authorization", "")
            if not hmac.compare_digest(actual.encode("utf-8"), ("Bearer " + token).encode("ascii")):
                self.failure("unauthenticated", "Local API authentication required", 401)
                return False
            return True

        def current_grant(self):
            # File-backed write authority may be revoked while this server runs.
            # Existing read callers continue to supply an immutable grant.
            return grant() if callable(grant) else grant

        def execute(self, request, control=None):
            try:
                current = self.current_grant()
            except (ValueError, OSError):
                identity = request if isinstance(request, dict) else {}
                return {"protocol_version": 1, **{key: identity.get(key) for key in ("request_id", "task_id", "operation", "scope")},
                        "ok": False, "status": "error", "source_versions": [],
                        "error": {"code": "invalid_argument", "message": "Invalid current host grant", "retryable": False},
                        "receipt": {"execution": "not_started", "domain_write": "not_attempted"}}
            return facade.execute(request, current, control)

        def body(self):
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Chunked requests are not supported")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                raise ValueError("application/json required")
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= MAX_BODY:
                raise ValueError("Request body size is out of range")
            self.connection.settimeout(5)
            raw = self.rfile.read(length)
            self._body_consumed = True
            if len(raw) != length:
                raise ValueError("Incomplete request body")
            return json.loads(raw.decode("utf-8"))

        def do_GET(self):
            if not self.authorized():
                return
            if self.path != "/v1/status":
                self.failure("not_found", "Unknown endpoint", 404)
                return
            try:
                self.reply(200, facade.describe(self.current_grant()))
            except (ValueError, OSError):
                self.failure("invalid_argument", "Invalid current host grant")

        def do_POST(self):
            if not self.authorized():
                return
            try:
                request = self.body()
            except (ValueError, UnicodeError, OSError):
                self.failure("invalid_argument", "Invalid JSON request body")
                return
            if self.path == "/v1/cancel":
                if not isinstance(request, dict) or set(request) != {"request_id"} or not isinstance(request["request_id"], str):
                    self.failure("invalid_argument", "Cancel requires only request_id")
                    return
                with lock:
                    control = jobs.get(request["request_id"])
                    if control:
                        control.cancelled.set()
                self.reply(200, {"request_id": request["request_id"], "cancel_requested": control is not None,
                                 "terminal": False, "note": "Wait for the read result/receipt; cancellation is cooperative"})
                return
            if self.path != operation_path:
                self.failure("not_found", "Unknown endpoint", 404)
                return
            identity = request.get("request_id") if isinstance(request, dict) else None
            if not isinstance(identity, str) or not identity or len(identity) > 200:
                self.reply(200, self.execute(request))
                return
            timeout = request.get("timeout_ms", 15000)
            control = ReadControl(timeout if type(timeout) is int and 1 <= timeout <= 30000 else 15000)
            with lock:
                duplicate = identity in jobs
                if not duplicate:
                    jobs[identity] = control
            if duplicate:
                self.failure("request_conflict", "Request ID is already in flight", 409)
                return
            try:
                self.reply(200, self.execute(request, control))
            finally:
                with lock:
                    jobs.pop(identity, None)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def create_read_server(facade, grant, token: str, port: int = 0):
    """Compatibility entry; read endpoint and authorization semantics unchanged."""
    return create_operation_server(facade, grant, token, port)
