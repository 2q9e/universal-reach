"""Anonymous, read-only access to Telegram's public channel web previews."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_text
from universal_reach.models import AccessInfo, AdapterStatus, Record, ResultPage, Pagination
from universal_reach.security import validate_https_url


URL_HOSTS = frozenset({"t.me"})
_USERNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{4,31}$")
_POST_ID_RE = re.compile(r"^[1-9][0-9]{0,15}$")
_POST_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{4,31})/([1-9][0-9]{0,15})$")
_SUBSCRIBER_RE = re.compile(r"\bsubscriber(?:s)?\b", re.IGNORECASE)
_PAGE_SIZE = 20
_MAX_LIMIT = 100
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})


def _clean_text(value: str) -> str:
    lines = [re.sub(r"[\t\f\v ]+", " ", line).strip() for line in value.replace("\r", "").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    cleaned: list[str] = []
    for line in lines:
        if not line and cleaned and not cleaned[-1]:
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def _username(value: str) -> str:
    if not isinstance(value, str):
        raise ReachError("invalid_input", "Telegram channel usernames must be strings.")
    username = value.strip()
    if username.startswith("@"):
        username = username[1:]
    if not _USERNAME_RE.fullmatch(username):
        raise ReachError("invalid_input", "Provide a public Telegram channel username using 5 to 32 letters, digits, or underscores.")
    return username


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_LIMIT:
        raise ReachError("invalid_input", f"limit must be between 1 and {_MAX_LIMIT}.")
    return value


def _cursor(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _POST_ID_RE.fullmatch(value):
        raise ReachError("invalid_input", "Telegram cursors must be positive public message IDs.")
    return value


def _created_at(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _PreviewParser(HTMLParser):
    """Extract only visible text and metadata from Telegram's public HTML preview."""

    def __init__(self, requested_username: str) -> None:
        super().__init__(convert_charrefs=True)
        self.requested_username = requested_username.casefold()
        self.title = ""
        self.extra = ""
        self.description = ""
        self.messages: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.message_depth = 0
        self.capture: str | None = None
        self.capture_depth = 0
        self.capture_parts: list[str] = []

    def _start_capture(self, key: str) -> None:
        self.capture = key
        self.capture_depth = 1
        self.capture_parts = []

    def _finish_capture(self) -> None:
        value = _clean_text("".join(self.capture_parts))
        if self.capture == "page_title":
            self.title = value
        elif self.capture == "page_extra":
            self.extra = " ".join(part for part in (self.extra, value) if part)
        elif self.capture == "page_description":
            self.description = value
        elif self.current is not None and self.capture == "message_text":
            self.current["text"] = value or None
        elif self.current is not None and self.capture == "message_views":
            self.current["views_text"] = value or None
        elif self.current is not None and self.capture == "message_owner":
            self.current["channel_title"] = value or None
        elif self.current is not None and self.capture == "message_signature":
            self.current["author_signature"] = value or None
        self.capture = None
        self.capture_depth = 0
        self.capture_parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        tag = tag.lower()
        is_void = tag in _VOID_TAGS

        if self.capture is not None:
            if not is_void:
                self.capture_depth += 1
            if tag == "br":
                self.capture_parts.append("\n")
        if self.current is not None and not is_void:
            self.message_depth += 1

        if self.current is None and "tgme_widget_message" in classes:
            post_key = _POST_KEY_RE.fullmatch(attributes.get("data-post") or "")
            if post_key and post_key.group(1).casefold() == self.requested_username:
                self.current = {
                    "username": post_key.group(1),
                    "id": post_key.group(2),
                    "text": None,
                    "views_text": None,
                    "channel_title": None,
                    "author_signature": None,
                    "created_at": None,
                    "has_media": False,
                }
                self.message_depth = 0 if is_void else 1
            return

        if self.current is not None:
            if tag == "time" and attributes.get("datetime"):
                self.current["created_at"] = attributes["datetime"]
            if any(token in classes for token in (
                "tgme_widget_message_photo_wrap",
                "tgme_widget_message_video_player",
                "tgme_widget_message_document",
                "tgme_widget_message_audio_player",
                "tgme_widget_message_poll",
                "tgme_widget_message_sticker",
                "tgme_widget_message_location",
            )):
                self.current["has_media"] = True
            if self.capture is None:
                if "tgme_widget_message_text" in classes or "js-message_text" in classes:
                    self._start_capture("message_text")
                elif "tgme_widget_message_views" in classes:
                    self._start_capture("message_views")
                elif "tgme_widget_message_owner_name" in classes:
                    self._start_capture("message_owner")
                elif "tgme_widget_message_author" in classes:
                    self._start_capture("message_signature")
            return

        if self.capture is None:
            if classes & {"tgme_page_title", "tgme_channel_info_header_title"}:
                self._start_capture("page_title")
            elif classes & {"tgme_page_extra", "tgme_channel_info_counter"}:
                self._start_capture("page_extra")
            elif classes & {"tgme_page_description", "tgme_channel_info_description"}:
                self._start_capture("page_description")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag not in _VOID_TAGS:
            if self.capture is not None:
                self.capture_depth -= 1
                if self.capture_depth <= 0:
                    self._finish_capture()
            if self.current is not None:
                self.message_depth -= 1
                if self.message_depth <= 0:
                    self.messages.append(self.current)
                    self.current = None
                    self.message_depth = 0

    def handle_data(self, data: str) -> None:
        if self.capture is not None:
            self.capture_parts.append(data)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)


