"""Read the authorized Threads profile, its posts, and replies to its posts."""

from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from universal_reach.adapters.base import Adapter
from universal_reach.adapters.meta_graph import (
    THREADS_GRAPH_HOST,
    THREADS_GRAPH_ROOT,
    collection_payload,
    configured_id,
    configured_value,
    graph_get,
    metric_count,
    object_payload,
    page_cursor,
    safe_https_url,
    validate_cursor,
    validate_limit,
)
from universal_reach.capabilities import get_capability
from universal_reach.errors import ReachError
from universal_reach.models import AccessInfo, MediaItem, Pagination, Record, ResultPage


_PROFILE_FIELDS = "id,username,name,threads_profile_picture_url,threads_biography"
_THREAD_FIELDS = "id,media_product_type,media_type,permalink,owner,username,text,timestamp,shortcode,has_replies,is_quote_post,media_url,thumbnail_url"
_REPLY_FIELDS = "id,text,timestamp,permalink,username,is_reply,root_post,replied_to,media_type,media_url,thumbnail_url"
_THREADS_HOSTS = frozenset({"threads.com", "www.threads.com", "threads.net", "www.threads.net"})
_HANDLE = re.compile(r"^[A-Za-z0-9._]{1,50}$")
_THREAD_ID = re.compile(r"^[0-9]{1,40}$")
_THREADS_DOC = "https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api"
_THREADS_PROFILE_DOC = "https://developers.facebook.com/documentation/threads/threads-profiles"
_THREADS_REPLY_DOC = "https://developers.facebook.com/documentation/threads/reply-management"


