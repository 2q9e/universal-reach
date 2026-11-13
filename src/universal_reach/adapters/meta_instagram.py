"""Read an authorized Instagram professional account through Instagram Graph API."""

from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Any
from urllib.parse import quote

from universal_reach.adapters.base import Adapter
from universal_reach.adapters.meta_graph import (
    META_GRAPH_HOST,
    META_GRAPH_ROOT,
    collection_payload,
    configured_id,
    configured_value,
    graph_get,
    metric_count,
    page_cursor,
    requested_account,
    safe_https_url,
    validate_cursor,
    validate_limit,
)
from universal_reach.capabilities import get_capability
from universal_reach.errors import ReachError
from universal_reach.models import AccessInfo, MediaItem, Pagination, Record, ResultPage


_MEDIA_FIELDS = "id,caption,media_type,media_url,permalink,timestamp,username,thumbnail_url,like_count,comments_count"
_PROFILE_FIELDS = "id,username,name,biography,website,profile_picture_url,followers_count,follows_count,media_count"
_INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com"})
_INSTAGRAM_HANDLE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_IG_MEDIA_DOC = "https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media"
_IG_PROFILE_DOC = "https://developers.facebook.com/docs/graph-api/reference/ig-user/"
_IG_ACCESS_DOC = "https://developers.facebook.com/documentation/instagram-platform/overview"