def _parse_preview(username: str, document: str) -> _PreviewParser:
    parser = _PreviewParser(username)
    parser.feed(document)
    parser.close()
    return parser


def _ensure_public_channel(page: _PreviewParser, username: str) -> None:
    if re.search(r"\bmembers?\b", page.extra, re.IGNORECASE) and not _SUBSCRIBER_RE.search(page.extra):
        raise ReachError("unsupported_url", "Only public Telegram channels are supported; public groups are outside this adapter.")
    if not _SUBSCRIBER_RE.search(page.extra):
        raise ReachError("private_or_inaccessible", "Telegram did not expose a public channel preview for this username.", details={"username": username})
    if not page.title:
        raise ReachError("parser_error", "Telegram's public preview did not contain a channel title.")


def _preview_url(username: str) -> str:
    return f"https://t.me/s/{username}"


def _profile_record(username: str, page: _PreviewParser) -> Record:
    subscriber_text = page.extra.strip()
    match = re.search(r"([^\s]+\s+subscribers?)", subscriber_text, re.IGNORECASE)
    metrics: dict[str, int | float | str | None] = {}
    if match:
        metrics["subscribers_text"] = match.group(1)
    return Record(
        platform="telegram",
        type="channel",
        id=username,
        url=f"https://t.me/{username}",
        author=page.title,
        title=page.title,
        text=page.description or None,
        metrics=metrics,
        access=AccessInfo(mode="anonymous", complete=True),
        extensions={
            "username": username,
            "preview_url": _preview_url(username),
            "preview_only": True,
            "subscriber_summary": subscriber_text,
        },
    )


def _post_record(username: str, channel_title: str, post: dict[str, Any]) -> Record:
    post_id = post["id"]
    message_url = f"https://t.me/{post['username']}/{post_id}"
    text = post.get("text")
    preview_limited = isinstance(text, str) and text.strip().casefold() == "please open telegram to view this post"
    if preview_limited:
        text = None
    metrics: dict[str, int | float | str | None] = {}
    if post.get("views_text"):
        metrics["views_text"] = post["views_text"]
    complete = bool(text) and not bool(post.get("has_media")) and not preview_limited
    return Record(
        platform="telegram",
        type="channel_post",
        id=post_id,
        url=message_url,
        author=post.get("author_signature") or post.get("channel_title") or channel_title or username,
        text=text,
        created_at=_created_at(post.get("created_at")),
        metrics=metrics,
        access=AccessInfo(mode="anonymous", complete=complete),
        extensions={
            "channel_username": username,
            "message_id": post_id,
            "public_preview_url": _preview_url(username),
            "preview_limited": preview_limited,
            "media_present_but_not_retrieved": bool(post.get("has_media")),
        },
    )


