"""Read Pinterest Pins and the token owner's profile through API v5.

Pinterest prohibits automated scraping unless expressly permitted. This
adapter uses only the official API and never calls Pinterest's undocumented
web-resource endpoints. Its reads are limited to the API token's account and
Pins that account owns or is authorized to access.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlsplit

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_json
from universal_reach.models import AccessInfo, AdapterStatus, MediaItem, Pagination, Record, ResultPage

API_HOST = "api.pinterest.com"
API_ROOT = f"https://{API_HOST}/v5"
API_HOSTS = frozenset({API_HOST})
URL_HOSTS = frozenset({"pinterest.com"})
OPERATIONS = ("read", "profile", "profile_posts")
_PIN_ID = re.compile(r"^[0-9]{1,32}$")
_USERNAME = re.compile(r"^[A-Za-z0-9._-]{1,50}$")
_MAX_PAGE_SIZE = 250
_MAX_CURSOR_LENGTH = 4096
_MAX_MEDIA_ITEMS = 20


def _access_token() -> tuple[str, dict[str, str]]:
    token = os.environ.get("PINTEREST_ACCESS_TOKEN", "").strip()
    if not token:
        raise ReachError(
            "pinterest_auth_required",
            "Set PINTEREST_ACCESS_TOKEN to an app-approved Pinterest API access token in the local environment; no request was sent.",
        )
    if len(token) > 8192 or any(char.isspace() for char in token):
        raise ReachError("pinterest_auth_invalid", "PINTEREST_ACCESS_TOKEN is malformed; no request was sent.")
    return token, {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _map_api_error(error: ReachError) -> ReachError:
    status = error.details.get("http_status")
    if status == 401:
        return ReachError("pinterest_authentication_failed", "Pinterest rejected the configured access token.", details={"http_status": 401})
    if status == 403:
        return ReachError(
            "pinterest_access_denied",
            "Pinterest denied this API read. Check app approval, token scopes, and whether the Pin belongs to or is shared with the token account.",
            details={"http_status": 403},
        )
    if status == 404:
        return ReachError("not_found", "Pinterest did not return this Pin for the authorized account.", details={"http_status": 404})
    if status == 429:
        return ReachError("rate_limited", "Pinterest rate-limited this request; wait before retrying.", retryable=True, details={"http_status": 429})
    return error


def _pin_id_from_url(value: str) -> str:
    if not isinstance(value, str):
        raise ReachError("invalid_url", "A Pinterest Pin URL is required.")
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise ReachError("invalid_url", "The Pinterest URL is malformed.") from exc
    if (
        parsed.scheme.casefold() != "https"
        or host not in {"pinterest.com", "www.pinterest.com"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ReachError("invalid_url", "Only HTTPS Pinterest Pin URLs are supported.")

    path = parsed.path.rstrip("/")
    segments = path.lstrip("/").split("/")
    if len(segments) != 2 or segments[0].casefold() != "pin":
        raise ReachError("invalid_url", "Pinterest Pin URLs must use /pin/{numeric_id}/.")
    raw_id = segments[1]
    if re.search(r"%(?:2f|5c)", raw_id, re.IGNORECASE):
        raise ReachError("invalid_url", "Encoded path separators are not allowed in Pinterest Pin URLs.")
    try:
        pin_id = unquote(raw_id, errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReachError("invalid_url", "The Pinterest URL contains malformed path encoding.") from exc
    if not _PIN_ID.fullmatch(pin_id):
        raise ReachError("invalid_url", "The Pinterest URL must contain a numeric Pin ID.")
    return pin_id


def _page_size(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_PAGE_SIZE:
        raise ReachError(
            "invalid_limit",
            f"Pinterest profile_posts requires a page size from 1 to {_MAX_PAGE_SIZE}.",
            details={"minimum": 1, "maximum": _MAX_PAGE_SIZE},
        )
    return limit


def _bookmark(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor.strip() or len(cursor) > _MAX_CURSOR_LENGTH or any(ord(c) < 32 for c in cursor):
        raise ReachError("invalid_cursor", "The Pinterest pagination bookmark is malformed.")
    return cursor


def _next_bookmark(payload: Mapping[str, Any]) -> tuple[str | None, bool]:
    raw = payload.get("bookmark")
    if raw is None or raw == "":
        return None, False
    if isinstance(raw, str) and len(raw) <= _MAX_CURSOR_LENGTH and not any(ord(c) < 32 for c in raw):
        return raw, False
    return None, True


def _safe_text(value: Any, maximum: int) -> str | None:
    return value[:maximum] if isinstance(value, str) else None


def _safe_number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _pinterest_media_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or not (host == "pinimg.com" or host.endswith(".pinimg.com"))
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    return value


def _best_image(images: Any, title: str | None = None) -> MediaItem | None:
    if not isinstance(images, Mapping):
        return None
    for size in ("1200x", "600x", "400x300", "150x150"):
        detail = images.get(size)
        if not isinstance(detail, Mapping):
            continue
        url = _pinterest_media_url(detail.get("url"))
        if not url:
            continue
        metadata = {
            key: value
            for source, key in (("width", "width"), ("height", "height"))
            if (value := _safe_number(detail.get(source))) is not None
        }
        return MediaItem(kind="image", url=url, title=title or "Pinterest Pin image", metadata=metadata)
    return None


def _pin_media(value: Any) -> list[MediaItem]:
    if not isinstance(value, Mapping):
        return []
    media: list[MediaItem] = []
    seen: set[str] = set()

    def add(item: MediaItem | None) -> None:
        if item is not None and item.url and item.url not in seen and len(media) < _MAX_MEDIA_ITEMS:
            seen.add(item.url)
            media.append(item)

    add(_best_image(value.get("images")))
    cover = _pinterest_media_url(value.get("cover_image_url"))
    if cover:
        add(MediaItem(kind="image", url=cover, title="Pinterest Pin video cover"))
    video = _pinterest_media_url(value.get("video_url"))
    if video:
        add(MediaItem(kind="video", url=video, title="Pinterest Pin video"))

    items = value.get("items")
    if isinstance(items, list):
        for item in items[:_MAX_MEDIA_ITEMS]:
            if not isinstance(item, Mapping):
                continue
            add(_best_image(item.get("images"), _safe_text(item.get("title"), 256)))
            item_cover = _pinterest_media_url(item.get("cover_image_url"))
            if item_cover:
                add(MediaItem(kind="image", url=item_cover, title="Pinterest Pin video cover"))
            item_video = _pinterest_media_url(item.get("video_url"))
            if item_video:
                add(MediaItem(kind="video", url=item_video, title="Pinterest Pin video"))

    return media


def _pin_record(payload: Mapping[str, Any], expected_id: str | None = None) -> Record:
    raw_id = payload.get("id")
    pin_id = str(raw_id) if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) else ""
    if not _PIN_ID.fullmatch(pin_id) or (expected_id is not None and pin_id != expected_id):
        raise ReachError("parser_error", "Pinterest returned a Pin with an invalid or mismatched ID.")
    board_owner = payload.get("board_owner") if isinstance(payload.get("board_owner"), Mapping) else {}
    username = board_owner.get("username")
    username = username if isinstance(username, str) and _USERNAME.fullmatch(username) else None
    extensions = {
        key: value
        for source, key, maximum in (
            ("board_id", "board_id", 32),
            ("board_section_id", "board_section_id", 32),
            ("parent_pin_id", "parent_pin_id", 32),
            ("creative_type", "creative_type", 64),
            ("dominant_color", "dominant_color", 32),
            ("alt_text", "alt_text", 500),
        )
        if (value := _safe_text(payload.get(source), maximum)) is not None
    }
    extensions["metadata_source"] = "pinterest_api_v5"
    if username:
        extensions["board_owner_username"] = username
    created_at = _safe_text(payload.get("created_at"), 40)
    return Record(
        platform="pinterest",
        type="pin",
        id=pin_id,
        url=f"https://www.pinterest.com/pin/{pin_id}/",
        author=username,
        title=_safe_text(payload.get("title"), 100),
        text=_safe_text(payload.get("description"), 800),
        created_at=created_at,
        media=_pin_media(payload.get("media")),
        access=AccessInfo(mode="authenticated", complete=True),
        extensions=extensions,
    )


def _account_record(payload: Mapping[str, Any]) -> Record:
    account_id = payload.get("id")
    username = payload.get("username")
    if not isinstance(account_id, str) or not account_id.isdecimal() or not isinstance(username, str) or not _USERNAME.fullmatch(username):
        raise ReachError("parser_error", "Pinterest returned an invalid user-account response.")
    metrics = {
        name: number
        for field, name in (
            ("follower_count", "followers"),
            ("following_count", "following"),
            ("board_count", "boards"),
            ("pin_count", "pins"),
            ("monthly_views", "monthly_views"),
        )
        if (number := _safe_number(payload.get(field))) is not None
    }
    extensions = {
        key: value
        for source, key in (("account_type", "account_type"), ("website_url", "website_url"))
        if isinstance((value := payload.get(source)), str)
    }
    profile_image = _pinterest_media_url(payload.get("profile_image"))
    media = [MediaItem(kind="image", url=profile_image, title="Pinterest profile image")] if profile_image else []
    return Record(
        platform="pinterest",
        type="profile",
        id=account_id,
        url=f"https://www.pinterest.com/{username}/",
        author=username,
        title=_safe_text(payload.get("business_name"), 256) or username,
        text=_safe_text(payload.get("about"), 4000),
        metrics=metrics,
        media=media,
        access=AccessInfo(mode="authenticated", complete=True),
        extensions=extensions,
    )


class PinterestAdapter(Adapter):
    """Read Pins owned by or shared with the explicitly configured API token."""

    url_hosts = URL_HOSTS

    def __init__(self) -> None:
        super().__init__(adapter_capability("pinterest", OPERATIONS))

    @staticmethod
    def _validate_options(options: dict[str, Any] | None) -> None:
        if options is not None and (not isinstance(options, Mapping) or options):
            raise ReachError("invalid_option", "Pinterest does not accept operation options.")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        _token, headers = _access_token()
        try:
            return await get_json(
                f"{API_ROOT}{path}",
                allowed_hosts=API_HOSTS,
                params=params,
                headers=headers,
                retries=0,
            )
        except ReachError as exc:
            raise _map_api_error(exc) from exc

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        self._validate_options(options)
        pin_id = _pin_id_from_url(url)
        payload = await self._get(f"/pins/{pin_id}")
        if not isinstance(payload, Mapping):
            raise ReachError("parser_error", "Pinterest returned a malformed Pin response.")
        return _pin_record(payload, expected_id=pin_id)

    async def profile(self, username: str, options: dict[str, Any] | None = None) -> Record:
        self._validate_options(options)
        if not isinstance(username, str) or username.casefold() != "me":
            raise ReachError("unsupported_profile", "Pinterest API profile reads are limited to username='me' for the configured token account.")
        payload = await self._get("/user_account")
        if not isinstance(payload, Mapping):
            raise ReachError("parser_error", "Pinterest returned a malformed user-account response.")
        return _account_record(payload)

    async def profile_posts(
        self,
        username: str,
        limit: int = 20,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        self._validate_options(options)
        if not isinstance(username, str) or username.casefold() != "me":
            raise ReachError("unsupported_profile", "Pinterest profile_posts is limited to username='me' for the configured token account.")
        limit = _page_size(limit)
        bookmark = _bookmark(cursor)
        params: dict[str, Any] = {"page_size": limit}
        if bookmark is not None:
            params["bookmark"] = bookmark
        payload = await self._get("/pins", params)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
            raise ReachError("parser_error", "Pinterest returned a malformed Pin-list response.")
        records: list[Record] = []
        malformed_count = 0
        for item in payload["items"]:
            if not isinstance(item, Mapping):
                malformed_count += 1
                continue
            try:
                records.append(_pin_record(item))
            except ReachError:
                malformed_count += 1
        next_cursor, malformed_bookmark = _next_bookmark(payload)
        warnings: list[str] = []
        if malformed_count:
            warnings.append(f"Skipped {malformed_count} malformed Pin item(s) returned by Pinterest.")
        if malformed_bookmark:
            warnings.append("Pinterest returned a malformed pagination bookmark; additional results may be missing.")
        return ResultPage(
            platform="pinterest",
            operation="profile_posts",
            items=records,
            pagination=Pagination(cursor=next_cursor, has_more=next_cursor is not None),
            complete=next_cursor is None and malformed_count == 0 and not malformed_bookmark,
            warnings=warnings,
            extensions={"source": "pinterest_api_v5"},
        )

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        del options
        if not os.environ.get("PINTEREST_ACCESS_TOKEN", "").strip():
            return AdapterStatus(
                platform="pinterest",
                status="unavailable",
                message="No Pinterest API access token is configured; no request was sent.",
                operations=list(self.capability.operations),
                access_mode="unknown",
            )
        try:
            payload = await self._get("/user_account")
            if not isinstance(payload, Mapping):
                raise ReachError("parser_error", "Pinterest returned a malformed user-account response.")
            _account_record(payload)
        except ReachError as exc:
            return AdapterStatus(
                platform="pinterest",
                status="unavailable",
                message=f"Pinterest API health check failed ({exc.code}).",
                operations=list(self.capability.operations),
                access_mode="authenticated",
            )
        return AdapterStatus(
            platform="pinterest",
            status="available",
            message="Pinterest API token and user_accounts:read scope are available.",
            operations=list(self.capability.operations),
            access_mode="authenticated",
        )
