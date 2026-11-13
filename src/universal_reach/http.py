"""Small asynchronous JSON GET client with explicit host and redirect checks."""

from __future__ import annotations

import asyncio
import functools
import gzip
import json
import logging
import socket
import time
from typing import Any
from http.client import HTTPSConnection
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
import zlib

from universal_reach.errors import ReachError
from universal_reach.security import resolve_public_addresses, validate_https_url

logger = logging.getLogger("universal_reach.http")


class _SafeRedirects(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str] | frozenset[str]) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request | None:
        validate_https_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _PinnedHTTPSConnection(HTTPSConnection):
    """HTTPS connection that connects to prevalidated addresses for one host."""

    def __init__(self, host: str, *, pinned_addresses: list[tuple[int, int, int, str, tuple]], **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._pinned_addresses = pinned_addresses

    def connect(self) -> None:
        failures: list[OSError] = []
        for family, socktype, protocol, _canonname, sockaddr in self._pinned_addresses:
            connection = socket.socket(family, socktype, protocol)
            try:
                connection.settimeout(self.timeout)
                if self.source_address:
                    connection.bind(self.source_address)
                connection.connect(sockaddr)
                if self._tunnel_host:
                    self.sock = connection
                    self._tunnel()
                self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
                return
            except OSError as exc:
                failures.append(exc)
                connection.close()
        if failures:
            raise failures[-1]
        raise OSError("No validated addresses are available for the instance host.")


class _PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, pinned_addresses: list[tuple[int, int, int, str, tuple]]) -> None:
        super().__init__()
        self.pinned_addresses = pinned_addresses

    def https_open(self, request: Request) -> Any:
        connection_factory = functools.partial(
            _PinnedHTTPSConnection,
            pinned_addresses=self.pinned_addresses,
        )
        return self.do_open(
            connection_factory,
            request,
            context=self._context,
        )


def _with_query(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        query.append((key, str(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _decode_json_body(body: bytes, content_encoding: str | None, *, assume_gzip: bool = False) -> Any:
    """Decode a JSON body, optionally applying Stack Exchange's gzip fallback."""
    encoding = (content_encoding or "").strip().lower()
    try:
        if encoding == "gzip":
            body = gzip.decompress(body)
        elif encoding == "deflate":
            body = zlib.decompress(body)
        elif encoding in {"", "identity"}:
            # Stack Exchange documents gzip as its normal fallback when a
            # proxy strips Content-Encoding. Detect the gzip magic bytes so
            # early, uncompressed error responses remain readable.
            if assume_gzip and body.startswith(b"\x1f\x8b"):
                body = gzip.decompress(body)
        else:
            raise ReachError("parser_error", "The platform returned an unsupported content encoding.")
        return json.loads(body.decode("utf-8"))
    except ReachError:
        raise
    except (OSError, zlib.error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReachError("parser_error", "The platform returned malformed or undecodable JSON.") from exc


def _fetch_json_sync(
    url: str,
    allowed_hosts: set[str] | frozenset[str],
    params: dict[str, Any] | None,
    headers: dict[str, str] | None,
    timeout: float,
    retries: int,
    assume_gzip: bool,
    json_error_statuses: frozenset[int],
    public_instance: bool = False,
) -> Any:
    final_url = _with_query(url, params)
    validate_https_url(final_url, allowed_hosts)
    pinned_addresses = None
    if public_instance:
        host = (urlsplit(final_url).hostname or "").lower().rstrip(".")
        pinned_addresses = resolve_public_addresses(host, 443)
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "UniversalReach/0.1.0 (+https://github.com/2q9e/universal-reach)",
    }
    if headers:
        request_headers.update(headers)
    if pinned_addresses is None:
        opener = build_opener(_SafeRedirects(allowed_hosts))
    else:
        # A proxy would resolve the instance host independently, defeating the
        # checked-address pin. Static upstream APIs still use normal proxy rules.
        opener = build_opener(
            ProxyHandler({}),
            _SafeRedirects(allowed_hosts),
            _PinnedHTTPSHandler(pinned_addresses),
        )
    for attempt in range(retries + 1):
        started = time.monotonic()
        try:
            request = Request(final_url, headers=request_headers, method="GET")
            with opener.open(request, timeout=timeout) as response:
                response_url = response.geturl()
                validate_https_url(response_url, allowed_hosts)
                payload = _decode_json_body(
                    response.read(),
                    response.headers.get("Content-Encoding"),
                    assume_gzip=assume_gzip,
                )
                logger.debug("GET %s completed in %.3fs", urlsplit(final_url).hostname, time.monotonic() - started)
                return payload
        except HTTPError as exc:
            if exc.code in json_error_statuses:
                return _decode_json_body(
                    exc.read(),
                    exc.headers.get("Content-Encoding") if exc.headers else None,
                    assume_gzip=assume_gzip,
                )
            if exc.code == 404:
                raise ReachError("not_found", "The requested public content was not found.", details={"http_status": 404}) from exc
            if exc.code in (401, 403):
                raise ReachError("private_or_inaccessible", "The platform did not make this content publicly accessible.", details={"http_status": exc.code}) from exc
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                if attempt < retries:
                    try:
                        delay = min(max(int(retry_after), 1), 8)
                    except ValueError:
                        delay = 2 ** attempt
                    time.sleep(delay)
                    continue
                raise ReachError("rate_limited", "The platform rate-limited this request.", retryable=True, details={"http_status": 429}) from exc
            if 500 <= exc.code < 600:
                if attempt < retries:
                    time.sleep(0.25 * (2 ** attempt))
                    continue
                raise ReachError("temporarily_unavailable", "The upstream platform is temporarily unavailable.", retryable=True, details={"http_status": exc.code}) from exc
            raise ReachError("upstream_error", "The upstream platform rejected the request.", details={"http_status": exc.code}) from exc
        except ReachError:
            raise
        except (TimeoutError, URLError) as exc:
            if attempt < retries:
                time.sleep(0.25 * (2 ** attempt))
                continue
            raise ReachError("network_error", "The upstream request failed or timed out.", retryable=True) from exc
    raise ReachError("network_error", "The upstream request failed.", retryable=True)


async def get_json(
    url: str,
    *,
    allowed_hosts: set[str] | frozenset[str],
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 12.0,
    retries: int = 1,
    assume_gzip: bool = False,
    json_error_statuses: frozenset[int] = frozenset(),
    public_instance: bool = False,
) -> Any:
    return await asyncio.to_thread(
        _fetch_json_sync,
        url,
        allowed_hosts,
        params,
        headers,
        timeout,
        retries,
        assume_gzip,
        json_error_statuses,
        public_instance,
    )
