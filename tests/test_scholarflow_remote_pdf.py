import http.client
import io
import socket
import unittest
from unittest.mock import Mock, patch

from scholarflow.remote_pdf import (
    RemotePdfError,
    _NoRedirect,
    _PinnedHttpsConnection,
    _resolve_synthetic_dns,
    fetch_remote_pdf,
)


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"%PDF-1.7\nfixture\n%%EOF"):
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.offset = 0
        self.closed = False

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, size=-1):
        result = self.body[self.offset:self.offset + size]
        self.offset += len(result)
        return result

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.target = ""
        self.headers = {}
        self.closed = False

    def putrequest(self, method, target, **kwargs):
        self.target = target

    def putheader(self, name, value):
        self.headers[name] = value

    def endheaders(self):
        pass

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class RemotePdfTest(unittest.TestCase):
    def test_public_resolver_sends_only_hostname_and_rejects_redirect_and_oversize(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'{"Status":0,"Answer":[{"type":1,"data":"93.184.216.34"}]}')
        with patch("scholarflow.remote_pdf.urllib.request.build_opener", return_value=opener) as build:
            self.assertEqual(_resolve_synthetic_dns("papers.example"), ["93.184.216.34"])
        self.assertIsInstance(build.call_args.args[0], _NoRedirect)
        request = opener.open.call_args.args[0]
        self.assertIn("name=papers.example", request.full_url)
        self.assertNotIn("paper.pdf", request.full_url)
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", None, "http://127.0.0.1/"))
        opener.open.return_value = io.BytesIO(b"x" * 65537)
        with patch("scholarflow.remote_pdf.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(RemotePdfError) as caught:
                _resolve_synthetic_dns("papers.example")
        self.assertEqual(caught.exception.code, "remote_pdf_unavailable")

    def test_fake_ip_dns_uses_public_resolver_and_still_pins_the_checked_address(self):
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.67", 443))]
        factory = Mock(return_value=FakeConnection(FakeResponse()))
        with patch("scholarflow.remote_pdf.socket.getaddrinfo", return_value=rows), patch(
            "scholarflow.remote_pdf._resolve_synthetic_dns", return_value=["93.184.216.34"], create=True
        ) as resolver:
            body, _, _ = fetch_remote_pdf("https://papers.example/paper.pdf", connection_factory=factory)
        self.assertTrue(body.startswith(b"%PDF-"))
        resolver.assert_called_once_with("papers.example")
        self.assertEqual(factory.call_args.args[:3], ("papers.example", "93.184.216.34", 443))

    def test_fake_ip_fallback_rejects_private_or_mixed_public_answers(self):
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.67", 443))]
        for addresses in (["127.0.0.2"], ["93.184.216.34", "127.0.0.1"]):
            factory = Mock()
            with self.subTest(addresses=addresses), patch("scholarflow.remote_pdf.socket.getaddrinfo", return_value=rows), patch(
                "scholarflow.remote_pdf._resolve_synthetic_dns", return_value=addresses, create=True
            ):
                with self.assertRaises(RemotePdfError) as caught:
                    fetch_remote_pdf("https://papers.example/paper.pdf", connection_factory=factory)
            self.assertEqual(caught.exception.code, "remote_pdf_forbidden")
            factory.assert_not_called()

    def test_fake_ip_fallback_does_not_disclose_local_names_or_allow_mixed_dns(self):
        fake = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.67", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", 443))
        for host, rows in (("printer.local", [fake]), ("printer.local.", [fake]), ("papers.example", [fake, private])):
            factory = Mock()
            with self.subTest(host=host), patch("scholarflow.remote_pdf.socket.getaddrinfo", return_value=rows), patch(
                "scholarflow.remote_pdf._resolve_synthetic_dns", create=True
            ) as resolver:
                with self.assertRaises(RemotePdfError) as caught:
                    fetch_remote_pdf(f"https://{host}/paper.pdf", connection_factory=factory)
            self.assertEqual(caught.exception.code, "remote_pdf_forbidden")
            resolver.assert_not_called()
            factory.assert_not_called()

    def test_rejects_loopback_literal_without_resolving_or_connecting(self):
        factory = Mock()
        with self.assertRaises(RemotePdfError) as caught:
            fetch_remote_pdf("https://127.0.0.1/private.pdf", connection_factory=factory)
        self.assertEqual(caught.exception.code, "remote_pdf_forbidden")
        factory.assert_not_called()

    def test_rejects_ipv6_loopback_and_mixed_link_local_dns_without_connecting(self):
        factory = Mock()
        with self.assertRaises(RemotePdfError) as literal_error:
            fetch_remote_pdf("https://[::1]/private.pdf", connection_factory=factory)
        self.assertEqual(literal_error.exception.code, "remote_pdf_forbidden")
        rows = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 443, 0, 0))
            for address in ("2606:4700:4700::1111", "fe80::1")
        ]
        with patch("scholarflow.remote_pdf.socket.getaddrinfo", return_value=rows):
            with self.assertRaises(RemotePdfError) as dns_error:
                fetch_remote_pdf("https://papers.example/paper.pdf", connection_factory=factory)
        self.assertEqual(dns_error.exception.code, "remote_pdf_forbidden")
        factory.assert_not_called()

    def test_rejects_mixed_public_and_private_dns_answers(self):
        factory = Mock()
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in ("93.184.216.34", "127.0.0.2")]
        with patch("scholarflow.remote_pdf.socket.getaddrinfo", return_value=rows):
            with self.assertRaises(RemotePdfError) as caught:
                fetch_remote_pdf("https://papers.example/paper.pdf", connection_factory=factory)
        self.assertEqual(caught.exception.code, "remote_pdf_forbidden")
        factory.assert_not_called()

    def test_revalidates_redirect_before_opening_its_target(self):
        first = FakeResponse(302, {"Location": "https://127.0.0.1/metadata"}, b"")
        factory = Mock(return_value=FakeConnection(first))
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("scholarflow.remote_pdf.socket.getaddrinfo", return_value=rows):
            with self.assertRaises(RemotePdfError) as caught:
                fetch_remote_pdf("https://papers.example/paper.pdf", connection_factory=factory)
        self.assertEqual(caught.exception.code, "remote_pdf_forbidden")
        factory.assert_called_once()
        self.assertTrue(first.closed)

    def test_rejects_https_downgrade_redirect(self):
        factory = Mock(return_value=FakeConnection(FakeResponse(302, {"Location": "http://papers.example/paper.pdf"}, b"")))
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("scholarflow.remote_pdf.socket.getaddrinfo", return_value=rows):
            with self.assertRaises(RemotePdfError) as caught:
                fetch_remote_pdf("https://papers.example/paper.pdf", connection_factory=factory)
        self.assertEqual(caught.exception.code, "invalid_argument")
        factory.assert_called_once()

    def test_caps_redirect_chains_even_when_the_provider_loops(self):
        factory = Mock(return_value=FakeConnection(FakeResponse(302, {"Location": "/same.pdf"}, b"")))
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("scholarflow.remote_pdf.socket.getaddrinfo", return_value=rows):
            with self.assertRaises(RemotePdfError) as caught:
                fetch_remote_pdf("https://papers.example/paper.pdf", connection_factory=factory)
        self.assertEqual(caught.exception.code, "remote_pdf_redirect_invalid")
        self.assertEqual(factory.call_count, 6)

    def test_bounds_stream_bytes_and_rejects_bad_content(self):
        response = FakeResponse(headers={"Content-Length": "11"}, body=b"%PDF-1.7\nX")
        factory = Mock(return_value=FakeConnection(response))
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("scholarflow.remote_pdf.socket.getaddrinfo", return_value=rows):
            with self.assertRaises(RemotePdfError) as caught:
                fetch_remote_pdf("https://papers.example/paper.pdf", max_bytes=10, connection_factory=factory)
        self.assertEqual(caught.exception.code, "reader_pdf_size_invalid")
        self.assertTrue(response.closed)

        non_pdf = FakeResponse(headers={"Content-Type": "application/pdf"}, body=b"not actually a PDF")
        with patch("scholarflow.remote_pdf.socket.getaddrinfo", return_value=rows), patch("scholarflow.remote_pdf._PinnedHttpsConnection", return_value=FakeConnection(non_pdf)):
            with self.assertRaises(RemotePdfError) as invalid:
                fetch_remote_pdf("https://papers.example/not-a-pdf.pdf")
        self.assertEqual(invalid.exception.code, "reader_pdf_invalid")

    def test_pinned_transport_connects_to_checked_ip_but_verifies_original_host(self):
        connection = _PinnedHttpsConnection("papers.example", "93.184.216.34", 443, 2)
        raw_socket = Mock()
        tls_socket = Mock()
        context = Mock()
        context.wrap_socket.return_value = tls_socket
        with patch("scholarflow.remote_pdf.socket.create_connection", return_value=raw_socket) as connect, patch("scholarflow.remote_pdf.ssl.create_default_context", return_value=context):
            connection = _PinnedHttpsConnection("papers.example", "93.184.216.34", 443, 2)
            connection.connect()
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))
        self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], "papers.example")
        connection.close()

    def test_enforces_total_deadline_after_a_slow_chunk_read(self):
        now = [0.0]

        class SlowResponse(FakeResponse):
            def read(self, size=-1):
                now[0] = 2.0
                return b"%PDF-1.7\n"

        factory = Mock(return_value=FakeConnection(SlowResponse()))
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("scholarflow.remote_pdf.time.monotonic", side_effect=lambda: now[0]), patch(
            "scholarflow.remote_pdf.socket.getaddrinfo", return_value=rows
        ):
            with self.assertRaises(RemotePdfError) as caught:
                fetch_remote_pdf(
                    "https://papers.example/paper.pdf",
                    total_timeout_seconds=1,
                    connection_factory=factory,
                )
        self.assertEqual(caught.exception.code, "remote_pdf_unavailable")

    def test_rejects_embedded_credentials_and_nonstandard_ports_before_connecting(self):
        factory = Mock()
        with patch("scholarflow.remote_pdf.socket.getaddrinfo") as resolver:
            for url in (
                "https://user:secret@localhost/paper.pdf",
                "https://papers.example:8443/paper.pdf",
            ):
                with self.subTest(url=url), self.assertRaises(RemotePdfError) as caught:
                    fetch_remote_pdf(url, connection_factory=factory)
                self.assertEqual(caught.exception.code, "invalid_argument")
        resolver.assert_not_called()
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
