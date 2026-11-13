"""Owner-bound, read-only TikTok Display API adapter.

Calls the official `/v2/user/info/` and `/v2/video/list/` routes only. Each
operation requires a token from the local `TIKTOK_ACCESS_TOKEN` environment
variable and an expected `open_id`; the adapter verifies that the token resolves
to that exact account. Tokens are never accepted in MCP/CLI tool arguments or
returned in results.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_json
from universal_reach.models import AccessInfo, MediaItem, Pagination, Record, ResultPage
from universal_reach.security import validate_https_url


API_HOST = "open.tiktokapis.com"
API_ROOT = f"https://{API_HOST}/v2"
API_HOSTS = frozenset({API_HOST})
USER_INFO_URL = f"{API_ROOT}/user/info/"
VIDEO_LIST_URL = f"{API_ROOT}/video/list/"
_USER_FIELDS = "open_id,display_name,avatar_url"
_VIDEO_FIELDS = "id,create_time,title,video_description,share_url,cover_image_url,embed_link,like_count,comment_count,share_count,view_count"
_MAX_LIMIT = 100
_API_MAX_COUNT = 20
_MAX_TOKEN_LENGTH = 8192
_MAX_RESPONSE_BYTES = 2_000_000
_OPEN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_VIDEO_ID_RE = re.compile(r"^[0-9]{1,32}$")
_JSON_ERROR_STATUSES = frozenset({400, 401, 403, 429, 500, 502, 503})


class TikTokAdapter(Adapter):
    """Read the profile and recent videos for a specifically authorized owner.

    The `username` argument is intentionally the TikTok `open_id`, not a
    discoverable username. The token is read only from `TIKTOK_ACCESS_TOKEN` in
    the local process environment; token contents are never accepted in tool
    arguments or returned.
    """

    url_hosts = frozenset({"www.tiktok.com", "tiktok.com"})

    def __init__(self) -> None:
        super().__init__(adapter_capability("tiktok", ("profile", "profile_posts")))

    async def profile(self, username: str, options: dict[str, Any] | None = None) -> Record:
        expected_open_id = _validate_open_id(username)
        token = _access_token(options)
        user = await self._verified_user(expected_open_id, token)
        avatar = _https_url(user.get("avatar_url"), allowed_suffixes=("tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com"))
        media = [MediaItem(kind="image", url=avatar, title="TikTok profile avatar")] if avatar else []
        return Record(
            platform=self.platform,
            type="profile",
            id=expected_open_id,
            author=_string(user.get("display_name")),
            title=_string(user.get("display_name")),
            media=media,
            access=AccessInfo(mode="authenticated", complete=True),
            extensions={"open_id": expected_open_id},
        )

    async def profile_posts(
        self,
        username: str,
        limit: int = 20,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        expected_open_id = _validate_open_id(username)
        limit = _validate_limit(limit)
        numeric_cursor = _validate_cursor(cursor)
        token = _access_token(options)
        await self._verified_user(expected_open_id, token)

        body: dict[str, int] = {"max_count": min(limit, _API_MAX_COUNT)}
        if numeric_cursor is not None:
            body["cursor"] = numeric_cursor
        payload = await _post_json(
            VIDEO_LIST_URL,
            params={"fields": _VIDEO_FIELDS},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            body=body,
        )
        data = _response_data(payload, "video list")
        raw_videos = data.get("videos")
        has_more = data.get("has_more")
        if (
            not isinstance(raw_videos, list)
            or len(raw_videos) > min(limit, _API_MAX_COUNT)
            or not isinstance(has_more, bool)
        ):
            raise _parser_error("video list")
        next_cursor = _cursor_number(data.get("cursor"), required=has_more)
        records = [_video_record(item, expected_open_id) for item in raw_videos]
        warnings = []
        if limit > _API_MAX_COUNT:
            warnings.append(
                f"TikTok's Display API returns at most {_API_MAX_COUNT} videos per page; this request was capped at that maximum."
            )
        return ResultPage(
            platform=self.platform,
            operation="profile_posts",
            items=records,
            pagination=Pagination(
                cursor=str(next_cursor) if has_more and next_cursor is not None else None,
                has_more=has_more,
                detail="TikTok's cursor is a UTC Unix timestamp in milliseconds; pass it back unchanged for the next page.",
            ),
            complete=not has_more,
            warnings=warnings,
            extensions={"owner_open_id": expected_open_id, "api_max_count": _API_MAX_COUNT},
        )

    async def _verified_user(self, expected_open_id: str, token: str) -> dict[str, Any]:
        try:
            payload = await get_json(
                USER_INFO_URL,
                allowed_hosts=API_HOSTS,
                params={"fields": _USER_FIELDS},
                headers={"Authorization": f"Bearer {token}"},
                json_error_statuses=_JSON_ERROR_STATUSES,
            )
        except ReachError as exc:
            raise _translate_http_error(exc) from exc
        data = _response_data(payload, "user info")
        user = data.get("user")
        if not isinstance(user, Mapping) or not isinstance(user.get("open_id"), str):
            raise _parser_error("user info")
        if user["open_id"] != expected_open_id:
            raise ReachError(
                "account_mismatch",
                "The supplied access token belongs to a different TikTok account than the requested open_id.",
                details={"expected_open_id": expected_open_id},
            )
        return dict(user)


def _access_token(options: dict[str, Any] | None) -> str:
    if isinstance(options, dict) and "access_token" in options:
        raise ReachError(
            "invalid_argument",
            "Do not pass TikTok tokens in tool options; set TIKTOK_ACCESS_TOKEN in the local process environment.",
        )
    token = os.environ.get("TIKTOK_ACCESS_TOKEN", "").strip()
    if (
        not isinstance(token, str)
        or not token
        or len(token) > _MAX_TOKEN_LENGTH
        or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in token)
    ):
        raise ReachError(
            "authentication_required",
            "Set TIKTOK_ACCESS_TOKEN in the local process environment to use this owner-bound TikTok operation.",
        )
    return token


def _validate_open_id(value: str) -> str:
    if not isinstance(value, str) or not _OPEN_ID_RE.fullmatch(value.strip()):
        raise ReachError("invalid_request", "username must be the TikTok open_id returned for this app and account.")
    return value.strip()


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIMIT:
        raise ReachError("invalid_request", f"Limit must be an integer from 1 to {_MAX_LIMIT}.")
    return limit


def _validate_cursor(cursor: str | None) -> int | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 19:
        raise ReachError("invalid_cursor", "The TikTok cursor must be a non-negative 64-bit millisecond timestamp.")
    value = int(cursor)
    if value > 9_223_372_036_854_775_807:
        raise ReachError("invalid_cursor", "The TikTok cursor is outside the supported 64-bit range.")
    return value


def _cursor_number(value: Any, *, required: bool) -> int | None:
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 9_223_372_036_854_775_807:
        raise _parser_error("video pagination")
    return value


def _response_data(payload: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise _parser_error(context)
    error = payload.get("error")
    if not isinstance(error, Mapping) or not isinstance(error.get("code"), str):
        raise _parser_error(context)
    code = error["code"]
    if code != "ok":
        _raise_api_error(code, error.get("message"), error.get("log_id"))
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise _parser_error(context)
    return data


def _raise_api_error(code: str, message: Any, log_id: Any, *, status: int | None = None) -> None:
    details: dict[str, Any] = {"provider_code": code}
    if isinstance(log_id, str) and len(log_id) <= 128:
        details["log_id"] = log_id
    if status is not None:
        details["http_status"] = status
    description = message.strip()[:500] if isinstance(message, str) else ""
    if code == "access_token_invalid":
        raise ReachError("authentication_failed", "TikTok rejected the access token.", details=details)
    if code in {"scope_not_authorized", "scope_permission_missed"}:
        raise ReachError("permission_denied", "The TikTok token is missing a required Display API scope.", details=details)
    if code == "rate_limit_exceeded":
        raise ReachError("rate_limited", "TikTok rate-limited this request.", retryable=True, details=details)
    if code == "internal_error":
        raise ReachError("temporarily_unavailable", "TikTok reported an internal API error.", retryable=True, details=details)
    if code == "invalid_params":
        raise ReachError("upstream_error", "TikTok rejected the request parameters.", details=details)
    if status is not None and status == 401:
        raise ReachError("authentication_failed", "TikTok rejected the access token.", details=details)
    if status is not None and status == 403:
        raise ReachError("permission_denied", "TikTok denied this Display API request.", details=details)
    raise ReachError(
        "upstream_error",
        "TikTok returned an API error." + (f" {description}" if description else ""),
        retryable=status is not None and status >= 500,
        details=details,
    )


def _translate_http_error(error: ReachError) -> ReachError:
    status = error.details.get("http_status")
    if status == 401:
        return ReachError("authentication_failed", "TikTok rejected the access token.", details={"http_status": 401})
    if status == 403:
        return ReachError("permission_denied", "TikTok denied this Display API request.", details={"http_status": 403})
    if status == 429:
        return ReachError("rate_limited", "TikTok rate-limited this request.", retryable=True, details={"http_status": 429})
    if isinstance(status, int) and status >= 500:
        return ReachError("temporarily_unavailable", "TikTok is temporarily unavailable.", retryable=True, details={"http_status": status})
    return error


def _video_record(value: Any, owner_open_id: str) -> Record:
    if not isinstance(value, Mapping):
        raise _parser_error("video list item")
    video_id = value.get("id")
    if not isinstance(video_id, str) or not _VIDEO_ID_RE.fullmatch(video_id):
        raise _parser_error("video ID")
    share_url = _https_url(value.get("share_url"), allowed_suffixes=("tiktok.com",))
    embed_url = _https_url(value.get("embed_link"), allowed_suffixes=("tiktok.com",))
    cover_url = _https_url(
        value.get("cover_image_url"),
        allowed_suffixes=("tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com"),
    )
    created_at = _created_at(value.get("create_time"))
    metrics = {
        name: count
        for field, name in (
            ("like_count", "likes"),
            ("comment_count", "comments"),
            ("share_count", "shares"),
            ("view_count", "views"),
        )
        if (count := _nonnegative_int(value.get(field))) is not None
    }
    media = [MediaItem(kind="image", url=cover_url, title="TikTok video cover", metadata={"expires_after_seconds": 21600})] if cover_url else []
    return Record(
        platform="tiktok",
        type="video",
        id=video_id,
        url=share_url,
        author=owner_open_id,
        title=_string(value.get("title")),
        text=_string(value.get("video_description")),
        created_at=created_at,
        metrics=metrics,
        media=media,
        access=AccessInfo(mode="authenticated", complete=True),
        extensions={
            "owner_open_id": owner_open_id,
            "duration_seconds": _nonnegative_int(value.get("duration")),
            "embed_link": embed_url,
        },
    )


def _created_at(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _https_url(value: Any, *, allowed_suffixes: tuple[str, ...]) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    allowed = any(host == suffix or host.endswith("." + suffix) for suffix in allowed_suffixes)
    if parsed.scheme != "https" or not allowed or parsed.username is not None or parsed.password is not None or port not in (None, 443):
        return None
    return value


def _parser_error(context: str) -> ReachError:
    return ReachError("parser_error", f"TikTok returned malformed {context} data.")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _post_json_sync(
    url: str,
    *,
    params: dict[str, str],
    headers: dict[str, str],
    body: dict[str, int],
    timeout: float = 12.0,
) -> Any:
    validate_https_url(url, API_HOSTS)
    parts = urlsplit(url)
    query = urlencode(params)
    request_url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    encoded_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = Request(request_url, data=encoded_body, headers=headers, method="POST")
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return _decode_response(response.read(_MAX_RESPONSE_BYTES + 1), response.status, response.headers.get("Content-Type"))
    except HTTPError as exc:
        raw = exc.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ReachError("response_too_large", "TikTok's API response exceeded the configured size limit.") from exc
        parsed = _decode_json(raw, exc.code)
        return parsed
    except ReachError:
        raise
    except (TimeoutError, URLError, OSError) as exc:
        raise ReachError("network_error", "The TikTok API request failed or timed out.", retryable=True) from exc


async def _post_json(
    url: str,
    *,
    params: dict[str, str],
    headers: dict[str, str],
    body: dict[str, int],
) -> Any:
    return await asyncio.to_thread(_post_json_sync, url, params=params, headers=headers, body=body)


def _decode_response(raw: bytes, status: int, content_type: str | None) -> Any:
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ReachError("response_too_large", "TikTok's API response exceeded the configured size limit.")
    return _decode_json(raw, status, content_type)


def _decode_json(raw: bytes, status: int, content_type: str | None = None) -> Any:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if status >= 500:
            raise ReachError("temporarily_unavailable", "TikTok is temporarily unavailable.", retryable=True, details={"http_status": status}) from exc
        raise ReachError("parser_error", "TikTok returned malformed JSON.", details={"http_status": status}) from exc
    if not isinstance(payload, Mapping):
        raise ReachError("parser_error", "TikTok returned a non-object JSON response.", details={"http_status": status})
    error = payload.get("error")
    if isinstance(error, Mapping) and isinstance(error.get("code"), str) and error["code"] != "ok":
        _raise_api_error(error["code"], error.get("message"), error.get("log_id"), status=status)
    if status < 200 or status >= 300:
        raise ReachError("upstream_error", "TikTok returned an unexpected HTTP status.", details={"http_status": status})
    return payload
