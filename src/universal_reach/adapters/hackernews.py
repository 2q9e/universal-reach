"""Anonymous Hacker News adapter: Firebase reads and Algolia-backed search."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_json
from universal_reach.models import AccessInfo, AdapterStatus, Pagination, Record, ResultPage
from universal_reach.security import validate_https_url

API_BASE = "https://hacker-news.firebaseio.com/v0"
API_HOSTS = frozenset({"hacker-news.firebaseio.com"})
SEARCH_BASE = "https://hn.algolia.com/api/v1"
SEARCH_HOSTS = frozenset({"hn.algolia.com"})
URL_HOSTS = frozenset({"news.ycombinator.com"})
_ID_RE = re.compile(r"^[1-9][0-9]{0,15}$")
_USER_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SEARCH_TAGS_RE = re.compile(r"^[A-Za-z0-9_(),-]{1,256}$")
_NUMERIC_FILTER_RE = re.compile(r"^(?:created_at_i|points|num_comments)(?:<=|>=|=|<|>)[0-9]{1,12}$")
MAX_LIMIT = 100
_CHILD_FETCH_CONCURRENCY = 8


class HackerNewsAdapter(Adapter):
    """Read public HN data without an account or API key.

    Exact items, comments, and profiles use the official Firebase API. Search
    uses the public HN Search service operated by Algolia because Firebase has
    no search endpoint. Search coverage and availability therefore depend on
    that separate index.
    """

    url_hosts = URL_HOSTS

    def __init__(self) -> None:
        operations = ("search", "read", "replies", "profile", "profile_posts")
        super().__init__(adapter_capability("hackernews", operations))

    @staticmethod
    def _search_page(cursor: str | None) -> int:
        if cursor is None:
            return 0
        if not isinstance(cursor, str) or not cursor.isdecimal():
            raise ReachError("invalid_input", "The Hacker News search cursor must be a non-negative page number.")
        page = int(cursor)
        if page > 999:
            raise ReachError("invalid_input", "The Hacker News search cursor is outside the supported range.")
        return page

    @staticmethod
    def _search_options(options: dict[str, Any] | None) -> tuple[str, str | None, str | None]:
        values = {} if options is None else options
        if not isinstance(values, dict):
            raise ReachError("invalid_option", "Hacker News search options must be an object.")
        allowed = {"sort", "tags", "numeric_filters"}
        unknown = set(values) - allowed
        if unknown:
            raise ReachError(
                "invalid_option",
                "Hacker News search received unsupported options.",
                details={"options": sorted(unknown)},
            )

        sort = values.get("sort", "relevance")
        if not isinstance(sort, str) or sort not in {"relevance", "recent"}:
            raise ReachError("invalid_option", "Hacker News search sort must be relevance or recent.")

        tags = values.get("tags")
        if tags is not None:
            if not isinstance(tags, str) or not _SEARCH_TAGS_RE.fullmatch(tags):
                raise ReachError(
                    "invalid_option",
                    "Hacker News tags must use documented tag names, commas, and balanced parentheses.",
                )
            depth = 0
            for char in tags:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth < 0:
                        break
            if depth != 0 or not any(char.isalnum() for char in tags):
                raise ReachError("invalid_option", "Hacker News tags contain invalid grouping syntax.")

        numeric_filters = values.get("numeric_filters")
        if numeric_filters is not None:
            if isinstance(numeric_filters, str):
                filters = numeric_filters.split(",")
            elif isinstance(numeric_filters, list) and all(isinstance(value, str) for value in numeric_filters):
                filters = numeric_filters
            else:
                raise ReachError(
                    "invalid_option",
                    "Hacker News numeric_filters must be a comma-separated string or list of strings.",
                )
            if len(filters) > 20 or not filters or any(not _NUMERIC_FILTER_RE.fullmatch(value) for value in filters):
                raise ReachError(
                    "invalid_option",
                    "Hacker News numeric filters support created_at_i, points, and num_comments comparisons.",
                )
            numeric_filter_value = ",".join(filters)
        else:
            numeric_filter_value = None
        return sort, tags, numeric_filter_value

    @staticmethod
    def _search_created_at(value: Any) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            return None

    def _search_record(self, hit: dict[str, Any]) -> Record:
        raw_id = hit.get("objectID")
        item_id = str(raw_id) if isinstance(raw_id, (int, str)) and not isinstance(raw_id, bool) else ""
        if not _ID_RE.fullmatch(item_id):
            raise ReachError("parser_error", "Hacker News Search returned a hit without a valid item ID.")

        raw_tags = hit.get("_tags") if isinstance(hit.get("_tags"), list) else []
        tags = [tag for tag in raw_tags if isinstance(tag, str)]
        item_type = next((tag for tag in tags if tag in {"story", "comment", "poll", "pollopt", "job"}), "item")
        points = hit.get("points")
        num_comments = hit.get("num_comments")
        metrics: dict[str, int | float | str | None] = {}
        if isinstance(points, (int, float)) and not isinstance(points, bool):
            metrics["score"] = points
        if isinstance(num_comments, (int, float)) and not isinstance(num_comments, bool):
            metrics["descendants"] = num_comments

        text = hit.get("comment_text") if isinstance(hit.get("comment_text"), str) else None
        if text is None and isinstance(hit.get("story_text"), str):
            text = hit["story_text"]
        title = hit.get("title") if isinstance(hit.get("title"), str) else None
        if title is None and isinstance(hit.get("story_title"), str):
            title = hit["story_title"]

        extensions: dict[str, Any] = {
            "item_type": item_type,
            "search_tags": tags,
        }
        for field in ("story_id", "parent_id"):
            value = hit.get(field)
            if isinstance(value, (int, str)) and not isinstance(value, bool):
                extensions[field] = str(value)
        external_url = hit.get("url") if isinstance(hit.get("url"), str) else hit.get("story_url")
        if isinstance(external_url, str) and external_url:
            extensions["external_url"] = external_url

        return Record(
            platform=self.platform,
            type=str(item_type),
            id=item_id,
            url=self._item_url(item_id),
            author=hit.get("author") if isinstance(hit.get("author"), str) else None,
            title=title,
            text=text,
            created_at=self._search_created_at(hit.get("created_at")),
            metrics=metrics,
            access=AccessInfo(mode="anonymous", complete=False),
            extensions=extensions,
        )

    async def search(
        self, query: str, limit: int = 10, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        limit = self._validated_limit(limit)
        page_number = self._search_page(cursor)
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ReachError("invalid_input", "Hacker News search query must contain 1 to 500 characters.")
        query = query.strip()
        sort, tags, numeric_filters = self._search_options(options)
        endpoint = "search_by_date" if sort == "recent" else "search"
        params: dict[str, Any] = {
            "query": query,
            "page": page_number,
            "hitsPerPage": limit,
        }
        if tags is not None:
            params["tags"] = tags
        if numeric_filters is not None:
            params["numericFilters"] = numeric_filters
        response = await get_json(f"{SEARCH_BASE}/{endpoint}", allowed_hosts=SEARCH_HOSTS, params=params)
        if not isinstance(response, dict):
            raise ReachError("parser_error", "Hacker News Search returned an invalid response.")
        if response.get("message"):
            raise ReachError(
                "upstream_error",
                "Hacker News Search could not return the requested page.",
                details={"source": "hn_search"},
            )
        hits = response.get("hits")
        if not isinstance(hits, list) or any(not isinstance(hit, dict) for hit in hits):
            raise ReachError("parser_error", "Hacker News Search returned an invalid hits collection.")

        nb_hits = response.get("nbHits")
        nb_pages = response.get("nbPages")
        hits_per_page = response.get("hitsPerPage")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (nb_hits, nb_pages, hits_per_page)
        ):
            raise ReachError("parser_error", "Hacker News Search returned invalid pagination metadata.")

        next_page = page_number + 1
        has_more = next_page < nb_pages
        capped = nb_hits > nb_pages * hits_per_page
        warnings = ["Hacker News Search reports more matches than its available pages contain."] if capped else []
        return ResultPage(
            platform=self.platform,
            operation="search",
            items=[self._search_record(hit) for hit in hits],
            pagination=Pagination(
                cursor=str(next_page) if has_more else None,
                has_more=has_more,
                detail="Cursor is a zero-based HN Search page number.",
            ),
            complete=not has_more and not capped,
            warnings=warnings,
            extensions={
                "source": "hn_search_algolia",
                "query": query,
                "sort": sort,
                "total_matches": nb_hits,
                "page": page_number,
                "pages": nb_pages,
                "hits_per_page": hits_per_page,
            },
        )

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
        firebase_issue: str | None = None
        search_issue: str | None = None
        try:
            value = await get_json(f"{API_BASE}/maxitem.json", allowed_hosts=API_HOSTS, timeout=8.0, retries=0)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                firebase_issue = "unexpected_response"
        except ReachError as exc:
            firebase_issue = exc.code

        try:
            response = await get_json(
                f"{SEARCH_BASE}/search",
                allowed_hosts=SEARCH_HOSTS,
                params={"query": "universalreach-health-check", "hitsPerPage": 1},
                timeout=8.0,
                retries=0,
            )
            if not isinstance(response, dict) or not isinstance(response.get("hits"), list):
                search_issue = "unexpected_response"
        except ReachError as exc:
            search_issue = exc.code

        if firebase_issue is None and search_issue is None:
            status: Literal["available", "degraded", "unavailable"] = "available"
            message = "The Hacker News Firebase and HN Search APIs responded to live checks."
        elif firebase_issue is None or search_issue is None:
            status = "degraded"
            message = (
                "Only one Hacker News source responded; "
                f"Firebase={firebase_issue or 'available'}, HN Search={search_issue or 'available'}."
            )
        else:
            status = "unavailable"
            message = f"Hacker News live checks failed; Firebase={firebase_issue}, HN Search={search_issue}."
        return AdapterStatus(
            platform=self.platform,
            status=status,
            message=message,
            operations=list(self.capability.operations),
            access_mode="anonymous",
        )
