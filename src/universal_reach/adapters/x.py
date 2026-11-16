"""Read-only X adapter for anonymous post embeds and opt-in paid API reads.

The official API supports recent post search (last seven days) and post lookup
with app-only authentication for public data. X currently bills fetched Posts
under its pay-per-use model (the published price was $0.005 per Post resource
when checked on 2026-11-13). Review current pricing before enabling this
adapter: https://docs.x.com/x-api/getting-started/pricing

Exact public post reads default to X's public oEmbed endpoint and return only
limited inert metadata. Search and more detailed exact reads use the official
API and are disabled unless both X_API_BEARER_TOKEN and
X_API_ALLOW_PAID_READS=true are present. The token must be app-only; user-
context access is not used. This module never logs credentials and makes GET
requests only.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlsplit

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_json
from universal_reach.models import AccessInfo, AdapterStatus, Pagination, Record, ResultPage

API_HOST = "api.x.com"
API_ROOT = f"https://{API_HOST}/2"
OEMBED_URL = "https://publish.x.com/oembed"
OEMBED_HOSTS = frozenset({"publish.x.com"})
POST_URL_HOSTS = frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"})
_MAX_LIMIT = 100
_MAX_QUERY_LENGTH = 4096
_MAX_CURSOR_LENGTH = 4096
_POST_ID = re.compile(r"^[0-9]{1,19}$")
_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_POST_FIELDS = "created_at,author_id,public_metrics,conversation_id,lang,possibly_sensitive"
_USER_FIELDS = "username,name,verified"
_MAX_OEMBED_HTML = 100_000


def _truthy(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes"}


def _configuration() -> tuple[str, dict[str, str]]:
    """Return environment-supplied auth after an explicit paid-read opt-in."""
    token = os.environ.get("X_API_BEARER_TOKEN", "").strip()
    if not token or len(token) > 8192 or any(char.isspace() for char in token):
        raise ReachError(
            "x_auth_required",
            "Set X_API_BEARER_TOKEN to an app-only X API Bearer token outside chat; no request was sent.",
        )
    if not _truthy(os.environ.get("X_API_ALLOW_PAID_READS")):
        raise ReachError(
            "x_paid_read_opt_in_required",
            "X reads use pay-per-use credits. Review current pricing and set X_API_ALLOW_PAID_READS=true to enable billable requests; no request was sent.",
        )
    return token, {"Authorization": f"Bearer {token}"}


def _validate_limit(limit: int) -> int:
    # X documents a minimum max_results of 10, and a maximum of 100. Reject
    # lower limits rather than making an unexpectedly larger billable request.
    if isinstance(limit, bool) or not isinstance(limit, int) or not 10 <= limit <= _MAX_LIMIT:
        raise ReachError(
            "invalid_limit",
            f"X recent search requires a page size between 10 and {_MAX_LIMIT}; smaller requests are not supported by the API.",
            details={"limit": limit, "minimum": 10, "maximum": _MAX_LIMIT},
        )
    return limit


def _validate_cursor(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor.strip() or len(cursor) > _MAX_CURSOR_LENGTH:
        raise ReachError("invalid_cursor", "The X pagination cursor is malformed.")
    return cursor


def _decode_segment(segment: str) -> str:
    if re.search(r"%(?:2f|5c)", segment, re.IGNORECASE):
        raise ReachError("invalid_url", "Encoded path separators are not allowed in X post URLs.")
    try:
        decoded = unquote(segment, errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReachError("invalid_url", "The X URL contains malformed path encoding.") from exc
    if not decoded or "/" in decoded or "\\" in decoded:
        raise ReachError("invalid_url", "The X URL contains an invalid path segment.")
    return decoded


def _parse_post_url(value: str) -> str:
    """Extract only a numeric post ID from a canonical X/Twitter status URL."""
    if not isinstance(value, str):
        raise ReachError("invalid_url", "An X post URL is required.")
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise ReachError("invalid_url", "The X URL is malformed.") from exc

    if (
        parsed.scheme.lower() != "https"
        or host not in POST_URL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ReachError("invalid_url", "Only HTTPS post URLs on x.com or twitter.com are supported.")

    path = parsed.path
    if not path.startswith("/") or path.startswith("//"):
        raise ReachError("invalid_url", "The X URL path is malformed.")
    if path.endswith("/"):
        path = path[:-1]
    raw_segments = path[1:].split("/")
    if not raw_segments or any(not segment for segment in raw_segments):
        raise ReachError("invalid_url", "The X URL does not contain a post path.")
    segments = [_decode_segment(part) for part in raw_segments]
    if len(segments) == 4 and segments[:3] == ["i", "web", "status"]:
        post_id = segments[3]
    elif len(segments) == 3 and segments[1] == "status" and _USERNAME.fullmatch(segments[0]):
        post_id = segments[2]
    else:
        raise ReachError(
            "invalid_url",
            "Supported X post URLs use /{username}/status/{id} or /i/web/status/{id}.",
        )
    if not _POST_ID.fullmatch(post_id):
        raise ReachError("invalid_url", "The X URL contains an invalid post ID.")
    # Query strings and fragments are ignored; no supplied URL is fetched.
    return post_id


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _safe_username(value: Any) -> str | None:
    return value if isinstance(value, str) and _USERNAME.fullmatch(value) else None


def _included_users(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    includes = payload.get("includes")
    if not isinstance(includes, Mapping):
        return {}
    users = includes.get("users")
    if not isinstance(users, list):
        return {}
    mapped: dict[str, Mapping[str, Any]] = {}
    for user in users:
        if isinstance(user, Mapping) and isinstance(user.get("id"), str):
            mapped[user["id"]] = user
    return mapped


def _post_record(post: Mapping[str, Any], user: Mapping[str, Any] | None = None) -> Record:
    post_id = post.get("id")
    if not isinstance(post_id, str) or not _POST_ID.fullmatch(post_id):
        raise ReachError("parser_error", "X returned a Post without a valid numeric ID.")
    author_id = post.get("author_id") if isinstance(post.get("author_id"), str) else None
    user = user if isinstance(user, Mapping) else {}
    username = _safe_username(user.get("username"))
    metrics_raw = post.get("public_metrics")
    metrics_raw = metrics_raw if isinstance(metrics_raw, Mapping) else {}
    metric_names = {
        "like_count": "like_count",
        "reply_count": "reply_count",
        "retweet_count": "retweet_count",
        "quote_count": "quote_count",
        "bookmark_count": "bookmark_count",
        "impression_count": "impression_count",
    }
    metrics = {
        normalized: count
        for source, normalized in metric_names.items()
        if (count := _count(metrics_raw.get(source))) is not None
    }
    extensions = {
        key: value
        for key, value in {
            "author_id": author_id,
            "username": username,
            "display_name": user.get("name") if isinstance(user.get("name"), str) else None,
            "verified": user.get("verified") if isinstance(user.get("verified"), bool) else None,
            "conversation_id": post.get("conversation_id") if isinstance(post.get("conversation_id"), str) else None,
            "lang": post.get("lang") if isinstance(post.get("lang"), str) else None,
            "possibly_sensitive": post.get("possibly_sensitive") if isinstance(post.get("possibly_sensitive"), bool) else None,
        }.items()
        if value is not None
    }
    url = f"https://x.com/{username}/status/{post_id}" if username else f"https://x.com/i/web/status/{post_id}"
    return Record(
        platform="x",
        type="post",
        id=post_id,
        url=url,
        author=f"@{username}" if username else author_id,
        text=post.get("text") if isinstance(post.get("text"), str) else None,
        created_at=post.get("created_at") if isinstance(post.get("created_at"), str) else None,
        metrics=metrics,
        access=AccessInfo(mode="authenticated", complete=True),
        extensions=extensions,
    )


class _OEmbedParagraphParser(HTMLParser):
    """Extract only the first post paragraph as inert text; never render HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._found_paragraph = False
        self._in_paragraph = False
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "p" and not self._found_paragraph:
            self._found_paragraph = True
            self._in_paragraph = True
        elif tag == "br" and self._in_paragraph:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "p" and self._in_paragraph:
            self._in_paragraph = False

    def handle_data(self, data: str) -> None:
        if self._in_paragraph and self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str | None:
        if not self._found_paragraph:
            return None
        lines = [" ".join(line.split()) for line in "".join(self._parts).splitlines()]
        result = "\n".join(line for line in lines if line)
        return result[:20_000] or None


