"""Read published posts from one Facebook Page authorized by its Page token."""

from __future__ import annotations

import os
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
    object_payload,
    page_cursor,
    requested_account,
    safe_https_url,
    validate_cursor,
    validate_limit,
)
from universal_reach.capabilities import get_capability
from universal_reach.errors import ReachError
from universal_reach.models import AccessInfo, MediaItem, Pagination, Record, ResultPage


_POST_FIELDS = "id,message,created_time,permalink_url,full_picture,shares,is_published,from"
_FACEBOOK_POST_HOSTS = frozenset({"facebook.com", "www.facebook.com", "m.facebook.com"})
_PAGE_DOC = "https://developers.facebook.com/docs/graph-api/reference/page/published_posts/"


class FacebookPageAdapter(Adapter):
    """Read the configured Page's published posts through an authorized Page token.

    Meta documents Page Public Content Access as a possible feature requirement for this
    edge. This adapter never uses that feature to access non-owned Pages.
    """

    def __init__(self) -> None:
        capability = replace(
            get_capability("facebook"),
            operations=("profile_posts",),
            auth_note=(
                "Reads only META_FACEBOOK_PAGE_ID with META_FACEBOOK_PAGE_ACCESS_TOKEN. "
                "The authorizing person needs MODERATE on the configured Page plus "
                "pages_read_engagement and pages_read_user_content. The feed reference lists "
                "CREATE_CONTENT, MANAGE, or MODERATE tasks, while the Page reference says "
                "permission-based Page reads require MODERATE. The endpoint may require Page "
                "Public Content Access for non-owned Pages; this adapter does not support those Pages."
            ),
            notes=(
                "Published Page posts only. The Page Published Posts reference says Page Public "
                "Content Access may be required; this adapter is owner-bound and has no non-owned "
                "Page mode, so that feature gate is not attempted. No comments or writes."
            ),
            documentation=(_PAGE_DOC,),
        )
        super().__init__(capability)

    async def profile_posts(
        self,
        username: str,
        limit: int = 20,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        del options
        page_id = configured_id("META_FACEBOOK_PAGE_ID")
        token = configured_value("META_FACEBOOK_PAGE_ACCESS_TOKEN")
        requested_account(username, page_id, os.environ.get("META_FACEBOOK_PAGE_USERNAME") or None)
        limit = validate_limit(limit, maximum=100, platform="Facebook")
        cursor = validate_cursor(cursor)
        token_page = object_payload(
            await graph_get(
                root=META_GRAPH_ROOT,
                host=META_GRAPH_HOST,
                path="me",
                token=token,
                params={"fields": "id"},
            ),
            context="Facebook Page token",
        )
        if str(token_page.get("id", "")) != page_id:
            raise ReachError(
                "account_mismatch",
                "META_FACEBOOK_PAGE_ID does not match the Page represented by META_FACEBOOK_PAGE_ACCESS_TOKEN.",
            )
        params: dict[str, Any] = {"fields": _POST_FIELDS, "limit": limit}
        if cursor is not None:
            params["after"] = cursor

        payload = await graph_get(
            root=META_GRAPH_ROOT,
            host=META_GRAPH_HOST,
            path=f"{quote(page_id, safe='')}/published_posts",
            token=token,
            params=params,
        )
        rows, paging = collection_payload(payload, context="Facebook Page post list")
        next_cursor, has_more, detail = page_cursor(paging, row_count=len(rows), limit=limit)
        warnings = [
            "Facebook limits this edge to approximately 600 ranked published posts per year; this is not a complete Page archive.",
            "Only posts readable by the configured Page token are returned. Reels and some shared or expired posts may be absent.",
        ]
        return ResultPage(
            platform="facebook",
            operation="profile_posts",
            items=[_post_record(row) for row in rows[:limit]],
            pagination=Pagination(cursor=next_cursor, has_more=has_more, detail=detail),
            complete=False,
            warnings=warnings,
            extensions={"page_id": page_id, "scope": "configured_owner_page_only"},
        )


def _post_record(row: Any) -> Record:
    post_id = row.get("id")
    if not isinstance(post_id, str) or not post_id:
        raise ReachError("parser_error", "Meta returned a Facebook post without an ID.")
    url = safe_https_url(row.get("permalink_url"), allowed_hosts=_FACEBOOK_POST_HOSTS)
    picture = safe_https_url(row.get("full_picture"))
    media = [MediaItem(kind="image", url=picture)] if picture else []
    shares = row.get("shares")
    share_count = metric_count(shares.get("count")) if isinstance(shares, dict) else None
    metrics = {"shares": share_count} if share_count is not None else {}
    author_obj = row.get("from") if isinstance(row.get("from"), dict) else {}
    author = author_obj.get("name") if isinstance(author_obj.get("name"), str) else None
    extensions = {}
    if isinstance(row.get("is_published"), bool):
        extensions["is_published"] = row["is_published"]
    return Record(
        platform="facebook",
        type="post",
        id=post_id,
        url=url,
        author=author,
        text=row.get("message") if isinstance(row.get("message"), str) else None,
        created_at=row.get("created_time") if isinstance(row.get("created_time"), str) else None,
        metrics=metrics,
        media=media,
        access=AccessInfo(mode="authenticated", complete=False),
        extensions=extensions,
    )