class InstagramProfessionalAdapter(Adapter):
    """Read one configured professional account; never discover or enumerate other accounts."""

    def __init__(self) -> None:
        capability = replace(
            get_capability("instagram"),
            operations=("profile", "profile_posts"),
            auth_note=(
                "Reads META_INSTAGRAM_USER_ID with META_INSTAGRAM_ACCESS_TOKEN using the "
                "Facebook Login Instagram Graph API. Requires a professional account, "
                "instagram_basic, and pages_read_engagement or pages_show_list. Standard "
                "Access is limited to app-role users; serving unrelated clients requires "
                "Advanced Access, App Review, and Business Verification."
            ),
            notes="Read-only configured-account data; private personal accounts and public account discovery are unsupported.",
            documentation=(_IG_MEDIA_DOC, _IG_PROFILE_DOC, _IG_ACCESS_DOC),
        )
        super().__init__(capability)

    async def profile(self, username: str, options: dict[str, Any] | None = None) -> Record:
        del options
        account_id, token = self._configuration()
        requested_account(
            username,
            account_id,
            os.environ.get("META_INSTAGRAM_USERNAME") or None,
        )
        payload = await graph_get(
            root=META_GRAPH_ROOT,
            host=META_GRAPH_HOST,
            path=quote(account_id, safe=""),
            token=token,
            params={"fields": _PROFILE_FIELDS},
        )
        profile = payload if isinstance(payload, dict) else None
        if profile is None:
            raise ReachError("parser_error", "Meta returned malformed Instagram profile data.")
        return _profile_record(profile, fallback_id=account_id)

    async def profile_posts(
        self,
        username: str,
        limit: int = 20,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        del options
        account_id, token = self._configuration()
        requested_account(
            username,
            account_id,
            os.environ.get("META_INSTAGRAM_USERNAME") or None,
        )
        limit = validate_limit(limit, maximum=100, platform="Instagram")
        cursor = validate_cursor(cursor)
        params: dict[str, Any] = {"fields": _MEDIA_FIELDS, "limit": limit}
        if cursor is not None:
            params["after"] = cursor
        payload = await graph_get(
            root=META_GRAPH_ROOT,
            host=META_GRAPH_HOST,
            path=f"{quote(account_id, safe='')}/media",
            token=token,
            params=params,
        )
        rows, paging = collection_payload(payload, context="Instagram media list")
        next_cursor, has_more, detail = page_cursor(paging, row_count=len(rows), limit=limit)
        return ResultPage(
            platform="instagram",
            operation="profile_posts",
            items=[_media_record(row, fallback_username=os_username()) for row in rows[:limit]],
            pagination=Pagination(cursor=next_cursor, has_more=has_more, detail=detail),
            complete=False,
            warnings=[
                "Instagram returns at most its 10,000 most recently created media objects; Stories are excluded.",
                "Meta may omit video media_url for copyrighted or licensed audio and when reel downloads are disabled.",
                "This adapter reads only the configured professional account authorized by the token.",
            ],
            extensions={"account_id": account_id, "scope": "configured_authorized_professional_account_only"},
        )

    def _configuration(self) -> tuple[str, str]:
        return configured_id("META_INSTAGRAM_USER_ID"), configured_value("META_INSTAGRAM_ACCESS_TOKEN")


def os_username() -> str | None:
    value = os.environ.get("META_INSTAGRAM_USERNAME", "").strip().lstrip("@")
    return value if _INSTAGRAM_HANDLE.fullmatch(value) else None


def _profile_record(profile: dict[str, Any], *, fallback_id: str) -> Record:
    username = profile.get("username") if isinstance(profile.get("username"), str) else None
    profile_id = profile.get("id") if isinstance(profile.get("id"), str) else fallback_id
    if username and not _INSTAGRAM_HANDLE.fullmatch(username):
        username = None
    url = f"https://www.instagram.com/{quote(username, safe='._')}/" if username else None
    picture = safe_https_url(profile.get("profile_picture_url"))
    media = [MediaItem(kind="image", url=picture)] if picture else []
    metrics = {
        key: value
        for key, candidate in {
            "followers": profile.get("followers_count"),
            "following": profile.get("follows_count"),
            "media": profile.get("media_count"),
        }.items()
        if (value := metric_count(candidate)) is not None
    }
    extensions = {
        key: profile[key]
        for key in ("name", "website")
        if isinstance(profile.get(key), str)
    }
    return Record(
        platform="instagram",
        type="profile",
        id=profile_id,
        url=url,
        author=f"@{username}" if username else None,
        title=profile.get("name") if isinstance(profile.get("name"), str) else None,
        text=profile.get("biography") if isinstance(profile.get("biography"), str) else None,
        metrics=metrics,
        media=media,
        access=AccessInfo(mode="authenticated", complete=False),
        extensions=extensions,
    )


def _media_record(item: Any, *, fallback_username: str | None) -> Record:
    media_id = item.get("id")
    if not isinstance(media_id, str) or not media_id:
        raise ReachError("parser_error", "Meta returned Instagram media without an ID.")
    username = item.get("username") if isinstance(item.get("username"), str) else fallback_username
    if username and not _INSTAGRAM_HANDLE.fullmatch(username):
        username = None
    media_type = item.get("media_type") if isinstance(item.get("media_type"), str) else None
    media_url = safe_https_url(item.get("media_url")) or safe_https_url(item.get("thumbnail_url"))
    media: list[MediaItem] = []
    if media_url:
        kind = "video" if media_type in {"VIDEO", "REELS"} else ("carousel" if media_type == "CAROUSEL_ALBUM" else "image")
        media.append(MediaItem(kind=kind, url=media_url))
    metrics = {
        key: value
        for key, candidate in {
            "likes": item.get("like_count"),
            "comments": item.get("comments_count"),
        }.items()
        if (value := metric_count(candidate)) is not None
    }
    permalink = safe_https_url(item.get("permalink"), allowed_hosts=_INSTAGRAM_HOSTS)
    return Record(
        platform="instagram",
        type="post",
        id=media_id,
        url=permalink,
        author=f"@{username}" if username else None,
        text=item.get("caption") if isinstance(item.get("caption"), str) else None,
        created_at=item.get("timestamp") if isinstance(item.get("timestamp"), str) else None,
        metrics=metrics,
        media=media,
        access=AccessInfo(mode="authenticated", complete=False),
        extensions={"media_type": media_type} if media_type else {},
    )
