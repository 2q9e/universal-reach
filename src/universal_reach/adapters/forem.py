"""Anonymous, read-only adapter for public DEV articles and profiles.

DEV is powered by Forem. This adapter uses only documented Forem API v1 GET
routes and deliberately does not send an API key.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_json
from universal_reach.models import AccessInfo, AdapterStatus, MediaItem, Pagination, Record, ResultPage
from universal_reach.security import validate_https_url


_API_ROOT = "https://dev.to/api"
_API_HEADERS = {"Accept": "application/vnd.forem.api-v1+json"}
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


class ForemAdapter(Adapter):
    """Fetch public DEV content through the official anonymous API routes."""

    url_hosts = frozenset({"dev.to"})

    def __init__(self) -> None:
        super().__init__(adapter_capability("devto", ("search", "read", "profile", "profile_posts")))

    async def search(
        self,
        query: str,
        limit: int = 10,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        query = query.strip() if isinstance(query, str) else ""
        if not query:
            raise ReachError("invalid_request", "A non-empty search query is required.")
        limit = _validate_limit(limit)
        page_number = _parse_page(cursor)
        params: dict[str, Any] = {"q": query, "page": page_number, "per_page": limit}
        top = _positive_option(options, "top")
        if top is not None:
            params["top"] = top

        payload = await get_json(
            f"{_API_ROOT}/articles/search",
            allowed_hosts=self.url_hosts,
            params=params,
            headers=_API_HEADERS,
        )
        return _article_page(payload, operation="search", page_number=page_number, limit=limit)

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        del options  # This public endpoint has no supported per-request read options.
        validate_https_url(url, self.url_hosts)
        parts = urlsplit(url)
        segments = [unquote(part) for part in parts.path.strip("/").split("/") if part]
        if len(segments) != 2 or not all(_valid_segment(part) for part in segments):
            raise ReachError(
                "unsupported_url",
                "Provide a public DEV article URL in the form https://dev.to/{username}/{slug}.",
            )
        username, slug = segments
        endpoint = f"{_API_ROOT}/articles/{quote(username, safe='')}/{quote(slug, safe='')}"
        payload = await get_json(endpoint, allowed_hosts=self.url_hosts, headers=_API_HEADERS)
        if not isinstance(payload, dict):
            raise _parser_error("article")
        return _article_record(payload, complete=True)

    async def profile(self, username: str, options: dict[str, Any] | None = None) -> Record:
        del options
        username = _validate_username(username)
        payload = await get_json(
            f"{_API_ROOT}/users/{quote(username, safe='')}",
            allowed_hosts=self.url_hosts,
            headers=_API_HEADERS,
        )
        if not isinstance(payload, dict):
            raise _parser_error("profile")
        return _profile_record(payload, fallback_username=username)

    async def profile_posts(
        self,
        username: str,
        limit: int = 20,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        del options
        username = _validate_username(username)
        limit = _validate_limit(limit)
        page_number = _parse_page(cursor)
        payload = await get_json(
            f"{_API_ROOT}/articles",
            allowed_hosts=self.url_hosts,
            params={"username": username, "page": page_number, "per_page": limit},
            headers=_API_HEADERS,
        )
        return _article_page(payload, operation="profile_posts", page_number=page_number, limit=limit)

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        del options
        try:
            payload = await get_json(
                f"{_API_ROOT}/articles/search",
                allowed_hosts=self.url_hosts,
                params={"q": "universal-reach-health-check", "per_page": 1},
                headers=_API_HEADERS,
                timeout=5.0,
                retries=0,
            )
            if not isinstance(payload, list):
                raise _parser_error("health-check search")
            return AdapterStatus(
                platform=self.platform,
                status="available",
                message="The public Forem API responded to a live check.",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )
        except ReachError as exc:
            return AdapterStatus(
                platform=self.platform,
                status="degraded" if exc.code == "rate_limited" else "unavailable",
                message=f"Forem health check failed ({exc.code}).",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ReachError("invalid_request", "Limit must be an integer from 1 to 100.")
    return limit


def _parse_page(cursor: str | None) -> int:
    if cursor is None:
        return 1
    if not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdecimal():
        raise ReachError("invalid_cursor", "The DEV pagination cursor must be a positive page number.")
    page_number = int(cursor)
    if page_number < 1:
        raise ReachError("invalid_cursor", "The DEV pagination cursor must be a positive page number.")
    return page_number


def _positive_option(options: dict[str, Any] | None, name: str) -> int | None:
    if not options or name not in options or options[name] is None:
        return None
    value = options[name]
    if isinstance(value, bool):
        raise ReachError("invalid_request", f"The {name} option must be a positive integer.")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        parsed = int(value)
    else:
        raise ReachError("invalid_request", f"The {name} option must be a positive integer.")
    if parsed < 1:
        raise ReachError("invalid_request", f"The {name} option must be a positive integer.")
    return parsed


def _valid_segment(value: str) -> bool:
    return value not in {".", ".."} and bool(_SEGMENT_RE.fullmatch(value))


def _validate_username(username: str) -> str:
    if not isinstance(username, str):
        raise ReachError("invalid_request", "A valid DEV username is required.")
    username = username.strip()
    if not _valid_segment(username):
        raise ReachError("invalid_request", "A valid DEV username is required.")
    return username


def _article_page(payload: Any, *, operation: str, page_number: int, limit: int) -> ResultPage:
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise _parser_error("article list")

    rows = payload[:limit]
    records = [_article_record(item, complete=False) for item in rows]
    # The documented list/search routes expose page and per_page, but no total
    # count or has_more flag. A full page is therefore only an estimate.
    has_more = len(payload) >= limit
    pagination = Pagination(
        cursor=str(page_number + 1) if has_more else None,
        has_more=has_more,
        detail=(
            "has_more is inferred from a full page; Forem does not return a total-page count."
            if has_more
            else "Fewer than per_page articles were returned; no further page is inferred."
        ),
    )
    warnings = ["Forem does not expose a total-page count for this route; pagination is inferred from page length."] if has_more else []
    warnings.append("List and search results contain article summaries; use read for the full published article.")
    return ResultPage(
        platform="devto",
        operation=operation,
        items=records,
        pagination=pagination,
        complete=not has_more,
        warnings=warnings,
    )


def _article_record(item: dict[str, Any], *, complete: bool) -> Record:
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    username = _first_string(user.get("username"), item.get("username"), item.get("author_username"))
    slug = _first_string(item.get("slug"))
    article_id = item.get("id")
    if article_id is None:
        article_id = item.get("path") or item.get("url") or slug
    if article_id is None:
        raise _parser_error("article")

    article_url = _first_string(item.get("url"))
    path = _first_string(item.get("path"))
    if not article_url and path and path.startswith("/") and not path.startswith("//"):
        article_url = f"https://dev.to{path}"
    if not article_url and username and slug:
        article_url = f"https://dev.to/{quote(username, safe='')}/{quote(slug, safe='')}"

    tags = item.get("tag_list")
    if not isinstance(tags, list):
        raw_tags = item.get("tags")
        tags = [tag.strip() for tag in raw_tags.split(",") if tag.strip()] if isinstance(raw_tags, str) else []
    else:
        tags = [tag for tag in tags if isinstance(tag, str)]

    metrics: dict[str, int | float | str | None] = {}
    for metric_name, candidates in {
        "reactions": ("public_reactions_count", "positive_reactions_count"),
        "comments": ("comments_count",),
        "reading_time_minutes": ("reading_time_minutes",),
    }.items():
        value = next((item[key] for key in candidates if _is_scalar_metric(item.get(key))), None)
        if value is not None:
            metrics[metric_name] = value

    cover = _first_string(item.get("cover_image"), item.get("social_image"))
    media = [MediaItem(kind="image", url=cover, title="Article cover")] if cover else []
    extensions = {"tags": tags}
    for key in ("canonical_url", "last_comment_at", "organization"):
        value = item.get(key)
        if value is not None:
            extensions[key] = value

    return Record(
        platform="devto",
        type="article",
        id=str(article_id),
        url=article_url,
        author=_first_string(user.get("username"), user.get("name"), username),
        title=_first_string(item.get("title")),
        text=_first_string(item.get("body_markdown"), item.get("description")),
        created_at=_first_string(item.get("published_timestamp"), item.get("published_at"), item.get("created_at")),
        metrics=metrics,
        media=media,
        access=AccessInfo(mode="anonymous", complete=complete),
        extensions=extensions,
    )


def _profile_record(item: dict[str, Any], *, fallback_username: str) -> Record:
    username = _first_string(item.get("username"), fallback_username)
    profile_id = item.get("id")
    if profile_id is None:
        profile_id = username
    extensions: dict[str, Any] = {}
    for key in (
        "location",
        "website_url",
        "twitter_username",
        "github_username",
        "profile_image",
        "profile_image_90",
        "joined_at",
    ):
        if item.get(key) is not None:
            extensions[key] = item[key]
    metrics: dict[str, int | float | str | None] = {}
    for key in ("public_reactions_count", "posts_count", "comments_count"):
        if _is_scalar_metric(item.get(key)):
            metrics[key] = item[key]
    avatar = _first_string(item.get("profile_image"), item.get("profile_image_90"))
    media = [MediaItem(kind="image", url=avatar, title="Profile image")] if avatar else []
    return Record(
        platform="devto",
        type="profile",
        id=str(profile_id),
        url=f"https://dev.to/{quote(username, safe='')}",
        author=username,
        title=_first_string(item.get("name")),
        text=_first_string(item.get("summary"), item.get("bio")),
        created_at=_first_string(item.get("joined_at")),
        metrics=metrics,
        media=media,
        access=AccessInfo(mode="anonymous", complete=True),
        extensions=extensions,
    )


def _is_scalar_metric(value: Any) -> bool:
    return value is not None and not isinstance(value, bool) and isinstance(value, (int, float, str))


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _parser_error(resource: str) -> ReachError:
    return ReachError("parser_error", f"The Forem API returned an unexpected {resource} response.")