class TelegramAdapter(Adapter):
    """Read public Telegram channel profiles and posts exposed by t.me preview pages.

    This adapter does not use MTProto, the Bot API, a login, cookies, or a search
    endpoint. Telegram's public preview remains the authority for what is visible.
    """

    url_hosts = URL_HOSTS

    def __init__(self) -> None:
        capability = adapter_capability("telegram", ("read", "profile", "profile_posts"))
        capability = replace(
            capability,
            anonymous_access="partial",
            auth_note="Anonymous reads cover only public channel content that Telegram exposes in its t.me web preview; groups, private channels, and preview-hidden messages are unsupported.",
        )
        super().__init__(capability)

    async def _page(self, username: str, before: str | None = None) -> _PreviewParser:
        params = {"before": before} if before is not None else None
        document = await get_text(
            _preview_url(username),
            allowed_hosts=URL_HOSTS,
            params=params,
            timeout=8.0,
            retries=0,
            max_bytes=1_500_000,
        )
        if not isinstance(document, str):
            raise ReachError("parser_error", "Telegram's public preview returned a non-text response.")
        return _parse_preview(username, document)

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        del options
        try:
            await self.profile("telegram")
        except ReachError as exc:
            return AdapterStatus(
                platform=self.platform,
                status="degraded" if exc.code == "rate_limited" else "unavailable",
                operations=list(self.capability.operations),
                message=f"Telegram public-preview health check failed ({exc.code}).",
                access_mode="anonymous",
            )
        return AdapterStatus(
            platform=self.platform,
            status="available",
            operations=list(self.capability.operations),
            message="Telegram's public channel preview responded.",
            access_mode="anonymous",
        )

    async def profile(self, username: str, options: dict[str, Any] | None = None) -> Record:
        del options
        name = _username(username)
        page = await self._page(name)
        _ensure_public_channel(page, name)
        return _profile_record(name, page)

    async def profile_posts(
        self,
        username: str,
        limit: int = 20,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        del options
        name = _username(username)
        limit = _limit(limit)
        before = _cursor(cursor)
        page = await self._page(name, before)
        _ensure_public_channel(page, name)

        source_posts = sorted(page.messages, key=lambda item: int(item["id"]), reverse=True)
        records: list[Record] = []
        warnings: list[str] = []
        for post in source_posts:
            record = _post_record(name, page.title, post)
            records.append(record)
        if any(not record.access.complete for record in records):
            warnings.append("Telegram's public preview omitted media or some message text; affected records are marked incomplete.")

        selected = records[:limit]
        has_more = len(records) > len(selected) or len(source_posts) >= _PAGE_SIZE
        next_cursor: str | None = None
        if has_more:
            if selected:
                next_cursor = selected[-1].id
            elif source_posts:
                next_cursor = source_posts[-1]["id"]
        return ResultPage(
            platform=self.platform,
            operation="profile_posts",
            items=selected,
            pagination=Pagination(
                cursor=next_cursor,
                has_more=has_more,
                detail="The cursor is a public message ID passed to Telegram's t.me/s preview using its before parameter.",
            ),
            complete=not has_more and not warnings,
            warnings=warnings,
            extensions={
                "channel_username": name,
                "preview_url": _preview_url(name),
                "source_preview_item_count": len(source_posts),
                "preview_page_size": _PAGE_SIZE,
            },
        )

    @staticmethod
    def _post_url(url: str) -> tuple[str, str]:
        validate_https_url(url, URL_HOSTS)
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise ReachError("invalid_url", "The Telegram post URL is malformed.") from exc
        if parsed.fragment:
            raise ReachError("invalid_url", "Telegram post URLs must not contain fragments.")
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if any(key not in {"single", "embed"} for key, _value in query):
            raise ReachError("invalid_url", "Telegram post URLs may only use the single or embed display parameter.")
        segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
        if segments and segments[0] == "s":
            segments = segments[1:]
        if len(segments) != 2:
            raise ReachError("invalid_url", "Expected a public Telegram channel post URL such as https://t.me/channel/123.")
        username, post_id = segments
        if not _USERNAME_RE.fullmatch(username) or not _POST_ID_RE.fullmatch(post_id):
            raise ReachError("invalid_url", "The Telegram URL must contain a public channel username and positive message ID.")
        return username, post_id

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        del options
        username, post_id = self._post_url(url)
        # before is exclusive, so the next integer includes the requested post.
        page = await self._page(username, str(int(post_id) + 1))
        _ensure_public_channel(page, username)
        for post in page.messages:
            if post["id"] == post_id:
                return _post_record(username, page.title, post)
        raise ReachError(
            "not_found",
            "The requested post was not present in Telegram's public channel preview.",
            details={"channel_username": username, "message_id": post_id},
        )


__all__ = ["TelegramAdapter"]
