"""Anonymous read-only adapter for the official Hacker News Firebase API."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError, UnsupportedOperation
from universal_reach.http import get_json
from universal_reach.models import AccessInfo, AdapterStatus, Pagination, Record, ResultPage
from universal_reach.security import validate_https_url

API_BASE = "https://hacker-news.firebaseio.com/v0"
API_HOSTS = frozenset({"hacker-news.firebaseio.com"})
URL_HOSTS = frozenset({"news.ycombinator.com"})
_ID_RE = re.compile(r"^[1-9][0-9]{0,15}$")
_USER_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_LIMIT = 100
_CHILD_FETCH_CONCURRENCY = 8


class HackerNewsAdapter(Adapter):
    """Read HN items and public profiles without an account or API key.

    The official Firebase API has no search route, so this adapter deliberately
    does not implement a synthetic or third-party search operation.
    """

    url_hosts = URL_HOSTS

    def __init__(self) -> None:
        operations = ("read", "replies", "profile", "profile_posts")
        super().__init__(adapter_capability("hackernews", operations))

    async def search(
        self, query: str, limit: int = 10, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        raise UnsupportedOperation(self.platform, "search")

    async def _item(self, item_id: str) -> dict[str, Any] | None:
        if not _ID_RE.fullmatch(item_id):
            raise ReachError("invalid_input", "Hacker News item IDs must be positive integers.")
        value = await get_json(f"{API_BASE}/item/{item_id}.json", allowed_hosts=API_HOSTS)
        if value is not None and not isinstance(value, dict):
            raise ReachError("parser_error", "The Hacker News API returned an invalid item.")
        return value

    @staticmethod
    def _created_at(value: Any) -> str | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return None
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None

    @staticmethod
    def _item_url(item_id: str) -> str:
        return f"https://news.ycombinator.com/item?id={item_id}"

    def _record(self, item: dict[str, Any]) -> Record:
        item_id = str(item.get("id", ""))
        if not _ID_RE.fullmatch(item_id):
            raise ReachError("parser_error", "The Hacker News API returned an item without a valid ID.")
        original_url = item.get("url") if isinstance(item.get("url"), str) else None
        text = item.get("text") if isinstance(item.get("text"), str) else None
        metrics: dict[str, int | float | str | None] = {}
        for key in ("score", "descendants"):
            value = item.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics[key] = value
        kids = item.get("kids")
        kid_ids = [str(kid) for kid in kids if isinstance(kid, int) and not isinstance(kid, bool) and kid > 0] if isinstance(kids, list) else []
        extensions: dict[str, Any] = {
            "item_type": item.get("type"),
            "deleted": bool(item.get("deleted", False)),
            "dead": bool(item.get("dead", False)),
            "comment_ids": kid_ids,
        }
        if original_url:
            extensions["external_url"] = original_url
        if isinstance(item.get("parts"), list):
            extensions["parts"] = item["parts"]
        return Record(
            platform=self.platform,
            type=str(item.get("type") or "item"),
            id=item_id,
            url=self._item_url(item_id),
            author=item.get("by") if isinstance(item.get("by"), str) else None,
            title=item.get("title") if isinstance(item.get("title"), str) else None,
            text=text,
            created_at=self._created_at(item.get("time")),
            metrics=metrics,
            access=AccessInfo(mode="anonymous", complete=True),
            extensions=extensions,
        )

    def _parse_item_url(self, url: str) -> str:
        validate_https_url(url, URL_HOSTS)
        parsed = urlsplit(url)
        if parsed.path != "/item":
            raise ReachError("invalid_url", "Expected a Hacker News item URL like https://news.ycombinator.com/item?id=123.")
        item_id = parse_qs(parsed.query).get("id", [""])[0]
        if not _ID_RE.fullmatch(item_id):
            raise ReachError("invalid_url", "The Hacker News item URL has no valid id parameter.")
        return item_id

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        item_id = self._parse_item_url(url)
        item = await self._item(item_id)
        if item is None or item.get("deleted") or item.get("dead"):
            raise ReachError("not_found", "The Hacker News item was not found or is no longer publicly available.", details={"id": item_id})
        return self._record(item)

    @staticmethod
    def _validated_limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise ReachError("invalid_input", f"limit must be between 1 and {MAX_LIMIT}.")
        return limit

    @staticmethod
    def _validated_cursor(cursor: str | None) -> int:
        if cursor is None:
            return 0
        if not cursor.isdecimal():
            raise ReachError("invalid_input", "The Hacker News cursor must be a non-negative offset.")
        offset = int(cursor)
        if offset > 1_000_000:
            raise ReachError("invalid_input", "The Hacker News cursor is outside the supported range.")
        return offset

    async def _get_items(self, item_ids: list[int]) -> tuple[list[dict[str, Any]], list[str]]:
        semaphore = asyncio.Semaphore(_CHILD_FETCH_CONCURRENCY)

        async def fetch(item_id: int) -> dict[str, Any] | None:
            async with semaphore:
                value = await self._item(str(item_id))
                return value

        values = await asyncio.gather(*(fetch(item_id) for item_id in item_ids), return_exceptions=True)
        items: list[dict[str, Any]] = []
        warnings: list[str] = []
        for item_id, value in zip(item_ids, values, strict=True):
            if isinstance(value, Exception):
                warnings.append(f"Comment {item_id} could not be retrieved.")
            elif value is None or value.get("deleted") or value.get("dead"):
                warnings.append(f"Comment {item_id} is missing or deleted.")
            else:
                items.append(value)
        return items, warnings

    async def replies(
        self, url: str, limit: int = 50, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        limit = self._validated_limit(limit)
        offset = self._validated_cursor(cursor)
        item_id = self._parse_item_url(url)
        parent = await self._item(item_id)
        if parent is None or parent.get("deleted") or parent.get("dead"):
            raise ReachError("not_found", "The Hacker News item was not found or is no longer publicly available.", details={"id": item_id})
        raw_kids = parent.get("kids")
        kids = [kid for kid in raw_kids if isinstance(kid, int) and not isinstance(kid, bool) and kid > 0] if isinstance(raw_kids, list) else []
        selected_ids = kids[offset:offset + limit]
        raw_items, warnings = await self._get_items(selected_ids) if selected_ids else ([], [])
        next_offset = offset + len(selected_ids)
        has_more = next_offset < len(kids)
        page = ResultPage(
            platform=self.platform,
            operation="replies",
            items=[self._record(item) for item in raw_items],
            pagination=Pagination(
                cursor=str(next_offset) if has_more else None,
                has_more=has_more,
                detail="Cursor is an offset into the parent item's ordered comment ID list.",
            ),
            complete=not warnings and not has_more,
            warnings=warnings,
            extensions={"parent_id": item_id, "source_comment_count": len(kids)},
        )
        return page

    @staticmethod
    def _validate_username(username: str) -> str:
        if not isinstance(username, str) or not _USER_RE.fullmatch(username):
            raise ReachError("invalid_input", "Hacker News usernames may contain letters, numbers, underscores, or hyphens.")
        return username

    async def _user(self, username: str) -> dict[str, Any]:
        username = self._validate_username(username)
        value = await get_json(f"{API_BASE}/user/{username}.json", allowed_hosts=API_HOSTS)
        if value is None:
            raise ReachError("not_found", "The Hacker News profile was not found.", details={"username": username})
        if not isinstance(value, dict):
            raise ReachError("parser_error", "The Hacker News API returned an invalid profile.")
        if value.get("deleted"):
            raise ReachError("not_found", "The Hacker News profile was deleted.", details={"username": username})
        return value

    async def profile(self, username: str, options: dict[str, Any] | None = None) -> Record:
        user = await self._user(username)
        user_id = user.get("id") if isinstance(user.get("id"), str) else username
        created_at = self._created_at(user.get("created"))
        karma = user.get("karma") if isinstance(user.get("karma"), int) and not isinstance(user.get("karma"), bool) else None
        submitted = user.get("submitted") if isinstance(user.get("submitted"), list) else []
        return Record(
            platform=self.platform,
            type="profile",
            id=user_id,
            url=f"https://news.ycombinator.com/user?id={user_id}",
            author=user_id,
            text=user.get("about") if isinstance(user.get("about"), str) else None,
            created_at=created_at,
            metrics={"karma": karma, "submitted_count": len(submitted)},
            access=AccessInfo(mode="anonymous", complete=True),
            extensions={"delay": user.get("delay")},
        )

    async def profile_posts(
        self, username: str, limit: int = 20, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        limit = self._validated_limit(limit)
        offset = self._validated_cursor(cursor)
        user = await self._user(username)
        submitted = user.get("submitted") if isinstance(user.get("submitted"), list) else []
        ids = [item_id for item_id in submitted if isinstance(item_id, int) and not isinstance(item_id, bool) and item_id > 0]
        selected_ids = ids[offset:offset + limit]
        items, warnings = await self._get_items(selected_ids) if selected_ids else ([], [])
        next_offset = offset + len(selected_ids)
        has_more = next_offset < len(ids)
        return ResultPage(
            platform=self.platform,
            operation="profile_posts",
            items=[self._record(item) for item in items],
            pagination=Pagination(
                cursor=str(next_offset) if has_more else None,
                has_more=has_more,
                detail="Cursor is an offset into the user's submitted item IDs.",
            ),
            complete=not warnings and not has_more,
            warnings=warnings,
            extensions={"username": user.get("id", username), "source_item_count": len(ids)},
        )

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        del options
        try:
            value = await get_json(f"{API_BASE}/maxitem.json", allowed_hosts=API_HOSTS, timeout=8.0, retries=0)
            if isinstance(value, int) and value > 0:
                return AdapterStatus(
                    platform=self.platform,
                    status="available",
                    message="The Hacker News Firebase API responded to a live check.",
                    operations=list(self.capability.operations),
                    access_mode="anonymous",
                )
            return AdapterStatus(
                platform=self.platform,
                status="degraded",
                message="The API responded with an unexpected maxitem value.",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )
        except ReachError as exc:
            return AdapterStatus(
                platform=self.platform,
                status="unavailable",
                message=f"Hacker News health check failed ({exc.code}).",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )
