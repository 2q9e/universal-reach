"""Anonymous, read-only access to YouTube's public channel feed and oEmbed.

The YouTube Data API requires credentials even for public reads; this adapter
does not use an API key or account credentials. Public channel Atom feeds give
recent uploads; oEmbed provides limited metadata for an exact public video URL.
It does not search, read comments, retrieve captions, or enumerate full channel
history.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import parse_qs, quote, urlsplit
from xml.etree import ElementTree

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_json, get_text
from universal_reach.models import AccessInfo, AdapterStatus, MediaItem, Pagination, Record, ResultPage
from universal_reach.security import validate_https_url


FEED_URL = "https://www.youtube.com/xml/feeds/videos.xml"
OEMBED_URL = "https://www.youtube.com/oembed"
FEED_HOSTS = frozenset({"www.youtube.com"})
OEMBED_HOSTS = frozenset({"www.youtube.com"})
THUMBNAIL_HOSTS = frozenset({"i.ytimg.com", "img.youtube.com"})
URL_HOSTS = frozenset({
    "www.youtube.com", "youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
})
_ATOM_NS = "http://www.w3.org/2005/Atom"
_YT_NS = "http://www.youtube.com/xml/schemas/2015"
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{1,126}$")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_LIMIT = 100
_MAX_FEED_BYTES = 500_000
_HEALTH_CHANNEL_ID = "UC_x5XG1OV2P6uZZ5FSM9Ttw"
_HEALTH_VIDEO_ID = "M7lc1UVf-VE"


class YouTubeAdapter(Adapter):
    """Read recent public uploads from an explicitly supplied channel ID."""

    url_hosts = URL_HOSTS

    def __init__(self) -> None:
        super().__init__(adapter_capability("youtube", ("read", "profile_posts")))

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        del options
        feed_issue: str | None = None
        oembed_issue: str | None = None
        try:
            await self.profile_posts(_HEALTH_CHANNEL_ID, limit=1)
        except ReachError as exc:
            feed_issue = exc.code
        try:
            await self.read(_video_url(_HEALTH_VIDEO_ID))
        except ReachError as exc:
            oembed_issue = exc.code

        if feed_issue is None and oembed_issue is None:
            status: Literal["available", "degraded", "unavailable"] = "available"
            message = "The public YouTube channel feed and oEmbed endpoint responded."
        elif feed_issue is None or oembed_issue is None:
            status = "degraded"
            message = (
                "Only one YouTube source responded; "
                f"channel feed={feed_issue or 'available'}, oEmbed={oembed_issue or 'available'}."
            )
        elif "rate_limited" in {feed_issue, oembed_issue}:
            status = "degraded"
            message = (
                "Both YouTube source checks failed and at least one was rate-limited; "
                f"channel feed={feed_issue}, oEmbed={oembed_issue}."
            )
        else:
            status = "unavailable"
            message = f"YouTube live checks failed; channel feed={feed_issue}, oEmbed={oembed_issue}."
        return AdapterStatus(
            platform=self.platform,
            status=status,
            operations=list(self.capability.operations),
            message=message,
            access_mode="anonymous",
        )

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        del options
        video_id = _parse_video_id(url)
        canonical_url = _video_url(video_id)
        response = await get_json(
            OEMBED_URL,
            allowed_hosts=OEMBED_HOSTS,
            params={"url": canonical_url, "format": "json"},
        )
        if not isinstance(response, dict) or response.get("type") != "video":
            raise ReachError("parser_error", "YouTube oEmbed returned an invalid video response.")
        raw_title = response.get("title")
        raw_author = response.get("author_name")
        title = raw_title.strip() if isinstance(raw_title, str) and raw_title.strip() else None
        author = raw_author.strip() if isinstance(raw_author, str) and raw_author.strip() else None
        if title is None and author is None:
            raise ReachError("parser_error", "YouTube oEmbed returned no video title or author metadata.")

        media: list[MediaItem] = []
        thumbnail_url = response.get("thumbnail_url")
        if isinstance(thumbnail_url, str):
            try:
                validate_https_url(thumbnail_url, THUMBNAIL_HOSTS)
            except ReachError:
                pass
            else:
                metadata: dict[str, Any] = {}
                for field in ("thumbnail_width", "thumbnail_height"):
                    value = response.get(field)
                    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                        metadata[field.removeprefix("thumbnail_")] = value
                media.append(MediaItem(kind="thumbnail", url=thumbnail_url, metadata=metadata))

        return Record(
            platform=self.platform,
            type="video",
            id=video_id,
            url=canonical_url,
            author=author,
            title=title,
            media=media,
            access=AccessInfo(mode="anonymous", complete=False),
            extensions={"metadata_source": "youtube_oembed"},
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
            allowed_hosts=FEED_HOSTS,
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


def _video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={quote(video_id, safe='')}"


def _parse_video_id(url: str) -> str:
    if not isinstance(url, str) or not url:
        raise ReachError("invalid_url", "A non-empty YouTube video URL is required.")
    validate_https_url(url, URL_HOSTS)
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.rstrip("/")
    candidate: str | None = None
    if host == "youtu.be":
        segments = path.lstrip("/").split("/")
        candidate = segments[0] if len(segments) == 1 else None
    elif path == "/watch":
        candidate = parse_qs(parsed.query).get("v", [None])[0]
    else:
        segments = path.strip("/").split("/")
        if len(segments) == 2 and segments[0] in {"shorts", "embed", "live", "v"}:
            candidate = segments[1]
    if not isinstance(candidate, str) or not _VIDEO_ID_RE.fullmatch(candidate):
        raise ReachError("invalid_url", "Expected a YouTube video URL with a valid video ID.")
    return candidate


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
