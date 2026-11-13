"""OAuth-only, read-only Reddit Data API adapter.

The current Reddit Responsible Builder Policy requires explicit approval before
an app accesses Reddit data. This adapter makes no request until the caller has
set REDDIT_API_APPROVED=true and configured an approved bearer token, app ID,
and contact username outside the chat/session.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_json
from universal_reach.models import AccessInfo, AdapterStatus, Pagination, Record, ResultPage

API_HOST = "oauth.reddit.com"
API_ROOT = f"https://{API_HOST}"
POST_URL_HOSTS = frozenset({"reddit.com", "www.reddit.com", "old.reddit.com", "redd.it"})
_MAX_LIMIT = 100
_MAX_DEPTH = 10
_USERNAME = re.compile(r"^[A-Za-z0-9_-]{3,20}$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_POST_ID = re.compile(r"^[0-9a-z]{1,20}$", re.IGNORECASE)
_COMMENT_ID = re.compile(r"^[0-9a-z]{1,20}$", re.IGNORECASE)
_FULLNAME = re.compile(r"^t[1-6]_[0-9a-z]+$", re.IGNORECASE)
_SUBREDDIT = re.compile(r"^[A-Za-z0-9_]{2,21}$")


def _configuration() -> tuple[str, str]:
    """Read approved access credentials from the process environment only."""
    approved = os.environ.get("REDDIT_API_APPROVED", "").strip().casefold()
    if approved not in {"1", "true", "yes"}:
        raise ReachError(
            "reddit_approval_required",
            "Reddit requires explicit Data API approval before access. Set REDDIT_API_APPROVED=true only after Reddit has approved this app and use case; no request was sent.",
        )

    token = os.environ.get("REDDIT_ACCESS_TOKEN", "").strip()
    if not token or len(token) > 4096 or any(char.isspace() for char in token):
        raise ReachError(
            "reddit_auth_required",
            "Set REDDIT_ACCESS_TOKEN to a valid OAuth access token for the approved Reddit app; no request was sent.",
        )

    client_id = os.environ.get("REDDIT_CLIENT_ID", "").strip()
    username = os.environ.get("REDDIT_USERNAME", "").strip()
    if not _CLIENT_ID.fullmatch(client_id) or not _USERNAME.fullmatch(username):
        raise ReachError(
            "reddit_configuration_required",
            "Set REDDIT_CLIENT_ID and REDDIT_USERNAME so Reddit receives the registered app identity and a truthful contact User-Agent; no request was sent.",
        )

    # Reddit asks for a descriptive, truthful User-Agent with platform, app ID,
    # version, and developer contact. Never include the bearer token here.
    user_agent = f"web:{client_id}:0.1.0 (by /u/{username})"
    return token, user_agent


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIMIT:
        raise ReachError(
            "invalid_limit",
            f"Reddit page size must be between 1 and {_MAX_LIMIT}.",
            details={"limit": limit, "maximum": _MAX_LIMIT},
        )
    return limit


def _validate_depth(depth: Any, *, default: int = 5, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if depth is None:
        return default
    if isinstance(depth, bool) or not isinstance(depth, int) or not minimum <= depth <= _MAX_DEPTH:
        raise ReachError(
            "invalid_depth",
            f"Reddit comment depth must be between {minimum} and {_MAX_DEPTH}.",
            details={"depth": depth, "maximum": _MAX_DEPTH},
        )
    return depth


def _validate_cursor(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or len(cursor) > 128 or not _FULLNAME.fullmatch(cursor):
        raise ReachError("invalid_cursor", "The Reddit listing cursor must be a valid item fullname.")
    return cursor


def _validate_username(username: str) -> str:
    if not isinstance(username, str):
        raise ReachError("invalid_username", "A valid Reddit username is required.")
    username = username.strip()
    if username.startswith("u/"):
        username = username[2:]
    if not _USERNAME.fullmatch(username):
        raise ReachError("invalid_username", "The Reddit username is malformed.")
    return username


def _validate_subreddit(subreddit: str) -> str:
    if not isinstance(subreddit, str):
        raise ReachError("invalid_subreddit", "A valid subreddit name is required.")
    subreddit = subreddit.strip()
    if subreddit.startswith("r/"):
        subreddit = subreddit[2:]
    if not _SUBREDDIT.fullmatch(subreddit):
        raise ReachError("invalid_subreddit", "The subreddit name is malformed.")
    return subreddit


def _decode_segment(segment: str) -> str:
    if re.search(r"%(?:2f|5c)", segment, re.IGNORECASE):
        raise ReachError("invalid_url", "Encoded path separators are not allowed in Reddit URLs.")
    try:
        value = unquote(segment, errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReachError("invalid_url", "The Reddit URL contains malformed path encoding.") from exc
    if not value or "/" in value or "\\" in value:
        raise ReachError("invalid_url", "The Reddit URL contains an invalid path segment.")
    return value


def _parse_post_id(url: str) -> tuple[str, str | None]:
    """Parse Reddit post links without resolving redirects or fetching web pages."""
    if not isinstance(url, str):
        raise ReachError("invalid_url", "A Reddit post URL is required.")
    try:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise ReachError("invalid_url", "The Reddit URL is malformed.") from exc

    if (
        parsed.scheme.lower() != "https"
        or host not in POST_URL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ReachError(
            "invalid_url",
            "Only clean HTTPS Reddit post URLs on reddit.com or redd.it are supported.",
        )

    raw_segments = parsed.path.strip("/").split("/")
    if not raw_segments or raw_segments == [""]:
        raise ReachError("invalid_url", "The Reddit URL does not contain a post path.")
    segments = [_decode_segment(part) for part in raw_segments]

    subreddit: str | None = None
    if host == "redd.it":
        if len(segments) != 1:
            raise ReachError("invalid_url", "A redd.it URL must contain only the post ID.")
        post_id = segments[0]
    elif len(segments) >= 2 and segments[0] == "comments":
        post_id = segments[1]
    elif len(segments) >= 4 and segments[0] == "r" and segments[2] == "comments":
        subreddit = _validate_subreddit(segments[1])
        post_id = segments[3]
    else:
        raise ReachError(
            "invalid_url",
            "Supported Reddit post URLs use /comments/{post-id} or /r/{subreddit}/comments/{post-id}.",
        )

    if not _POST_ID.fullmatch(post_id):
        raise ReachError("invalid_url", "The Reddit URL contains an invalid post ID.")
    return post_id.lower(), subreddit


def _created_at(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _canonical_permalink(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return f"https://www.reddit.com{value}"
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if parsed.scheme.lower() == "https" and host in POST_URL_HOSTS and parsed.username is None and parsed.password is None:
        return f"https://www.reddit.com{parsed.path}"
    return None


def _record_from_data(data: Mapping[str, Any], kind: str) -> Record | None:
    if kind not in {"t1", "t3"}:
        return None
    raw_id = data.get("id")
    if not isinstance(raw_id, str) or not _POST_ID.fullmatch(raw_id):
        return None

    is_post = kind == "t3"
    fullname = data.get("name")
    if not isinstance(fullname, str) or not _FULLNAME.fullmatch(fullname):
        fullname = f"{kind}_{raw_id}"
    author_name = data.get("author") if isinstance(data.get("author"), str) else None
    author = f"u/{author_name}" if author_name and author_name != "[deleted]" else None
    title = data.get("title") if is_post and isinstance(data.get("title"), str) else None
    body = data.get("selftext") if is_post else data.get("body")
    if not isinstance(body, str) or body in {"[deleted]", "[removed]"}:
        body = None

    metrics: dict[str, int | float | str | None] = {}
    for api_key, model_key in (
        ("score", "score"),
        ("num_comments", "reply_count"),
        ("upvote_ratio", "upvote_ratio"),
        ("controversiality", "controversiality"),
    ):
        value = data.get(api_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metrics[model_key] = value

    subreddit = data.get("subreddit") if isinstance(data.get("subreddit"), str) else None
    extension_keys = (
        "subreddit_id",
        "author_fullname",
        "is_self",
        "over_18",
        "link_flair_text",
        "edited",
        "stickied",
        "locked",
        "distinguished",
        "removed_by_category",
    )
    extensions = {key: data[key] for key in extension_keys if key in data and data[key] is not None}
    if subreddit is not None:
        extensions["subreddit"] = subreddit
    target_url = data.get("url_overridden_by_dest") or data.get("url")
    if isinstance(target_url, str):
        extensions["target_url"] = target_url

    return Record(
        platform="reddit",
        type="post" if is_post else "comment",
        id=fullname,
        url=_canonical_permalink(data.get("permalink")),
        author=author,
        title=title,
        text=body,
        created_at=_created_at(data.get("created_utc")),
        metrics=metrics,
        access=AccessInfo(mode="authenticated", complete=True),
        extensions=extensions,
    )


def _listing(payload: Any) -> tuple[list[Any], str | None]:
    if not isinstance(payload, Mapping):
        raise ReachError("parser_error", "Reddit returned a non-object listing response.")
    data = payload.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("children"), list):
        raise ReachError("parser_error", "Reddit response did not contain a listing.")
    after = data.get("after")
    if after is not None and (not isinstance(after, str) or not _FULLNAME.fullmatch(after)):
        raise ReachError("parser_error", "Reddit returned a malformed listing cursor.")
    return data["children"], after


def _page_from_listing(
    operation: str,
    payload: Any,
    limit: int,
) -> ResultPage:
    children, after = _listing(payload)
    items: list[Record] = []
    warnings: list[str] = []
    for child in children:
        if not isinstance(child, Mapping):
            warnings.append("Reddit returned a malformed listing item; it was skipped.")
            continue
        data = child.get("data")
        kind = child.get("kind")
        if not isinstance(data, Mapping) or not isinstance(kind, str):
            warnings.append("Reddit returned a listing item without a supported type; it was skipped.")
            continue
        record = _record_from_data(data, kind)
        if record is None:
            warnings.append("Reddit returned an unsupported or malformed listing item; it was skipped.")
            continue
        items.append(record)

    if after:
        pagination = Pagination(
            cursor=after,
            has_more=True,
            detail="Reddit returned an after cursor; pass it to retrieve the next listing page.",
        )
        complete = False
    elif len(children) >= limit:
        pagination = Pagination(
            detail="Reddit filled the requested limit without an after cursor; additional results cannot be confirmed.",
        )
        warnings.append("Pagination may be partial because no continuation cursor was returned.")
        complete = False
    else:
        pagination = Pagination()
        complete = True

    return ResultPage(
        platform="reddit",
        operation=operation,
        items=items,
        pagination=pagination,
        complete=complete and not warnings,
        warnings=warnings,
    )


def _comment_nodes(payload: Any) -> tuple[Mapping[str, Any], list[Any]]:
    if not isinstance(payload, list) or len(payload) < 2:
        raise ReachError("parser_error", "Reddit returned an invalid post comment-tree response.")
    post_listing = payload[0]
    comments_listing = payload[1]
    if not isinstance(post_listing, Mapping) or not isinstance(comments_listing, Mapping):
        raise ReachError("parser_error", "Reddit returned a malformed post comment tree.")
    posts, _ = _listing(post_listing)
    if not posts or not isinstance(posts[0], Mapping) or not isinstance(posts[0].get("data"), Mapping):
        raise ReachError("not_found", "The Reddit post was not found in the approved Data API.")
    post_data = posts[0]["data"]
    if not isinstance(post_data, Mapping):
        raise ReachError("parser_error", "Reddit returned a malformed post.")
    comments, _ = _listing(comments_listing)
    return post_data, comments


def _comments_from_nodes(
    nodes: list[Any],
    *,
    depth: int,
    partial_state: list[bool],
) -> list[Record]:
    records: list[Record] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            partial_state[0] = True
            continue
        kind = node.get("kind")
        if kind == "more":
            partial_state[0] = True
            continue
        data = node.get("data")
        if kind != "t1" or not isinstance(data, Mapping):
            partial_state[0] = True
            continue
        record = _record_from_data(data, "t1")
        if record is None:
            partial_state[0] = True
            continue
        replies = data.get("replies")
        if depth > 0 and isinstance(replies, Mapping):
            listing_data = replies.get("data")
            children = listing_data.get("children") if isinstance(listing_data, Mapping) else None
            if isinstance(children, list):
                record.replies = _comments_from_nodes(
                    children,
                    depth=depth - 1,
                    partial_state=partial_state,
                )
        elif depth == 0 and isinstance(replies, Mapping):
            listing_data = replies.get("data")
            children = listing_data.get("children") if isinstance(listing_data, Mapping) else None
            if isinstance(children, list) and children:
                partial_state[0] = True
        records.append(record)
    return records


class RedditAdapter(Adapter):
    """Authenticated, read-only access to Reddit's approved Data API.

    Configure REDDIT_API_APPROVED=true only after Reddit has explicitly approved
    the app and use case. Supply the issued OAuth token through
    REDDIT_ACCESS_TOKEN, with REDDIT_CLIENT_ID and REDDIT_USERNAME for the
    truthful Reddit-required User-Agent. Credentials are never accepted through
    operation arguments and are not included in logs or errors.
    """

    url_hosts = POST_URL_HOSTS
    operations = ("search", "read", "replies", "profile", "profile_posts")

    def __init__(self) -> None:
        super().__init__(adapter_capability("reddit", self.operations))

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        try:
            _configuration()
        except ReachError as exc:
            return AdapterStatus(
                platform="reddit",
                status="unavailable",
                operations=list(self.capability.operations),
                message=exc.message,
                access_mode="unknown",
            )
        return AdapterStatus(
            platform="reddit",
            status="degraded",
            operations=list(self.capability.operations),
            message="Approved OAuth configuration is present; no live Reddit request was made.",
            access_mode="authenticated",
        )

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        token, user_agent = _configuration()
        try:
            payload = await get_json(
                f"{API_ROOT}{path}",
                allowed_hosts=frozenset({API_HOST}),
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": user_agent,
                },
            )
        except ReachError as exc:
            status = exc.details.get("http_status")
            if status == 401:
                raise ReachError(
                    "reddit_auth_failed",
                    "Reddit rejected the configured OAuth token; replace it with a valid token from the approved app.",
                    details={"http_status": 401, "endpoint": path},
                ) from exc
            if status == 403:
                raise ReachError(
                    "reddit_access_denied",
                    "Reddit denied the Data API request; verify app approval, OAuth scope, and content access.",
                    details={"http_status": 403, "endpoint": path},
                ) from exc
            if exc.code == "rate_limited" or status == 429:
                raise ReachError(
                    "rate_limited",
                    "Reddit rate-limited the Data API request; retry after the reset window.",
                    retryable=True,
                    details={"http_status": 429, "endpoint": path},
                ) from exc
            raise

        if isinstance(payload, Mapping) and payload.get("error") is not None:
            error = payload.get("error")
            error_name = str(error).casefold()
            if error_name in {"429", "ratelimit", "rate_limited"}:
                raise ReachError(
                    "rate_limited",
                    "Reddit rate-limited the Data API request; retry after the reset window.",
                    retryable=True,
                    details={"endpoint": path},
                )
            if error_name in {"401", "invalid_token", "invalid_grant"}:
                raise ReachError(
                    "reddit_auth_failed",
                    "Reddit rejected the configured OAuth token; replace it with a valid token from the approved app.",
                    details={"endpoint": path},
                )
            if error_name in {"403", "forbidden", "unauthorized"}:
                raise ReachError(
                    "reddit_access_denied",
                    "Reddit denied the Data API request; verify app approval, OAuth scope, and content access.",
                    details={"endpoint": path},
                )
            raise ReachError(
                "reddit_api_error",
                "Reddit returned an API error for the read request.",
                details={"endpoint": path, "upstream_code": str(error)[:80]},
            )
        return payload

    async def search(
        self,
        query: str,
        limit: int = 10,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        limit = _validate_limit(limit)
        cursor = _validate_cursor(cursor)
        if not isinstance(query, str) or not query.strip() or len(query) > 512:
            raise ReachError("invalid_query", "Enter a non-empty Reddit search query up to 512 characters.")
        params: dict[str, Any] = {
            "q": query.strip(),
            "limit": limit,
            "sort": "relevance",
            "type": "link",
        }
        if cursor is not None:
            params["after"] = cursor
        if options is None:
            options = {}
        elif not isinstance(options, Mapping):
            raise ReachError("invalid_option", "Reddit search options must be an object.")
        subreddit = options.get("subreddit")
        if subreddit is not None:
            subreddit = _validate_subreddit(subreddit)
            path = f"/r/{quote(subreddit, safe='')}/search"
            params["restrict_sr"] = True
        else:
            path = "/search"
        sort = options.get("sort")
        if sort is not None:
            if not isinstance(sort, str) or sort not in {"relevance", "hot", "top", "new", "comments"}:
                raise ReachError("invalid_option", "Reddit search sort must be relevance, hot, top, new, or comments.")
            params["sort"] = sort
        time_filter = options.get("time")
        if time_filter is not None:
            if not isinstance(time_filter, str) or time_filter not in {"hour", "day", "week", "month", "year", "all"}:
                raise ReachError("invalid_option", "Reddit search time must be hour, day, week, month, year, or all.")
            params["t"] = time_filter
        payload = await self._get(path, params)
        return _page_from_listing("search", payload, limit)

    async def _comments(self, post_id: str, *, limit: int, depth: int, sort: str | None = None) -> tuple[Record, list[Record], bool]:
        params: dict[str, Any] = {
            "limit": limit,
            "depth": depth,
            "showmore": True,
            "raw_json": 1,
        }
        if sort is not None:
            if not isinstance(sort, str) or sort not in {"confidence", "top", "new", "controversial", "old", "random", "qa", "live"}:
                raise ReachError("invalid_option", "The Reddit comment sort is unsupported.")
            params["sort"] = sort
        payload = await self._get(f"/comments/{quote(post_id, safe='')}", params)
        post_data, nodes = _comment_nodes(payload)
        post_record = _record_from_data(post_data, "t3")
        if post_record is None:
            raise ReachError("parser_error", "Reddit returned a malformed post record.")
        partial = [False]
        comments = _comments_from_nodes(nodes, depth=depth, partial_state=partial)
        post_record.replies = comments
        return post_record, comments, partial[0]

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        post_id, _subreddit = _parse_post_id(url)
        if options is None:
            options = {}
        elif not isinstance(options, Mapping):
            raise ReachError("invalid_option", "Reddit read options must be an object.")
        limit = _validate_limit(options.get("limit", 100))
        depth = _validate_depth(options.get("depth"), default=5, allow_zero=True)
        record, _comments, _partial = await self._comments(
            post_id,
            limit=limit,
            depth=depth,
            sort=options.get("sort"),
        )
        if _partial:
            record.access.complete = False
            record.extensions["replies_partial"] = True
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
                "Reddit comment trees do not expose a listing cursor; increase the limit/depth or use a supported comment-specific request.",
            )
        post_id, _subreddit = _parse_post_id(url)
        if options is None:
            options = {}
        elif not isinstance(options, Mapping):
            raise ReachError("invalid_option", "Reddit replies options must be an object.")
        depth = _validate_depth(options.get("depth"), default=5, allow_zero=True)
        post, comments, partial = await self._comments(
            post_id,
            limit=limit,
            depth=depth,
            sort=options.get("sort"),
        )
        truncated = len(comments) > limit
        items = comments[:limit]
        warnings: list[str] = []
        if partial:
            warnings.append("Reddit returned MoreComments or incomplete nested replies; the comment tree is partial.")
        if truncated:
            warnings.append("The local limit truncated the comment tree; Reddit provides no cursor for resuming it.")
        pagination = Pagination(
            cursor=None,
            has_more=partial or truncated,
            detail="Reddit's comments endpoint returns a bounded tree without a continuation cursor.",
        )
        return ResultPage(
            platform="reddit",
            operation="replies",
            items=items,
            pagination=pagination,
            complete=not (partial or truncated),
            warnings=warnings,
            extensions={"post_id": post.id},
        )

    async def profile(self, username: str, options: dict[str, Any] | None = None) -> Record:
        username = _validate_username(username)
        payload = await self._get(f"/user/{quote(username, safe='')}/about", {"raw_json": 1})
        if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), Mapping):
            raise ReachError("parser_error", "Reddit returned a malformed user profile response.")
        data = payload["data"]
        fullname = data.get("name") if isinstance(data.get("name"), str) else username
        account_id = data.get("id") if isinstance(data.get("id"), str) and _POST_ID.fullmatch(data["id"]) else fullname
        metrics = {
            key: value
            for key, value in {
                "link_karma": data.get("link_karma"),
                "comment_karma": data.get("comment_karma"),
                "total_karma": data.get("total_karma"),
            }.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        if "total_karma" not in metrics and "link_karma" in metrics and "comment_karma" in metrics:
            metrics["total_karma"] = metrics["link_karma"] + metrics["comment_karma"]
        extensions = {
            key: data[key]
            for key in (
                "subreddit",
                "icon_img",
                "verified",
                "is_gold",
                "is_mod",
                "created_utc",
                "has_verified_email",
            )
            if key in data and data[key] is not None
        }
        profile_description = data.get("subreddit")
        description = None
        if isinstance(profile_description, Mapping):
            description = profile_description.get("public_description")
        return Record(
            platform="reddit",
            type="profile",
            id=f"t2_{account_id}",
            url=f"https://www.reddit.com/user/{quote(fullname, safe='')}/",
            author=f"u/{fullname}",
            title=fullname,
            text=description if isinstance(description, str) else None,
            created_at=_created_at(data.get("created_utc")),
            metrics=metrics,
            access=AccessInfo(mode="authenticated", complete=True),
            extensions=extensions,
        )

    async def profile_posts(
        self,
        username: str,
        limit: int = 20,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        username = _validate_username(username)
        limit = _validate_limit(limit)
        cursor = _validate_cursor(cursor)
        params: dict[str, Any] = {"limit": limit, "raw_json": 1}
        if cursor is not None:
            params["after"] = cursor
        if options is None:
            options = {}
        elif not isinstance(options, Mapping):
            raise ReachError("invalid_option", "Reddit profile-post options must be an object.")
        sort = options.get("sort")
        if sort is not None:
            if not isinstance(sort, str) or sort not in {"hot", "new", "top", "controversial"}:
                raise ReachError("invalid_option", "Reddit profile-post sort must be hot, new, top, or controversial.")
            params["sort"] = sort
        payload = await self._get(f"/user/{quote(username, safe='')}/submitted", params)
        return _page_from_listing("profile_posts", payload, limit)
