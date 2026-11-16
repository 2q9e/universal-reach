"""Read public Steam game announcements from Valve's documented RSS feeds.

Steamworks documents a per-game RSS feed for announcements and explicitly
describes using it on other sites. This adapter searches only the entries
currently present in one explicitly selected game's feed. It does not read
Steam discussions, follow undocumented endpoints, or use the Steam Web API.
"""

from __future__ import annotations

import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from xml.etree import ElementTree

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_text
from universal_reach.models import AccessInfo, AdapterStatus, Pagination, Record, ResultPage


_FEED_BASE = "https://steamcommunity.com/games"
_ALLOWED_HOSTS = frozenset({"steamcommunity.com", "www.steamcommunity.com"})
_APP_ID_RE = re.compile(r"^[1-9][0-9]{0,9}$")
_POSITIVE_INTEGER_RE = re.compile(r"^(?:0|[1-9][0-9]{0,8})$")
_MAX_APP_ID = 4_294_967_295
_MAX_LIMIT = 100
_MAX_CURSOR = 100_000
_HEALTH_APP_ID = "489830"  # Steamworks RSS documentation example


class _VisibleText(HTMLParser):
    """Extract visible text from an RSS description without rendering HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.hidden_depth += 1
        elif tag in {"br", "p", "div", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag in {"p", "div", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":", 1)[-1]


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag).casefold() == name.casefold():
            text = "".join(child.itertext()).strip()
            return text or None
    return None


def _plain_text(value: str | None) -> str | None:
    if not value:
        return None
    parser = _VisibleText()
    parser.feed(value)
    text = re.sub(r"[\t\r\f\v ]+", " ", "".join(parser.parts))
    text = re.sub(r" *\n+ *", "\n", text).strip()
    return text or None


def _published(value: str | None) -> str | None:
    if not value:
        return None
    try:
        timestamp = parsedate_to_datetime(value)
        if timestamp.tzinfo is None:
            return None
        return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        return None


def _entries(document: str) -> list[dict[str, str | None]]:
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", document, re.IGNORECASE):
        raise ReachError("parser_error", "The Steam RSS feed contained a prohibited XML declaration.")
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise ReachError("parser_error", "The Steam endpoint did not return valid RSS XML.") from exc
    if _local_name(root.tag).casefold() != "rss":
        raise ReachError("parser_error", "The Steam endpoint did not return an RSS feed.")
    channel = next((node for node in root if _local_name(node.tag).casefold() == "channel"), None)
    if channel is None:
        raise ReachError("parser_error", "The Steam RSS feed had no channel element.")

    results: list[dict[str, str | None]] = []
    for item in channel:
        if _local_name(item.tag).casefold() != "item":
            continue
        description = _child_text(item, "encoded") or _child_text(item, "description")
        link = _child_text(item, "link")
        guid = _child_text(item, "guid")
        identifier = guid or link
        if not identifier:
            # A feed item without a stable ID or URL cannot be represented as
            # a normalized post without inventing identity.
            continue
        results.append({
            "id": identifier,
            "guid": guid,
            "link": link,
            "title": _plain_text(_child_text(item, "title")),
            "author": _plain_text(_child_text(item, "creator") or _child_text(item, "author")),
            "text": _plain_text(description),
            "published": _child_text(item, "pubDate") or _child_text(item, "date"),
        })
    return results


def _app_id(options: dict[str, Any] | None) -> str:
    value = (options or {}).get("app_id")
    if isinstance(value, bool):
        raise ReachError("invalid_input", "options.app_id must be a positive Steam application ID.")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not _APP_ID_RE.fullmatch(value) or int(value) > _MAX_APP_ID:
        raise ReachError("invalid_input", "options.app_id must be a positive Steam application ID.")
    return value


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_LIMIT:
        raise ReachError("invalid_input", f"limit must be between 1 and {_MAX_LIMIT}.")
    return value


def _offset(value: str | None) -> int:
    if value is None:
        return 0
    if not isinstance(value, str) or not _POSITIVE_INTEGER_RE.fullmatch(value):
        raise ReachError("invalid_input", "cursor must be a non-negative numeric feed offset.")
    offset = int(value)
    if offset > _MAX_CURSOR:
        raise ReachError("invalid_input", "cursor is outside the supported feed range.")
    return offset


class SteamAdapter(Adapter):
    """Search a selected game's current public announcement RSS feed."""

    def __init__(self) -> None:
        super().__init__(adapter_capability("steam", ("search",)))

    async def _feed(self, app_id: str) -> list[dict[str, str | None]]:
        url = f"{_FEED_BASE}/{app_id}/rss"
        document = await get_text(
            url,
            allowed_hosts=_ALLOWED_HOSTS,
            retries=0,
            max_bytes=1_000_000,
        )
        return _entries(document)

    @staticmethod
    def _record(item: dict[str, str | None], app_id: str, feed_url: str) -> Record:
        return Record(
            platform="steam",
            type="announcement",
            id=item["id"] or "",
            url=item["link"],
            author=item["author"],
            title=item["title"],
            text=item["text"],
            created_at=_published(item["published"]),
            access=AccessInfo(mode="anonymous", complete=False),
            extensions={
                "app_id": app_id,
                "guid": item["guid"],
                "feed_url": feed_url,
                "feed_limited": True,
            },
        )

    async def search(
        self,
        query: str,
        limit: int = 10,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ReachError("invalid_input", "query must contain 1 to 500 characters.")
        limit = _limit(limit)
        offset = _offset(cursor)
        app_id = _app_id(options)
        feed_url = f"{_FEED_BASE}/{app_id}/rss"
        entries = await self._feed(app_id)
        needle = query.casefold().strip()
        matches = [
            item for item in entries
            if needle in " ".join(filter(None, (item["title"], item["text"]))).casefold()
        ]
        selected = matches[offset:offset + limit]
        next_offset = offset + len(selected)
        has_more = next_offset < len(matches)
        return ResultPage(
            platform="steam",
            operation="search",
            items=[self._record(item, app_id, feed_url) for item in selected],
            pagination=Pagination(
                cursor=str(next_offset) if has_more else None,
                has_more=has_more,
                detail="Search is limited to the current per-game RSS feed; Steam documents no search pagination or arbitrary discussion history.",
            ),
            complete=False,
            warnings=["Only the entries currently present in the selected game's RSS feed are searched."],
            extensions={"app_id": app_id, "feed_url": feed_url, "feed_limited": True},
        )

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        configured_app_id = (options or {}).get("app_id", _HEALTH_APP_ID)
        try:
            app_id = _app_id({"app_id": configured_app_id})
        except ReachError:
            return AdapterStatus(
                platform=self.platform,
                status="degraded",
                operations=list(self.capability.operations),
                message="The supplied Steam app ID is invalid.",
                access_mode="anonymous",
            )
        try:
            await self._feed(app_id)
        except ReachError as exc:
            return AdapterStatus(
                platform=self.platform,
                status="unavailable" if exc.code != "rate_limited" else "degraded",
                operations=list(self.capability.operations),
                message=f"Steam public RSS health check failed ({exc.code}).",
                access_mode="anonymous",
            )
        return AdapterStatus(
            platform=self.platform,
            status="available",
            operations=list(self.capability.operations),
            message="The selected game's public RSS feed is available.",
            access_mode="anonymous",
        )
