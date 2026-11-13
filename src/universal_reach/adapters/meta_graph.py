"""Shared, read-only helpers for owner-authorized Meta Graph API adapters."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from universal_reach.errors import ReachError
from universal_reach.http import get_json


META_GRAPH_HOST = "graph.facebook.com"
META_GRAPH_ROOT = f"https://{META_GRAPH_HOST}/v27.0"
THREADS_GRAPH_HOST = "graph.threads.net"
THREADS_GRAPH_ROOT = f"https://{THREADS_GRAPH_HOST}/v1.0"

_ID_RE = re.compile(r"^[0-9]{1,40}$")


def configured_value(env_name: str) -> str:
    value = os.environ.get(env_name, "").strip()
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ReachError(
            "authentication_required",
            f"Set {env_name} to use this owner-authorized Meta API operation.",
            details={"environment_variable": env_name},
        )
    return value


def configured_id(env_name: str) -> str:
    value = configured_value(env_name)
    if not _ID_RE.fullmatch(value):
        raise ReachError(
            "invalid_configuration",
            f"{env_name} must contain a numeric Meta account or Page ID.",
            details={"environment_variable": env_name},
        )
    return value


def requested_account(value: str, configured_id_value: str, env_handle: str | None = None) -> str:
    """Require the operation's account argument to identify its configured owner account."""
    if not isinstance(value, str) or not value.strip():
        raise ReachError("invalid_request", "A configured Meta account ID or username is required.")
    actor = value.strip()
    if actor.startswith("@"):
        actor = actor[1:]
    if actor == configured_id_value:
        return actor
    if env_handle:
        if actor.casefold() == env_handle.lstrip("@").strip().casefold():
            return actor
    raise ReachError(
        "account_mismatch",
        "This adapter only reads the Page or professional account configured for its owner-authorized token.",
    )


def validate_limit(limit: int, *, maximum: int, platform: str) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise ReachError(
            "invalid_limit",
            f"{platform} page size must be between 1 and {maximum}.",
            details={"limit": limit, "maximum": maximum},
        )
    return limit


def validate_cursor(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    if (
        not isinstance(cursor, str)
        or not cursor.strip()
        or len(cursor) > 4096
        or any(ord(char) < 33 or ord(char) == 127 for char in cursor)
    ):
        raise ReachError("invalid_cursor", "The Meta pagination cursor is malformed.")
    return cursor


async def graph_get(
    *,
    root: str,
    host: str,
    path: str,
    token: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """Request a fixed Meta host and translate Graph's JSON error envelope."""
    if path.startswith("/") or not path or ".." in path.split("/"):
        raise ReachError("invalid_request", "The Meta API path is invalid.")
    query = dict(params or {})
    payload = await get_json(
        f"{root}/{path}",
        allowed_hosts=frozenset({host}),
        params=query,
        headers={"Authorization": f"Bearer {token}"},
        json_error_statuses=frozenset({400}),
    )
    if isinstance(payload, Mapping) and isinstance(payload.get("error"), Mapping):
        error = payload["error"]
        raw_code = error.get("code")
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            code = None
        if code == 190:
            raise ReachError(
                "authentication_required",
                "Meta rejected the configured access token; refresh or replace it.",
                details={"graph_error_code": code},
            )
        if code in {10, 200, 283}:
            raise ReachError(
                "permission_required",
                "Meta denied this read. Confirm the token's required read scopes and the owner's Page or account role.",
                details={"graph_error_code": code},
            )
        if code in {4, 17, 32, 613, 80001, 80002}:
            raise ReachError(
                "rate_limited",
                "Meta rate-limited this read; retry later.",
                retryable=True,
                details={"graph_error_code": code},
            )
        if code == 2:
            raise ReachError(
                "temporarily_unavailable",
                "Meta temporarily rejected this Graph API request.",
                retryable=True,
                details={"graph_error_code": code},
            )
        if code in {100, 110}:
            raise ReachError(
                "invalid_request",
                "Meta rejected the requested object or fields.",
                details={"graph_error_code": code},
            )
        if code in {33, 210}:
            raise ReachError(
                "private_or_inaccessible",
                "The requested Meta object is unavailable to this authorized account.",
                details={"graph_error_code": code},
            )
        raise ReachError(
            "upstream_error",
            "Meta returned an unrecognized Graph API error.",
            details={"graph_error_code": code},
        )
    return payload


def object_payload(payload: Any, *, context: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ReachError("parser_error", f"Meta returned malformed {context} data.")
    return payload


def collection_payload(payload: Any, *, context: str) -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
    result = object_payload(payload, context=context)
    data = result.get("data")
    if not isinstance(data, list) or any(not isinstance(item, Mapping) for item in data):
        raise ReachError("parser_error", f"Meta returned malformed {context} data.")
    paging = result.get("paging")
    return data, paging if isinstance(paging, Mapping) else {}


def page_cursor(paging: Mapping[str, Any], *, row_count: int, limit: int) -> tuple[str | None, bool, str]:
    cursors = paging.get("cursors")
    cursors = cursors if isinstance(cursors, Mapping) else {}
    after = cursors.get("after")
    after = after if isinstance(after, str) and after and len(after) <= 4096 else None
    has_next_url = isinstance(paging.get("next"), str) and bool(paging.get("next"))
    has_more = has_next_url or (row_count >= limit and after is not None)
    detail = "Meta's next link or a full page with an after cursor indicates another page; the next link is never followed directly."
    return (after if has_more else None), has_more, detail


def safe_https_url(value: Any, *, allowed_hosts: frozenset[str] | None = None) -> str | None:
    if not isinstance(value, str) or len(value) > 8192:
        return None
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    if allowed_hosts is not None and host not in allowed_hosts:
        return None
    return value


def metric_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None