class ThreadsAdapter(Adapter):
    """Use only the token owner's `/me` data; no public-profile discovery or writes."""

    def __init__(self) -> None:
        capability = replace(
            get_capability("threads"),
            operations=("read", "profile", "profile_posts", "replies"),
            auth_note=(
                "Requires META_THREADS_ACCESS_TOKEN from a Threads OAuth user authorization. "
                "Profile and post reads are limited to /me (threads_basic); replies require "
                "threads_read_replies and are limited by Meta to posts owned by that user. "
                "External profile discovery is not enabled."
            ),
            notes="Read-only authorized-user profile, posts, and owned-post replies. No oEmbed or tokenless retrieval.",
            documentation=(_THREADS_DOC, _THREADS_PROFILE_DOC, _THREADS_REPLY_DOC),
        )
        super().__init__(capability)

    async def profile(self, username: str, options: dict[str, Any] | None = None) -> Record:
        del options
        profile, _token = await self._owner_profile(username)
        return _profile_record(profile)

    async def profile_posts(
        self,
        username: str,
        limit: int = 20,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        del options
        limit = validate_limit(limit, maximum=50, platform="Threads")
        cursor = validate_cursor(cursor)
        profile, token = await self._owner_profile(username)
        params: dict[str, Any] = {"fields": _THREAD_FIELDS, "limit": limit}
        if cursor is not None:
            params["after"] = cursor
        payload = await graph_get(
            root=THREADS_GRAPH_ROOT,
            host=THREADS_GRAPH_HOST,
            path="me/threads",
            token=token,
            params=params,
        )
        rows, paging = collection_payload(payload, context="Threads post list")
        next_cursor, has_more, detail = page_cursor(paging, row_count=len(rows), limit=limit)
        return ResultPage(
            platform="threads",
            operation="profile_posts",
            items=[_thread_record(row) for row in rows[:limit]],
            pagination=Pagination(cursor=next_cursor, has_more=has_more, detail=detail),
            complete=not has_more,
            warnings=["The Threads API returns only posts visible to the authorized profile; this adapter does not discover or read another public profile."],
            extensions={"profile_id": profile["id"], "username": profile["username"], "scope": "authorized_user_only"},
        )

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        post_id = _thread_id_option(options)
        handle, shortcode = _parse_post_url(url)
        profile, token = await self._owner_profile(handle)
        post = await self._verified_post(post_id, profile, token, expected_shortcode=shortcode)
        return _thread_record(post)

    async def replies(
        self,
        url: str,
        limit: int = 50,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        post_id = _thread_id_option(options)
        handle, shortcode = _parse_post_url(url)
        limit = validate_limit(limit, maximum=50, platform="Threads")
        cursor = validate_cursor(cursor)
        profile, token = await self._owner_profile(handle)
        await self._verified_post(post_id, profile, token, expected_shortcode=shortcode)
        params: dict[str, Any] = {"fields": _REPLY_FIELDS, "limit": limit}
        if cursor is not None:
            params["after"] = cursor
        payload = await graph_get(
            root=THREADS_GRAPH_ROOT,
            host=THREADS_GRAPH_HOST,
            path=f"{quote(post_id, safe='')}/replies",
            token=token,
            params=params,
        )
        rows, paging = collection_payload(payload, context="Threads reply list")
        next_cursor, has_more, detail = page_cursor(paging, row_count=len(rows), limit=limit)
        return ResultPage(
            platform="threads",
            operation="replies",
            items=[_thread_record(row, is_reply=True) for row in rows[:limit]],
            pagination=Pagination(cursor=next_cursor, has_more=has_more, detail=detail),
            complete=not has_more,
            warnings=["Replies are requested only after Meta confirms the parent post belongs to the authorized profile; threads_read_replies is required."],
            extensions={"parent_thread_id": post_id, "parent_url": url, "scope": "authorized_user_owned_post_replies"},
        )

    async def _owner_profile(self, requested_username: str) -> tuple[dict[str, Any], str]:
        requested = requested_username.strip().lstrip("@") if isinstance(requested_username, str) else ""
        if requested.casefold() not in {"me", "self"} and not _HANDLE.fullmatch(requested):
            raise ReachError("invalid_request", "Provide `me` or the authorized Threads profile's exact username.")
        token = configured_value("META_THREADS_ACCESS_TOKEN")
        configured_user_id = os.environ.get("META_THREADS_USER_ID", "").strip()
        if configured_user_id and not _THREAD_ID.fullmatch(configured_user_id):
            raise ReachError("invalid_configuration", "META_THREADS_USER_ID must be numeric.")
        payload = await graph_get(
            root=THREADS_GRAPH_ROOT,
            host=THREADS_GRAPH_HOST,
            path="me",
            token=token,
            params={"fields": _PROFILE_FIELDS},
        )
        profile = dict(object_payload(payload, context="Threads profile"))
        profile_id = profile.get("id")
        owner_handle = profile.get("username")
        if not isinstance(profile_id, str) or not _THREAD_ID.fullmatch(profile_id) or not isinstance(owner_handle, str) or not _HANDLE.fullmatch(owner_handle):
            raise ReachError("parser_error", "Meta returned an incomplete Threads owner profile.")
        if configured_user_id and profile_id != configured_user_id:
            raise ReachError("account_mismatch", "The configured Threads user ID does not match the access token owner.")
        configured_handle = os.environ.get("META_THREADS_USERNAME", "").strip().lstrip("@")
        if requested.casefold() not in {"me", "self"} and requested.casefold() != owner_handle.casefold():
            raise ReachError("account_mismatch", "Threads profile and post reads are limited to the configured access token owner.")
        if configured_handle and configured_handle.casefold() != owner_handle.casefold():
            raise ReachError("account_mismatch", "META_THREADS_USERNAME does not match the access token owner.")
        return profile, token

    async def _verified_post(
        self,
        post_id: str,
        profile: dict[str, Any],
        token: str,
        *,
        expected_shortcode: str,
    ) -> dict[str, Any]:
        payload = await graph_get(
            root=THREADS_GRAPH_ROOT,
            host=THREADS_GRAPH_HOST,
            path=quote(post_id, safe=""),
            token=token,
            params={"fields": _THREAD_FIELDS},
        )
        post = dict(object_payload(payload, context="Threads post"))
        owner = post.get("owner") if isinstance(post.get("owner"), dict) else {}
        if post.get("id") != post_id or str(owner.get("id", "")) != profile["id"]:
            raise ReachError("private_or_inaccessible", "The requested Threads post is not owned by the authorized profile.")
        if post.get("shortcode") != expected_shortcode:
            raise ReachError("invalid_request", "The supplied Threads API thread_id does not match the shortcode in the post URL.")
        return post


def _thread_id_option(options: dict[str, Any] | None) -> str:
    value = options.get("thread_id") if isinstance(options, dict) else None
    if not isinstance(value, str) or not _THREAD_ID.fullmatch(value):
        raise ReachError(
            "invalid_request",
            "Supply the numeric API thread_id in options; a public Threads URL shortcode is not an API object ID.",
        )
    return value


def _parse_post_url(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ReachError("invalid_url", "The Threads post URL is malformed.") from exc
    if (
        parsed.scheme.lower() != "https"
        or host not in _THREADS_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ReachError("invalid_url", "Only clean HTTPS Threads post URLs are supported.")
    if re.search(r"%(?:2f|5c)", parsed.path, re.IGNORECASE):
        raise ReachError("invalid_url", "Encoded path separators are not allowed in Threads URLs.")
    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if len(parts) != 3 or not parts[0].startswith("@") or parts[1] != "post":
        raise ReachError("invalid_url", "A Threads post URL must use /@username/post/{shortcode}.")
    handle, shortcode = parts[0][1:], parts[2]
    if not _HANDLE.fullmatch(handle) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", shortcode):
        raise ReachError("invalid_url", "The Threads post URL contains an invalid handle or shortcode.")
    return handle, shortcode


def _profile_record(profile: dict[str, Any]) -> Record:
    username = profile["username"]
    picture = safe_https_url(profile.get("threads_profile_picture_url"))
    media = [MediaItem(kind="image", url=picture)] if picture else []
    return Record(
        platform="threads",
        type="profile",
        id=profile["id"],
        url=f"https://www.threads.com/@{quote(username, safe='._')}",
        author=f"@{username}",
        title=profile.get("name") if isinstance(profile.get("name"), str) else None,
        text=profile.get("threads_biography") if isinstance(profile.get("threads_biography"), str) else None,
        media=media,
        access=AccessInfo(mode="authenticated", complete=False),
        extensions={"username": username, "scope": "authorized_user_only"},
    )


def _thread_record(item: dict[str, Any], *, is_reply: bool = False) -> Record:
    thread_id = item.get("id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ReachError("parser_error", "Meta returned a Threads item without an ID.")
    username = item.get("username") if isinstance(item.get("username"), str) and _HANDLE.fullmatch(item["username"]) else None
    permalink = safe_https_url(item.get("permalink"), allowed_hosts=_THREADS_HOSTS)
    media_type = item.get("media_type") if isinstance(item.get("media_type"), str) else None
    media_url = safe_https_url(item.get("media_url")) or safe_https_url(item.get("thumbnail_url"))
    media: list[MediaItem] = []
    if media_url:
        kind = "video" if media_type and "VIDEO" in media_type else "image"
        media.append(MediaItem(kind=kind, url=media_url))
    extensions: dict[str, Any] = {}
    for key in ("shortcode", "media_type", "media_product_type", "has_replies", "is_quote_post", "is_reply", "root_post", "replied_to"):
        if item.get(key) is not None:
            extensions[key] = item[key]
    return Record(
        platform="threads",
        type="reply" if is_reply else "post",
        id=thread_id,
        url=permalink,
        author=f"@{username}" if username else None,
        text=item.get("text") if isinstance(item.get("text"), str) else None,
        created_at=item.get("timestamp") if isinstance(item.get("timestamp"), str) else None,
        media=media,
        access=AccessInfo(mode="authenticated", complete=False),
        extensions=extensions,
    )
