"""Anonymous, read-only Bluesky AppView adapter."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_json
from universal_reach.models import AccessInfo, AdapterStatus, MediaItem, Pagination, Record, ResultPage

APPVIEW_HOST = "public.api.bsky.app"
APPVIEW_ROOT = f"https://{APPVIEW_HOST}/xrpc"
PUBLIC_URL_HOSTS = frozenset({"bsky.app", "bsky.social"})
AT_URI_COLLECTION = "app.bsky.feed.post"
_MAX_LIMIT = 100
_MAX_DEPTH = 100

_HANDLE_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
_DID = re.compile(r"^did:[a-z0-9]+:[A-Za-z0-9._:%-]{1,255}$", re.IGNORECASE)
_RKEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,254}$")


def _invalid_actor() -> ReachError:
    return ReachError(
        "invalid_actor",
        "Provide a valid Bluesky handle, DID, or bsky.app/bsky.social profile URL.",
    )


def _validate_actor(value: str) -> str:
    actor = value.strip()
    if actor.startswith("@"):
        actor = actor[1:]
    if not actor or len(actor) > 255 or any(ord(char) < 33 for char in actor):
        raise _invalid_actor()
    if _DID.fullmatch(actor):
        return actor
    if len(actor) > 253:
        raise _invalid_actor()
    labels = actor.split(".")
    if len(labels) < 2 or any(not _HANDLE_LABEL.fullmatch(label) for label in labels):
        raise _invalid_actor()
    return actor.lower()


def _decode_path_segment(segment: str) -> str:
    if re.search(r"%(?:2f|5c)", segment, re.IGNORECASE):
        raise ReachError("invalid_url", "Encoded path separators are not allowed in Bluesky URLs.")
    try:
        decoded = unquote(segment, errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReachError("invalid_url", "The Bluesky URL contains malformed path encoding.") from exc
    if not decoded or "/" in decoded or "\\" in decoded:
        raise ReachError("invalid_url", "The Bluesky URL contains an invalid path segment.")
    return decoded


def _parse_public_url(value: str, *, require_post: bool) -> tuple[str, str | None]:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise ReachError("invalid_url", "The Bluesky URL is malformed.") from exc

    if (
        parsed.scheme.lower() != "https"
        or host not in PUBLIC_URL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ReachError(
            "invalid_url",
            "Only clean HTTPS URLs on bsky.app or bsky.social are supported.",
        )

    path = parsed.path
    if not path.startswith("/") or path.startswith("//"):
        raise ReachError("invalid_url", "The Bluesky URL path is malformed.")
    if path.endswith("/"):
        path = path[:-1]
    raw_segments = path[1:].split("/")
    if any(not segment for segment in raw_segments):
        raise ReachError("invalid_url", "The Bluesky URL path is malformed.")
    segments = [_decode_path_segment(segment) for segment in raw_segments]

    if len(segments) == 2 and segments[0] == "profile":
        actor = _validate_actor(segments[1])
        if require_post:
            raise ReachError("invalid_url", "A Bluesky post URL must include /post/{record-key}.")
        return actor, None
    if len(segments) == 4 and segments[0] == "profile" and segments[2] == "post":
        actor = _validate_actor(segments[1])
        rkey = segments[3]
        if not _RKEY.fullmatch(rkey):
            raise ReachError("invalid_url", "The Bluesky post URL contains an invalid record key.")
        return actor, rkey
    raise ReachError(
        "invalid_url",
        "Supported Bluesky URLs use /profile/{handle-or-did} or /profile/{handle-or-did}/post/{record-key}.",
    )


def _parse_actor_input(value: str) -> str:
    if not isinstance(value, str):
        raise _invalid_actor()
    stripped = value.strip()
    if "://" in stripped:
        actor, rkey = _parse_public_url(stripped, require_post=False)
        if rkey is not None:
            raise ReachError("invalid_url", "A profile operation requires a Bluesky profile URL.")
        return actor
    return _validate_actor(stripped)


def _parse_post_url(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ReachError("invalid_url", "A Bluesky post URL is required.")
    actor, rkey = _parse_public_url(value.strip(), require_post=True)
    if rkey is None:
        raise ReachError("invalid_url", "A Bluesky post URL must include /post/{record-key}.")
    return actor, rkey


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIMIT:
        raise ReachError(
            "invalid_limit",
            f"Bluesky page size must be between 1 and {_MAX_LIMIT}.",
            details={"limit": limit, "maximum": _MAX_LIMIT},
        )
    return limit


def _validate_cursor(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor.strip() or len(cursor) > 4096:
        raise ReachError("invalid_cursor", "The Bluesky pagination cursor is malformed.")
    return cursor


def _validate_depth(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_DEPTH:
        raise ReachError(
            "invalid_depth",
            f"Bluesky thread depth must be between 1 and {_MAX_DEPTH}.",
            details={"depth": value, "maximum": _MAX_DEPTH},
        )
    return value


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _safe_post_url(handle: str | None, uri: str | None) -> str | None:
    if not isinstance(uri, str) or not uri.startswith("at://"):
        return None
    remainder = uri[5:].split("/")
    if len(remainder) != 3 or remainder[1] != AT_URI_COLLECTION:
        return None
    actor, rkey = remainder[0], remainder[2]
    try:
        actor = _validate_actor(actor)
    except ReachError:
        return None
    if not _RKEY.fullmatch(rkey):
        return None
    profile_actor = handle or actor
    try:
        profile_actor = _validate_actor(profile_actor)
    except ReachError:
        profile_actor = actor
    return f"https://bsky.app/profile/{quote(profile_actor, safe='.-:')}/post/{quote(rkey, safe='._~-')}"


def _media_from_embed(embed: Any) -> list[MediaItem]:
    if not isinstance(embed, Mapping):
        return []
    embed_type = str(embed.get("$type", ""))
    result: list[MediaItem] = []
    if embed_type.endswith("images#view") or isinstance(embed.get("images"), list):
        for image in embed.get("images", []):
            if not isinstance(image, Mapping):
                continue
            result.append(
                MediaItem(
                    kind="image",
                    url=image.get("fullsize") or image.get("thumb"),
                    mime_type="image/*",
                    title=image.get("alt") or None,
                    metadata={"thumb": image.get("thumb")} if image.get("thumb") else {},
                )
            )
    external = embed.get("external")
    if isinstance(external, Mapping):
        result.append(
            MediaItem(
                kind="link",
                url=external.get("uri"),
                title=external.get("title"),
                metadata={"description": external.get("description")} if external.get("description") else {},
            )
        )
    record_with_media = embed.get("media")
    if isinstance(record_with_media, Mapping):
        result.extend(_media_from_embed(record_with_media))
    if embed_type.endswith("video#view") or embed.get("playlist"):
        result.append(
            MediaItem(
                kind="video",
                url=embed.get("playlist") or embed.get("thumbnail"),
                mime_type="video/*",
                metadata={"alt": embed.get("alt")} if embed.get("alt") else {},
            )
        )
    return result


def _post_record(post_view: Mapping[str, Any], *, reason: Any = None) -> Record:
    author = post_view.get("author")
    author = author if isinstance(author, Mapping) else {}
    post_record = post_view.get("record")
    post_record = post_record if isinstance(post_record, Mapping) else {}
    handle = author.get("handle") if isinstance(author.get("handle"), str) else None
    uri = post_view.get("uri") if isinstance(post_view.get("uri"), str) else None
    metrics = {
        key: value
        for key, value in {
            "like_count": _count(post_view.get("likeCount")),
            "reply_count": _count(post_view.get("replyCount")),
            "repost_count": _count(post_view.get("repostCount")),
            "quote_count": _count(post_view.get("quoteCount")),
        }.items()
        if value is not None
    }
    extensions: dict[str, Any] = {
        "did": author.get("did"),
        "handle": handle,
        "display_name": author.get("displayName"),
        "cid": post_view.get("cid"),
        "labels": post_view.get("labels"),
        "facets": post_record.get("facets"),
    }
    if reason is not None:
        extensions["feed_reason"] = reason
    return Record(
        platform="bluesky",
        type="post",
        id=uri or str(post_view.get("cid") or ""),
        url=_safe_post_url(handle, uri),
        author=f"@{handle}" if handle else (author.get("did") if isinstance(author.get("did"), str) else None),
        text=post_record.get("text") if isinstance(post_record.get("text"), str) else None,
        created_at=post_record.get("createdAt") if isinstance(post_record.get("createdAt"), str) else None,
        metrics=metrics,
        media=_media_from_embed(post_view.get("embed") or post_record.get("embed")),
        access=AccessInfo(mode="anonymous", complete=True),
        extensions={key: value for key, value in extensions.items() if value is not None},
    )


def _thread_record(thread_view: Mapping[str, Any], *, nested_depth: int) -> Record | None:
    post_view = thread_view.get("post")
    if not isinstance(post_view, Mapping):
        return None
    record = _post_record(post_view)
    if nested_depth > 0:
        children = thread_view.get("replies")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, Mapping):
                    reply = _thread_record(child, nested_depth=nested_depth - 1)
                    if reply is not None:
                        record.replies.append(reply)
    return record


def _thread_root(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    thread = payload.get("thread")
    if not isinstance(thread, Mapping):
        raise ReachError("parser_error", "Bluesky returned a thread response without a thread object.")
    thread_type = str(thread.get("$type", ""))
    if "notFoundPost" in thread_type or thread.get("notFound") is True:
        raise ReachError("not_found", "The requested public Bluesky post was not found.")
    if "blockedPost" in thread_type:
        raise ReachError(
            "public_access_denied",
            "The Bluesky post is blocked or inaccessible in the public AppView.",
            details={"http_status": 403},
        )
    if not isinstance(thread.get("post"), Mapping):
        raise ReachError("parser_error", "Bluesky returned a thread without a post view.")
    return thread


class BlueskyAdapter(Adapter):
    """Read public Bluesky posts and profiles from the unauthenticated AppView."""

    url_hosts = PUBLIC_URL_HOSTS
    operations = ("search", "read", "replies", "profile", "profile_posts")

    def __init__(self) -> None:
        super().__init__(adapter_capability("bluesky", self.operations))

    async def _get(self, endpoint: str, params: dict[str, Any]) -> Mapping[str, Any]:
        try:
            payload = await get_json(
                f"{APPVIEW_ROOT}/{endpoint}",
                allowed_hosts=frozenset({APPVIEW_HOST}),
                params=params,
            )
        except ReachError as exc:
            status = exc.details.get("http_status")
            if status in (401, 403):
                raise ReachError(
                    "public_access_denied",
                    "Bluesky denied this unauthenticated public AppView request.",
                    details={"http_status": status, "endpoint": endpoint},
                ) from exc
            if exc.code == "rate_limited" or status == 429:
                raise ReachError(
                    "rate_limited",
                    "Bluesky rate-limited the public AppView request; retry later.",
                    retryable=True,
                    details={"http_status": 429, "endpoint": endpoint},
                ) from exc
            raise
        if not isinstance(payload, Mapping):
            raise ReachError("parser_error", "Bluesky returned a non-object JSON response.")
        upstream_error = payload.get("error")
        if isinstance(upstream_error, str):
            if upstream_error.casefold() in {"notfound", "not_found"}:
                raise ReachError("not_found", "The requested public Bluesky content was not found.")
            raise ReachError(
                "upstream_error",
                "Bluesky returned an API error for the public request.",
                details={"upstream_error": upstream_error, "endpoint": endpoint},
            )
        return payload

    async def _profile_view(self, actor: str) -> Mapping[str, Any]:
        payload = await self._get("app.bsky.actor.getProfile", {"actor": actor})
        if not isinstance(payload.get("did"), str) or not isinstance(payload.get("handle"), str):
            raise ReachError("parser_error", "Bluesky returned an incomplete profile response.")
        return payload

    @staticmethod
    def _profile_record(profile: Mapping[str, Any]) -> Record:
        did = profile["did"]
        handle = profile["handle"]
        profile_url = f"https://bsky.app/profile/{quote(handle, safe='.-:')}"
        metrics = {
            key: value
            for key, value in {
                "followers_count": _count(profile.get("followersCount")),
                "follows_count": _count(profile.get("followsCount")),
                "posts_count": _count(profile.get("postsCount")),
            }.items()
            if value is not None
        }
        extensions = {
            key: value
            for key, value in {
                "handle": handle,
                "display_name": profile.get("displayName"),
                "avatar": profile.get("avatar"),
                "banner": profile.get("banner"),
                "labels": profile.get("labels"),
                "description": profile.get("description"),
            }.items()
            if value is not None
        }
        return Record(
            platform="bluesky",
            type="profile",
            id=did,
            url=profile_url,
            author=f"@{handle}",
            title=profile.get("displayName") if isinstance(profile.get("displayName"), str) else handle,
            text=profile.get("description") if isinstance(profile.get("description"), str) else None,
            created_at=profile.get("createdAt") if isinstance(profile.get("createdAt"), str) else None,
            metrics=metrics,
            access=AccessInfo(mode="anonymous", complete=True),
            extensions=extensions,
        )

    @staticmethod
    def _page(
        operation: str,
        posts: list[Any],
        payload: Mapping[str, Any],
        limit: int,
    ) -> ResultPage:
        items: list[Record] = []
        warnings: list[str] = []
        for item in posts:
            if not isinstance(item, Mapping):
                warnings.append("Bluesky returned a malformed item; it was skipped.")
                continue
            view = item.get("post") if isinstance(item.get("post"), Mapping) else item
            if not isinstance(view, Mapping):
                warnings.append("Bluesky returned a malformed post; it was skipped.")
                continue
            items.append(_post_record(view, reason=item.get("reason") if view is not item else None))

        cursor = payload.get("cursor")
        if isinstance(cursor, str) and cursor:
            pagination = Pagination(
                cursor=cursor,
                has_more=True,
                detail="More results are available; pass this cursor to fetch the next page.",
            )
            warnings.append("This result page has more available; continue with the returned cursor.")
            complete = False
        elif len(posts) >= limit:
            pagination = Pagination(
                detail="The response filled the requested limit but supplied no continuation cursor; more results are unknown.",
            )
            warnings.append("Pagination may be partial because Bluesky returned a full page without a cursor.")
            complete = False
        else:
            pagination = Pagination()
            complete = True
        return ResultPage(
            platform="bluesky",
            operation=operation,
            items=items,
            pagination=pagination,
            complete=complete,
            warnings=warnings,
        )

    async def search(
        self,
        query: str,
        limit: int = 10,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        limit = _validate_limit(limit)
        cursor = _validate_cursor(cursor)
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ReachError("invalid_query", "Enter a non-empty Bluesky search query (up to 1000 characters).")
        params: dict[str, Any] = {"q": query.strip(), "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if options:
            for option in ("sort", "since", "until", "author", "lang", "domain", "tag"):
                value = options.get(option)
                if value is not None:
                    if not isinstance(value, str) or len(value) > 255:
                        raise ReachError("invalid_option", f"Bluesky search option {option} must be a short string.")
                    params[option] = value
        payload = await self._get("app.bsky.feed.searchPosts", params)
        posts = payload.get("posts")
        if not isinstance(posts, list):
            raise ReachError("parser_error", "Bluesky search response did not include a posts list.")
        return self._page("search", posts, payload, limit)

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        actor, rkey = _parse_post_url(url)
        profile = await self._profile_view(actor)
        uri = f"at://{profile['did']}/{AT_URI_COLLECTION}/{rkey}"
        depth = _validate_depth((options or {}).get("depth"), default=1)
        payload = await self._get(
            "app.bsky.feed.getPostThread",
            {"uri": uri, "depth": depth, "parentHeight": 0},
        )
        thread = _thread_root(payload)
        record = _thread_record(thread, nested_depth=depth)
        if record is None:
            raise ReachError("parser_error", "Bluesky returned a thread without a readable post.")
        return record

    async def replies(
        self,
        url: str,
        limit: int = 50,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        limit = _validate_limit(limit)
        if cursor is not None:
            raise ReachError(
                "unsupported_pagination",
                "Bluesky getPostThread does not provide a reply cursor; request a larger page/depth or follow reply URLs.",
            )
        actor, rkey = _parse_post_url(url)
        profile = await self._profile_view(actor)
        uri = f"at://{profile['did']}/{AT_URI_COLLECTION}/{rkey}"
        depth = _validate_depth((options or {}).get("depth"), default=6)
        payload = await self._get(
            "app.bsky.feed.getPostThread",
            {"uri": uri, "depth": depth, "parentHeight": 0},
        )
        root = _thread_root(payload)
        raw_replies = root.get("replies")
        if not isinstance(raw_replies, list):
            raw_replies = []
        warnings: list[str] = []
        replies: list[Record] = []
        for child in raw_replies:
            if not isinstance(child, Mapping):
                warnings.append("Bluesky returned a malformed reply; it was skipped.")
                continue
            reply = _thread_record(child, nested_depth=max(0, depth - 1))
            if reply is not None:
                replies.append(reply)

        root_post = root.get("post")
        root_post = root_post if isinstance(root_post, Mapping) else {}
        reply_count = _count(root_post.get("replyCount"))
        truncated_by_limit = len(replies) > limit
        items = replies[:limit]
        if truncated_by_limit:
            warnings.append("The local limit truncated replies; getPostThread has no cursor for resuming.")
        count_mismatch = reply_count is None or reply_count > len(replies)
        if count_mismatch:
            warnings.append(
                "The returned thread may be incomplete: the public thread response has no reply cursor and its reply count is unknown or exceeds the fetched replies."
            )
        detail = "getPostThread has no continuation cursor; reply coverage is bounded by the returned thread and requested depth."
        known_more = truncated_by_limit or (reply_count is not None and reply_count > len(replies))
        pagination = Pagination(cursor=None, has_more=known_more, detail=detail)
        return ResultPage(
            platform="bluesky",
            operation="replies",
            items=items,
            pagination=pagination,
            complete=not (truncated_by_limit or count_mismatch or bool(warnings)),
            warnings=warnings,
        )

    async def profile(self, username: str, options: dict[str, Any] | None = None) -> Record:
        actor = _parse_actor_input(username)
        profile = await self._profile_view(actor)
        return self._profile_record(profile)

    async def profile_posts(
        self,
        username: str,
        limit: int = 20,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        limit = _validate_limit(limit)
        cursor = _validate_cursor(cursor)
        actor = _parse_actor_input(username)
        profile = await self._profile_view(actor)
        post_filter = (options or {}).get("filter", "posts_no_replies")
        allowed_filters = {
            "posts_with_replies",
            "posts_no_replies",
            "posts_with_media",
            "posts_and_author_threads",
            "posts_with_video",
        }
        if not isinstance(post_filter, str) or post_filter not in allowed_filters:
            raise ReachError("invalid_option", "The Bluesky author-feed filter is not supported.")
        params: dict[str, Any] = {
            "actor": profile["did"],
            "filter": post_filter,
            "limit": limit,
        }
        if cursor is not None:
            params["cursor"] = cursor
        payload = await self._get("app.bsky.feed.getAuthorFeed", params)
        feed = payload.get("feed")
        if not isinstance(feed, list):
            raise ReachError("parser_error", "Bluesky author-feed response did not include a feed list.")
        return self._page("profile_posts", feed, payload, limit)

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        del options
        try:
            payload = await get_json(
                f"{APPVIEW_ROOT}/app.bsky.feed.searchPosts",
                allowed_hosts=frozenset({APPVIEW_HOST}),
                params={"q": "universal-reach-health-check", "limit": 1},
                timeout=5.0,
                retries=0,
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("posts"), list):
                raise ReachError("parser_error", "Bluesky returned an invalid public-search response.")
            return AdapterStatus(
                platform=self.platform,
                status="available",
                message="Bluesky public AppView responded to a live check.",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )
        except ReachError as exc:
            return AdapterStatus(
                platform=self.platform,
                status="degraded" if exc.code == "rate_limited" else "unavailable",
                message=f"Bluesky health check failed ({exc.code}).",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )
