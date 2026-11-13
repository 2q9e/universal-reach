from __future__ import annotations

import gzip
import io
import json
import zlib
from email.message import Message
from urllib.error import HTTPError
from urllib.request import ProxyHandler

import pytest

from universal_reach import http
from universal_reach.errors import ReachError


@pytest.mark.parametrize(
    ("body", "encoding"),
    [
        (gzip.compress(b'{"items":[]}'), "gzip"),
        (zlib.compress(b'{"items":[]}'), "deflate"),
        (gzip.compress(b'{"items":[]}'), None),
        (b'{"items":[]}', None),
    ],
)
def test_json_body_decodes_supported_stackexchange_encodings(body: bytes, encoding: str | None) -> None:
    assert http._decode_json_body(body, encoding, assume_gzip=True) == {"items": []}


def test_bad_compressed_body_returns_parser_error() -> None:
    with pytest.raises(ReachError) as error:
        http._decode_json_body(b"not-gzip", "gzip", assume_gzip=True)
    assert error.value.code == "parser_error"


def test_configured_json_http_error_status_returns_decoded_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = Message()
    payload = {"error_id": 502, "error_name": "throttle_violation", "error_message": "Wait."}
    compressed = gzip.compress(json.dumps(payload).encode())
    error = HTTPError(
        "https://api.stackexchange.com/2.3/search/advanced",
        400,
        "Bad Request",
        headers,
        io.BytesIO(compressed),
    )

    class FakeOpener:
        def open(self, request, timeout):
            assert request.get_header("Accept-encoding") == "gzip"
            raise error

    monkeypatch.setattr(http, "build_opener", lambda *_: FakeOpener())
    response = http._fetch_json_sync(
        "https://api.stackexchange.com/2.3/search/advanced",
        frozenset({"api.stackexchange.com"}),
        None,
        {"Accept-Encoding": "gzip"},
        1,
        0,
        True,
        frozenset({400}),
    )
    assert response == payload


def test_other_http_error_status_stays_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    error = HTTPError("https://api.example.test/items", 400, "Bad Request", Message(), io.BytesIO(b"{}"))

    class FakeOpener:
        def open(self, request, timeout):
            raise error

    monkeypatch.setattr(http, "build_opener", lambda *_: FakeOpener())
    with pytest.raises(ReachError) as caught:
        http._fetch_json_sync(
            "https://api.example.test/items",
            frozenset({"api.example.test"}),
            None,
            None,
            1,
            0,
            False,
            frozenset(),
        )
    assert caught.value.code == "upstream_error"
    assert caught.value.details["http_status"] == 400


def test_public_instance_uses_checked_pinned_address_without_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    address = (2, 1, 6, "", ("93.184.216.34", 443))
    resolved: list[tuple[str, int]] = []
    handlers = []

    class FakeResponse:
        headers = Message()

        def geturl(self) -> str:
            return "https://instance.example/api/v3/site"

        def read(self) -> bytes:
            return b'{"ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeOpener:
        def open(self, request, timeout):
            return FakeResponse()

    def resolve(host: str, port: int):
        resolved.append((host, port))
        return [address]

    monkeypatch.setattr(http, "resolve_public_addresses", resolve)
    monkeypatch.setattr(http, "build_opener", lambda *values: (handlers.extend(values), FakeOpener())[1])
    result = http._fetch_json_sync(
        "https://instance.example/api/v3/site",
        frozenset({"instance.example"}),
        None,
        None,
        1,
        0,
        False,
        frozenset(),
        True,
    )

    assert result == {"ok": True}
    assert resolved == [("instance.example", 443)]
    assert any(isinstance(handler, ProxyHandler) and handler.proxies == {} for handler in handlers)
    pinned = next(handler for handler in handlers if isinstance(handler, http._PinnedHTTPSHandler))
    assert pinned.pinned_addresses == [address]


def test_pinned_https_connection_uses_validated_ip_and_original_tls_host(monkeypatch: pytest.MonkeyPatch) -> None:
    address = (2, 1, 6, "", ("93.184.216.34", 443))

    class FakeSocket:
        destination = None
        timeout = None

        def settimeout(self, value):
            self.timeout = value

        def connect(self, destination):
            self.destination = destination

        def close(self):
            pass

    class FakeContext:
        server_hostname = None

        def wrap_socket(self, connection, *, server_hostname):
            self.server_hostname = server_hostname
            return connection

    fake_socket = FakeSocket()
    fake_context = FakeContext()
    monkeypatch.setattr(http.socket, "socket", lambda *_args: fake_socket)
    handler = http._PinnedHTTPSHandler([address])
    handler._context = fake_context

    def invoke_connection(factory, request, *, context):
        connection = factory(request.host, timeout=2.0, context=context)
        connection.connect()
        return connection

    monkeypatch.setattr(handler, "do_open", invoke_connection)
    handler.https_open(http.Request("https://instance.example/api/v3/site"))

    assert fake_socket.destination == ("93.184.216.34", 443)
    assert fake_socket.timeout == 2.0
    assert fake_context.server_hostname == "instance.example"


@pytest.mark.parametrize(
    ("body", "encoding", "charset", "expected"),
    [
        (gzip.compress("hello 🧭".encode()), "gzip", "utf-8", "hello 🧭"),
        (zlib.compress(b"<rss/>"), "deflate", "utf-8", "<rss/>"),
        (b"caf\xe9", None, "iso-8859-1", "café"),
        (b"\xef\xbb\xbfhello", None, "utf-8", "hello"),
    ],
)
def test_text_body_decodes_content_encoding_and_charset(
    body: bytes, encoding: str | None, charset: str, expected: str,
) -> None:
    assert http._decode_text_body(body, encoding, charset, 100) == expected


def test_text_body_limits_decompressed_responses() -> None:
    compressed = gzip.compress(b"x" * 100_000)
    with pytest.raises(ReachError) as caught:
        http._decode_text_body(compressed, "gzip", "utf-8", 1024)
    assert caught.value.code == "response_too_large"


def test_fetch_text_uses_allowed_https_host_and_enforces_response_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = Message()
    headers["Content-Type"] = "text/html; charset=utf-8"

    class FakeResponse:
        def __init__(self, body: bytes):
            self.headers = headers
            self.body = body

        def geturl(self) -> str:
            return "https://public.example/page"

        def read(self, limit: int) -> bytes:
            return self.body[:limit]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeOpener:
        def __init__(self, body: bytes):
            self.response = FakeResponse(body)

        def open(self, request, timeout):
            assert request.get_header("Accept-encoding") == "identity"
            assert timeout == 2
            return self.response

    opener = FakeOpener("<html>🧭</html>".encode())
    monkeypatch.setattr(http, "build_opener", lambda *_: opener)
    result = http._fetch_text_sync(
        "https://public.example/page",
        frozenset({"public.example"}),
        None,
        None,
        2,
        0,
        100,
    )
    assert result == "<html>🧭</html>"

    opener.response.body = b"too big"
    with pytest.raises(ReachError) as caught:
        http._fetch_text_sync(
            "https://public.example/page",
            frozenset({"public.example"}),
            None,
            None,
            2,
            0,
            4,
        )
    assert caught.value.code == "response_too_large"


def test_fetch_text_rejects_redirect_outside_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        headers = Message()

        def geturl(self) -> str:
            return "https://evil.example/page"

        def read(self, _limit: int) -> bytes:
            return b"<html/>"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeOpener:
        def open(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(http, "build_opener", lambda *_: FakeOpener())
    with pytest.raises(ReachError) as caught:
        http._fetch_text_sync(
            "https://public.example/page",
            frozenset({"public.example"}),
            None,
            None,
            2,
            0,
            100,
        )
    assert caught.value.code == "invalid_url"