def _extract_oembed_text(document: Any) -> str | None:
    if not isinstance(document, str) or len(document) > _MAX_OEMBED_HTML:
        raise ReachError("parser_error", "X returned malformed or oversized oEmbed markup.")
    parser = _OEmbedParagraphParser()
    try:
        parser.feed(document)
        parser.close()
    except (AssertionError, ValueError) as exc:
        raise ReachError("parser_error", "X returned malformed oEmbed markup.") from exc
    return parser.text()


def _author_handle(author_url: Any) -> str | None:
    if not isinstance(author_url, str) or len(author_url) > 4096:
        return None
    try:
        parsed = urlsplit(author_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or host not in POST_URL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    segments = parsed.path.rstrip("/").lstrip("/").split("/")
    if len(segments) != 1:
        return None
    try:
        handle = _decode_segment(segments[0])
    except ReachError:
        return None
    return handle if _USERNAME.fullmatch(handle) else None


def _api_error(error: ReachError) -> ReachError:
    status = error.details.get("http_status")
    if status == 401:
        return ReachError("x_authentication_failed", "X rejected the configured Bearer token.", details={"http_status": 401})
    if status == 403:
        return ReachError(
            "x_access_denied",
            "X denied this API request. Check app approval, read permissions, endpoint access, and current account credits.",
            details={"http_status": 403},
        )
    if status == 402:
        return ReachError(
            "x_credits_required",
            "X requires available API credits for this read. Check the developer console and current pay-per-use pricing.",
            details={"http_status": 402},
        )
    if status == 429:
        return ReachError(
            "rate_limited",
            "X rate-limited this request; wait for its limit window to reset before retrying.",
            retryable=True,
            details={"http_status": 429},
        )
    return error


class XAdapter(Adapter):
    """Anonymous public post embeds plus opt-in X API search and lookup."""

    url_hosts = frozenset({"x.com", "twitter.com"})

    def __init__(self) -> None:
        super().__init__(adapter_capability("x", ("search", "read")))

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        _token, headers = _configuration()
        try:
            return await get_json(
                f"{API_ROOT}{path}",
                allowed_hosts=frozenset({API_HOST}),
                params=params,
                headers=headers,
                retries=0,  # Avoid duplicating a potentially billable read.
            )
        except ReachError as exc:
            raise _api_error(exc) from exc

    async def search(
        self,
        query: str,
        limit: int = 10,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY_LENGTH:
            raise ReachError("invalid_query", "X search query must be between 1 and 4096 characters.")
        limit = _validate_limit(limit)
        cursor = _validate_cursor(cursor)
        params: dict[str, Any] = {
            "query": query,
            "max_results": limit,
            "post.fields": _POST_FIELDS,
            "expansions": "author_id",
            "user.fields": _USER_FIELDS,
        }
        if cursor is not None:
            params["pagination_token"] = cursor
        payload = await self._get("/tweets/search/recent", params)
        if not isinstance(payload, Mapping):
            raise ReachError("parser_error", "X returned a malformed search response.")
        errors = payload.get("errors")
        errors = errors if isinstance(errors, list) else []
        raw_posts = payload.get("data")
        if raw_posts is None:
            raw_posts = []
        if not isinstance(raw_posts, list):
            raise ReachError("parser_error", "X returned malformed search results.")
        if errors and not raw_posts:
            raise ReachError("x_api_error", "X returned an API error instead of search results.", details={"error_count": len(errors)})

        users = _included_users(payload)
        records: list[Record] = []
        malformed_count = 0
        for post in raw_posts:
            if not isinstance(post, Mapping):
                malformed_count += 1
                continue
            author_id = post.get("author_id")
            try:
                records.append(_post_record(post, users.get(author_id) if isinstance(author_id, str) else None))
            except ReachError:
                malformed_count += 1

        meta = payload.get("meta")
        meta = meta if isinstance(meta, Mapping) else {}
        next_token = meta.get("next_token") if isinstance(meta.get("next_token"), str) else None
        if next_token is not None and (not next_token.strip() or len(next_token) > _MAX_CURSOR_LENGTH):
            next_token = None
            malformed_count += 1
        warnings: list[str] = []
        if errors:
            warnings.append("X returned partial-result errors; some search results may be missing.")
        if malformed_count:
            warnings.append(f"Skipped {malformed_count} malformed item(s) from the X response.")
        has_partial_results = bool(errors or malformed_count)
        return ResultPage(
            platform="x",
            operation="search",
            items=records,
            pagination=Pagination(cursor=next_token, has_more=next_token is not None),
            complete=next_token is None and not has_partial_results,
            warnings=warnings,
            extensions={"result_count": _count(meta.get("result_count"))} if _count(meta.get("result_count")) is not None else {},
        )

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        post_id = _parse_post_url(url)
        if options is None:
            options = {}
        if not isinstance(options, Mapping) or set(options) - {"auth_mode"}:
            raise ReachError("invalid_option", "X read accepts only the auth_mode option ('anonymous' or 'api').")
        auth_mode = options.get("auth_mode", "anonymous")
        if auth_mode == "anonymous":
            return await self._read_oembed(post_id)
        if auth_mode != "api":
            raise ReachError("invalid_option", "X auth_mode must be 'anonymous' or 'api'.")

        payload = await self._get(
            f"/tweets/{post_id}",
            {
                "post.fields": _POST_FIELDS,
                "expansions": "author_id",
                "user.fields": _USER_FIELDS,
            },
        )
        if not isinstance(payload, Mapping):
            raise ReachError("parser_error", "X returned a malformed Post response.")
        post = payload.get("data")
        if not isinstance(post, Mapping):
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                raise ReachError("x_api_error", "X returned an API error for this Post lookup.", details={"error_count": len(errors)})
            raise ReachError("not_found", "X did not return the requested public Post.")
        author_id = post.get("author_id")
        user_map = _included_users(payload)
        try:
            return _post_record(post, user_map.get(author_id) if isinstance(author_id, str) else None)
        except ReachError as exc:
            raise ReachError("parser_error", "X returned a malformed Post response.") from exc

    async def _read_oembed(self, post_id: str) -> Record:
        canonical_url = f"https://x.com/i/web/status/{post_id}"
        payload = await get_json(
            OEMBED_URL,
            allowed_hosts=OEMBED_HOSTS,
            params={"url": canonical_url, "omit_script": "true", "hide_thread": "true"},
            retries=0,
        )
        if not isinstance(payload, Mapping) or payload.get("type") != "rich":
            raise ReachError("parser_error", "X returned a malformed oEmbed response.")
        response_url = payload.get("url")
        if response_url is not None:
            try:
                response_post_id = _parse_post_url(response_url)
            except ReachError as exc:
                raise ReachError("parser_error", "X returned a malformed post URL in its oEmbed response.") from exc
            if response_post_id != post_id:
                raise ReachError("parser_error", "X returned oEmbed data for a different Post.")
        text = _extract_oembed_text(payload.get("html"))
        author_name = payload.get("author_name")
        if not isinstance(author_name, str) or not author_name.strip() or len(author_name) > 256:
            author_name = None
        handle = _author_handle(payload.get("author_url"))
        if text is None and author_name is None:
            raise ReachError("parser_error", "X returned no usable public Post metadata.")
        return Record(
            platform="x",
            type="post",
            id=post_id,
            url=canonical_url,
            author=f"@{handle}" if handle else author_name,
            text=text,
            access=AccessInfo(mode="anonymous", complete=False),
            extensions={"metadata_source": "x_oembed", **({"author_handle": handle} if handle else {})},
        )

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        token_present = bool(os.environ.get("X_API_BEARER_TOKEN", "").strip())
        enabled = _truthy(os.environ.get("X_API_ALLOW_PAID_READS"))
        if not token_present:
            message = "Anonymous exact-post reads use X oEmbed; no paid API token is configured and no live check was performed."
        elif not enabled:
            message = "Anonymous exact-post reads use X oEmbed; the configured API token is not enabled for paid reads and no live check was performed."
        else:
            message = "X read credentials and paid-read opt-in are configured; no live API call was made."
        return AdapterStatus(
            platform="x",
            status="degraded",
            message=message,
            operations=list(self.capability.operations),
            access_mode="authenticated" if token_present and enabled else "anonymous",
        )
