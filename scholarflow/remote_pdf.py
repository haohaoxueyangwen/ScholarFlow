"""Bounded HTTPS-only PDF fetching with DNS pinning and manual redirects."""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlunsplit


MAX_REMOTE_PDF_BYTES = 80_000_000
MAX_REDIRECTS = 5
TOTAL_TIMEOUT_SECONDS = 60.0
SOCKET_TIMEOUT_SECONDS = 10.0
CHUNK_BYTES = 128 * 1024


class RemotePdfError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _validated_target(url: str) -> tuple[str, int, str]:
    try:
        parsed = urlsplit(str(url or "").strip())
        port = parsed.port or 443
    except ValueError as exc:
        raise RemotePdfError("invalid_argument", "open PDF URL is malformed") from exc
    if parsed.scheme.casefold() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RemotePdfError("invalid_argument", "open PDF import requires HTTPS without embedded credentials")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise RemotePdfError("invalid_argument", "open PDF host name is invalid") from exc
    if port != 443:
        raise RemotePdfError("invalid_argument", "open PDF import permits the standard HTTPS port only")
    path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return hostname, port, path


def _public_addresses(hostname: str, port: int) -> list[str]:
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = [str(literal)]
    except ValueError:
        try:
            records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            addresses = sorted({str(item[4][0]).split("%", 1)[0] for item in records})
        except OSError as exc:
            raise RemotePdfError("remote_pdf_unavailable", "open PDF host could not be resolved") from exc
    if not addresses:
        raise RemotePdfError("remote_pdf_unavailable", "open PDF host has no network addresses")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise RemotePdfError("remote_pdf_unavailable", "open PDF host returned an invalid address") from exc
        if not address.is_global:
            raise RemotePdfError("remote_pdf_forbidden", "open PDF host resolved to a non-public network")
    return addresses


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, port: int, timeout: float):
        super().__init__(address, port=port, timeout=timeout, context=ssl.create_default_context())
        self._certificate_hostname = hostname

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(sock, server_hostname=self._certificate_hostname)


def fetch_remote_pdf(url: str, *, max_bytes: int = MAX_REMOTE_PDF_BYTES,
                     total_timeout_seconds: float = TOTAL_TIMEOUT_SECONDS,
                     connection_factory=None) -> tuple[bytes, str, str]:
    """Return bounded PDF bytes, content type and final credential-free URL."""
    deadline = time.monotonic() + max(1.0, float(total_timeout_seconds))
    current = str(url or "").strip()
    factory = connection_factory or _PinnedHttpsConnection
    for redirect_count in range(MAX_REDIRECTS + 1):
        hostname, port, target = _validated_target(current)
        addresses = _public_addresses(hostname, port)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RemotePdfError("remote_pdf_unavailable", "open PDF download exceeded its time limit")
        connection = factory(hostname, addresses[0], port, min(SOCKET_TIMEOUT_SECONDS, remaining))
        response = None
        try:
            connection.putrequest("GET", target, skip_accept_encoding=True)
            host_header = f"[{hostname}]" if ":" in hostname else hostname
            connection.putheader("Host", host_header)
            connection.putheader("User-Agent", "ScholarFlow Workbench/1.0")
            connection.putheader("Accept", "application/pdf,application/octet-stream;q=0.9")
            connection.putheader("Accept-Encoding", "identity")
            connection.endheaders()
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location or redirect_count >= MAX_REDIRECTS:
                    raise RemotePdfError("remote_pdf_redirect_invalid", "open PDF has a missing or excessive redirect")
                current = urljoin(current, location)
                continue
            if response.status != 200:
                raise RemotePdfError("remote_pdf_unavailable", f"open PDF provider returned HTTP {response.status}")
            if (response.getheader("Content-Encoding") or "identity").casefold() not in {"", "identity"}:
                raise RemotePdfError("reader_pdf_invalid", "compressed remote PDF responses are unsupported")
            content_length = response.getheader("Content-Length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise RemotePdfError("reader_pdf_invalid", "open PDF has an invalid Content-Length") from exc
                if declared_size < 0 or declared_size > max_bytes:
                    raise RemotePdfError("reader_pdf_size_invalid", "open PDF exceeds the 80 MB import limit")
            chunks: list[bytes] = []
            received = 0
            while received <= max_bytes:
                if time.monotonic() >= deadline:
                    raise RemotePdfError("remote_pdf_unavailable", "open PDF download exceeded its time limit")
                chunk = response.read(min(CHUNK_BYTES, max_bytes + 1 - received))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
            if received > max_bytes:
                raise RemotePdfError("reader_pdf_size_invalid", "open PDF exceeds the 80 MB import limit")
            body = b"".join(chunks)
            if not body or b"%PDF-" not in body[:1024]:
                raise RemotePdfError("reader_pdf_invalid", "the open PDF link did not return a valid PDF")
            return body, str(response.getheader("Content-Type") or "").casefold(), current
        except RemotePdfError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as exc:
            raise RemotePdfError("remote_pdf_unavailable", "open PDF could not be safely downloaded") from exc
        finally:
            if response is not None:
                response.close()
            connection.close()
    raise RemotePdfError("remote_pdf_redirect_invalid", "open PDF redirect limit exceeded")
