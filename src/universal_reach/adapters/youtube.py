"""Anonymous, read-only access to YouTube's documented channel video feed.

The YouTube Data API requires credentials even for public reads; this adapter
does not use an API key or account credentials. It implements only the public
channel Atom feed documented for
YouTube's push-notification topic URLs. It does not search, fetch arbitrary
video metadata, read comments, or claim to enumerate a channel's full history.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_text
from universal_reach.models import AccessInfo, AdapterStatus, Pagination, Record, ResultPage


FEED_URL = "https://www.youtube.com/xml/feeds/videos.xml"
URL_HOSTS = frozenset({"www.youtube.com"})
_ATOM_NS = "http://www.w3.org/2005/Atom"
_YT_NS = "http://www.youtube.com/xml/schemas/2015"
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{1,126}$")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_LIMIT = 100
_MAX_FEED_BYTES = 500_000
_HEALTH_CHANNEL_ID = "UC_x5XG1OV2P6uZZ5FSM9Ttw"


class YouTubeAdapter(Adapter):
    """Read recent public uploads from an explicitly supplied channel ID."""

    url_hosts = URL_HOSTS

    def __init__(self) -> None:
        super().__init__(adapter_capability("youtube", ("profile_posts",)))

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        del options
        try:
            await self.profile_posts(_HEALTH_CHANNEL_ID, limit=1)
        except ReachError as exc:
            return AdapterStatus(
                platform=self.platform,
                status="degraded" if exc.code == "rate_limited" else "unavailable",
                operations=list(self.capability.operations),
                message=f"YouTube channel-feed health check failed ({exc.code}).",
                access_mode="anonymous",
            )
        return AdapterStatus(
            platform=self.platform,
            status="available",
            operations=list(self.capability.operations),
            message="The documented public YouTube channel feed responded.",
            access_mode="anonymous",
        )

    async def profile_posts(
        self,
        username: str,
        limit: int = 20,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        del options
        channel_id = _validate_channel_id(username)
        limit = _validate_limit(limit)
        offset = _validate_cursor(cursor)

        feed_text = await get_text(
            FEED_URL,
            allowed_hosts=URL_HOSTS,
            params={"channel_id": channel_id},
            max_bytes=_MAX_FEED_BYTES,
        )
        records, warnings = _parse_feed(feed_text, channel_id)
        selected = records[offset:offset + limit]
        next_offset = offset + len(selected)
        has_more = next_offset < len(records)
        if has_more:
            warnings.append(
                "The cursor is an offset within this feed response; new uploads can shift entries between requests."
            )
        warnings.append(
            "The channel feed provides recent upload notifications; this result does not represent complete channel history."
        )

        return ResultPage(
            platform=self.platform,
            operation="profile_posts",
            items=selected,
            pagination=Pagination(
                cursor=str(next_offset) if has_more else None,
                has_more=has_more,
                detail=(
                    "Cursor is an offset within the current Atom feed response. "
                    "The feed route does not provide a documented full-history continuation token."
                ),
            ),
            complete=False,
            warnings=warnings,
            extensions={
                "channel_id": channel_id,
                "feed_entry_count": len(records),
                "feed_title": _feed_text(feed_text, "title"),
                "feed_updated": _feed_text(feed_text, "updated"),
            },
        )


def _validate_channel_id(value: str) -> str:
    if not isinstance(value, str) or not _CHANNEL_ID_RE.fullmatch(value.strip()):
        raise ReachError(
            "invalid_request",
            "Supply a YouTube channel ID beginning with UC and containing only letters, numbers, underscores, or hyphens.",
        )
    return value.strip()


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIMIT:
        raise ReachError("invalid_request", f"Limit must be an integer from 1 to {_MAX_LIMIT}.")
    return limit


def _validate_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 5:
        raise ReachError("invalid_cursor", "The YouTube feed cursor must be a non-negative offset of at most 10000.")
    offset = int(cursor)
    if offset > 10_000:
        raise ReachError("invalid_cursor", "The YouTube feed cursor is outside the supported range.")
    return offset


def _feed_text(xml_text: str, local_name: str) -> str | None:
    """Return a simple Atom feed-level field, if present and well-formed."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return None
    node = root.find(f"{{{_ATOM_NS}}}{local_name}")
    value = node.text.strip() if node is not None and node.text else ""
    return value or None


def _parse_feed(xml_text: str, channel_id: str) -> tuple[list[Record], list[str]]:
    if not isinstance(xml_text, str) or "<!DOCTYPE" in xml_text.upper() or "<!ENTITY" in xml_text.upper():
        raise _parser_error()
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise _parser_error() from exc
    if root.tag != f"{{{_ATOM_NS}}}feed":
        raise _parser_error()

    records: list[Record] = []
    warnings: list[str] = []
    for entry in root.findall(f"{{{_ATOM_NS}}}entry"):
        video_id = _child_text(entry, _YT_NS, "videoId")
        entry_channel_id = _child_text(entry, _YT_NS, "channelId")
        if not video_id or not _VIDEO_ID_RE.fullmatch(video_id):
            warnings.append("A feed entry without a valid video ID was skipped.")
            continue
        if entry_channel_id != channel_id:
            warnings.append("A feed entry for a different or unidentified channel was skipped.")
            continue

        title = _child_text(entry, _ATOM_NS, "title")
        author = entry.find(f"{{{_ATOM_NS}}}author")
        author_name = _child_text(author, _ATOM_NS, "name") if author is not None else None
        published = _child_text(entry, _ATOM_NS, "published")
        updated = _child_text(entry, _ATOM_NS, "updated")
        created_at = _normalize_timestamp(published)
        if published and created_at is None:
            warnings.append(f"Video {video_id} has an unreadable published timestamp.")

        records.append(
            Record(
                platform="youtube",
                type="video",
                id=video_id,
                url=f"https://www.youtube.com/watch?v={quote(video_id, safe='')}",
                author=author_name,
                title=title,
                created_at=created_at,
                access=AccessInfo(mode="anonymous", complete=False),
                extensions={
                    "channel_id": channel_id,
                    "updated_at": updated,
                    "metadata_source": "youtube_channel_atom_feed",
                },
            )
        )
    return records, warnings


def _child_text(parent: ElementTree.Element, namespace: str, local_name: str) -> str | None:
    node = parent.find(f"{{{namespace}}}{local_name}")
    value = node.text.strip() if node is not None and node.text else ""
    return value or None


def _normalize_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parser_error() -> ReachError:
    return ReachError("parser_error", "The YouTube channel feed returned malformed or unsupported Atom XML.")
