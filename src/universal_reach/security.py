"""Input and URL validation for outbound read-only requests."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from universal_reach.errors import ReachError


def validate_https_url(url: str, allowed_hosts: set[str] | frozenset[str]) -> str:
    """Validate an outbound URL against an adapter's explicit host allowlist."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise ReachError("invalid_url", "The URL is malformed.") from exc

    if parsed.scheme.lower() != "https" or not host:
        raise ReachError("invalid_url", "Only absolute HTTPS URLs are supported.")
    if parsed.username is not None or parsed.password is not None:
        raise ReachError("invalid_url", "URLs containing credentials are not allowed.")
    if port not in (None, 443):
        raise ReachError("invalid_url", "Non-standard HTTPS ports are not allowed.")
    if host not in allowed_hosts:
        raise ReachError(
            "invalid_url",
            "The URL host is not supported by this platform adapter.",
            details={"host": host},
        )
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and not ip.is_global:
        raise ReachError("invalid_url", "Private or reserved network addresses are not allowed.")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost") or host.endswith(".local"):
        raise ReachError("invalid_url", "Local network hosts are not allowed.")
    return url


def validate_public_instance_url(url: str) -> tuple[str, frozenset[str]]:
    """Validate a configurable HTTPS instance and reject private destinations."""
    try:
        parsed = urlsplit(url.strip().rstrip("/"))
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise ReachError("invalid_url", "The instance URL is malformed.") from exc
    if parsed.scheme.lower() != "https" or not host:
        raise ReachError("invalid_url", "Configured instances must use HTTPS.")
    if parsed.username is not None or parsed.password is not None:
        raise ReachError("invalid_url", "Instance URLs containing credentials are not allowed.")
    if port not in (None, 443):
        raise ReachError("invalid_url", "Non-standard HTTPS ports are not allowed.")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ReachError("invalid_url", "Configure an instance origin without a path, query, or fragment.")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        raise ReachError("invalid_url", "Local network hosts are not allowed.")
    resolve_public_addresses(host, 443)
    authority_host = f"[{host}]" if ":" in host else host
    return f"https://{authority_host}", frozenset({host})


def resolve_public_addresses(host: str, port: int = 443) -> list[tuple[int, int, int, str, tuple]]:
    """Resolve a host and return only globally routable socket addresses.

    Configurable instance requests use this list to connect to the exact IP
    that was checked while retaining the hostname for TLS certificate checks.
    """
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise ReachError("invalid_url", "Instances resolving to private or reserved addresses are not allowed.")
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        sockaddr = (str(ip), port, 0, 0) if ip.version == 6 else (str(ip), port)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]
    except ValueError:
        try:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, ValueError) as exc:
            raise ReachError("invalid_instance", "The instance host could not be resolved to a public address.") from exc
    if not records:
        raise ReachError("invalid_instance", "The instance host could not be resolved to a public address.")
    try:
        addresses = [ipaddress.ip_address(row[4][0]) for row in records]
    except (IndexError, TypeError, ValueError) as exc:
        raise ReachError("invalid_instance", "The instance host resolved to an invalid address.") from exc
    if any(not address.is_global for address in addresses):
        raise ReachError("invalid_url", "Instances resolving to private or reserved addresses are not allowed.")
    return records
