"""Anonymous, read-only adapters for public 4chan, Lemmy, and Discourse content."""

from __future__ import annotations

import html
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_json
from universal_reach.models import AccessInfo, AdapterStatus, Pagination, Record, ResultPage
from universal_reach.security import validate_https_url, validate_public_instance_url


FOURCHAN_API_BASE = "https://a.4cdn.org"
FOURCHAN_API_HOSTS = frozenset({"a.4cdn.org"})
FOURCHAN_URL_HOSTS = frozenset({
    "boards.4chan.org", "boards.4channel.org", "4chan.org", "4channel.org",
})
_BOARD_RE = re.compile(r"^[a-z0-9]{1,8}$")
_POSITIVE_ID_RE = re.compile(r"^[1-9][0-9]{0,15}$")
_FOURCHAN_THREAD_RE = re.compile(r"^/([a-z0-9]{1,8})/thread/([1-9][0-9]{0,15})(?:/[^/]*)?/?$")
_FOURCHAN_LAST_REQUEST_AT: float | None = None
_FOURCHAN_THREAD_REQUESTS: dict[str, float] = {}
_FOURCHAN_RATE_LOCK = threading.Lock()
_FOURCHAN_REQUEST_INTERVAL = 1.0
_FOURCHAN_THREAD_INTERVAL = 10.0


def _limit(value: int, maximum: int = 100) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ReachError("invalid_input", f"limit must be between 1 and {maximum}.")
    return value


def _offset(cursor: str | None, *, maximum: int = 1_000_000) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.isdecimal():
        raise ReachError("invalid_input", "cursor must be a non-negative numeric offset.")
    value = int(cursor)
    if value > maximum:
        raise ReachError("invalid_input", "cursor is outside the supported range.")
    return value


def _plain_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    # The upstream APIs commonly return rendered HTML. Preserve visible text and
    # line breaks without exposing tags as if they were user-authored content.
    value = re.sub(r"(?i)<br\s*/?>|</(?:p|div|li|blockquote|h[1-6])\s*>", "\n", value)
    value = re.sub(r"<[^>]*>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t\xa0]+", " ", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    return value.strip() or None


def _iso_timestamp(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _source_extensions(source_url: str, attribution: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"source_url": source_url}
    if attribution:
        result["attribution"] = attribution
    return result


class FourChanAdapter(Adapter):
    """Read public board catalogs and threads through 4chan's read-only JSON API.

    Search is intentionally local and board-scoped: the official JSON API has
    no global full-text search, and catalog matching sees only the active-board
    catalog's available titles and preview text.
    """

    url_hosts = FOURCHAN_URL_HOSTS

    def __init__(self) -> None:
        super().__init__(adapter_capability("4chan", ("search", "read", "replies")))

    @staticmethod
    def _parse_thread_url(url: str) -> tuple[str, str]:
        validate_https_url(url, FOURCHAN_URL_HOSTS)
        match = _FOURCHAN_THREAD_RE.fullmatch(urlsplit(url).path)
        if not match:
            raise ReachError("invalid_url", "Expected a public 4chan board thread URL.")
        return match.group(1), match.group(2)

    @staticmethod
    def _validate_board(board: Any) -> str:
        if not isinstance(board, str) or not _BOARD_RE.fullmatch(board):
            raise ReachError("invalid_input", "A valid 4chan board must be supplied in options.board.")
        return board

    @staticmethod
    def _reserve_request(thread_key: str | None = None) -> None:
        """Enforce official request intervals before every upstream request."""
        global _FOURCHAN_LAST_REQUEST_AT
        now = time.monotonic()
        with _FOURCHAN_RATE_LOCK:
            global_wait = 0.0 if _FOURCHAN_LAST_REQUEST_AT is None else _FOURCHAN_REQUEST_INTERVAL - (now - _FOURCHAN_LAST_REQUEST_AT)
            thread_last = _FOURCHAN_THREAD_REQUESTS.get(thread_key) if thread_key else None
            thread_wait = 0.0 if thread_last is None else _FOURCHAN_THREAD_INTERVAL - (now - thread_last)
            wait = max(global_wait, thread_wait)
            if wait > 0:
                raise ReachError(
                    "rate_limited",
                    "The 4chan API request interval has not elapsed yet.",
                    retryable=True,
                    details={"retry_after_seconds": round(wait, 3), "thread_refresh_seconds": _FOURCHAN_THREAD_INTERVAL},
                )
            _FOURCHAN_LAST_REQUEST_AT = now
            if thread_key:
                _FOURCHAN_THREAD_REQUESTS[thread_key] = now

    async def _api_json(
        self,
        path: str,
        *,
        thread_key: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> Any:
        self._reserve_request(thread_key)
        headers: dict[str, str] = {}
        modified = (options or {}).get("if_modified_since")
        if isinstance(modified, str) and 1 <= len(modified) <= 128 and "\r" not in modified and "\n" not in modified:
            headers["If-Modified-Since"] = modified
        return await get_json(
            f"{FOURCHAN_API_BASE}{path}",
            allowed_hosts=FOURCHAN_API_HOSTS,
            headers=headers or None,
            retries=0,
        )

    @staticmethod
    def _catalog_threads(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise ReachError("parser_error", "The 4chan catalog response was not a JSON list.")
        threads: list[dict[str, Any]] = []
        for page in payload:
            if not isinstance(page, dict):
                continue
            rows = page.get("threads")
            if isinstance(rows, list):
                threads.extend(row for row in rows if isinstance(row, dict))
        return threads

    @staticmethod
    def _thread_posts(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("posts"), list):
            raise ReachError("parser_error", "The 4chan thread response did not contain a posts list.")
        posts = [post for post in payload["posts"] if isinstance(post, dict)]
        if not posts:
            raise ReachError("not_found", "The 4chan thread is no longer publicly available.")
        return posts

    def _record(self, post: dict[str, Any], board: str, thread_id: str, *, is_op: bool = False) -> Record:
        post_id = post.get("no")
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0:
            raise ReachError("parser_error", "The 4chan API returned a post without a valid ID.")
        url = f"https://boards.4chan.org/{board}/thread/{thread_id}"
        if not is_op:
            url += f"#p{post_id}"
        extensions = _source_extensions(url, "Source: 4chan")
        extensions.update({"board": board, "thread_id": thread_id})
        for key in ("filename", "ext", "tim", "md5", "w", "h", "tn_w", "tn_h"):
            value = post.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                extensions[f"file_{key}"] = value
        return Record(
            platform=self.platform,
            type="thread" if is_op else "reply",
            id=str(thread_id if is_op else post_id),
            url=url,
            author=post.get("name") if isinstance(post.get("name"), str) else None,
            title=_plain_text(post.get("sub")) if is_op else None,
            text=_plain_text(post.get("com")),
            created_at=_iso_timestamp(post.get("time")),
            metrics={"reply_count": post.get("replies")} if isinstance(post.get("replies"), int) and not isinstance(post.get("replies"), bool) else {},
            access=AccessInfo(mode="anonymous", complete=True),
            extensions=extensions,
        )

    async def search(
        self, query: str, limit: int = 10, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        limit = _limit(limit)
        offset = _offset(cursor)
        board = self._validate_board((options or {}).get("board"))
        needle = query.casefold().strip()
        if not needle:
            raise ReachError("invalid_input", "query must not be empty.")
        payload = await self._api_json(f"/{board}/catalog.json", options=options)
        candidates = self._catalog_threads(payload)
        matched = [
            row for row in candidates
            if needle in " ".join(filter(None, (_plain_text(row.get("sub")), _plain_text(row.get("com"))))).casefold()
        ]
        selected = matched[offset:offset + limit]
        items = [
            self._record(row, board, str(row.get("no")), is_op=True)
            for row in selected
            if isinstance(row.get("no"), int) and not isinstance(row.get("no"), bool) and row["no"] > 0
        ]
        next_offset = offset + len(selected)
        has_more = next_offset < len(matched)
        return ResultPage(
            platform=self.platform,
            operation="search",
            items=items,
            pagination=Pagination(cursor=str(next_offset) if has_more else None, has_more=has_more, detail="Offset into the current board catalog matches."),
            complete=False,
            warnings=["4chan has no global full-text search. Results are local matches in the selected board's active catalog and preview text only."],
            extensions={"board": board, "source_attribution": "Source: 4chan", "source_url": f"https://boards.4chan.org/{board}/"},
        )

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        board, thread_id = self._parse_thread_url(url)
        payload = await self._api_json(f"/{board}/thread/{thread_id}.json", thread_key=f"{board}/{thread_id}", options=options)
        posts = self._thread_posts(payload)
        record = self._record(posts[0], board, thread_id, is_op=True)
        record.url = url
        record.extensions["source_url"] = url
        return record

    async def replies(
        self, url: str, limit: int = 50, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        limit = _limit(limit)
        offset = _offset(cursor)
        board, thread_id = self._parse_thread_url(url)
        payload = await self._api_json(f"/{board}/thread/{thread_id}.json", thread_key=f"{board}/{thread_id}", options=options)
        posts = self._thread_posts(payload)
        raw_replies = posts[1:]
        selected = raw_replies[offset:offset + limit]
        items = [self._record(post, board, thread_id) for post in selected]
        next_offset = offset + len(selected)
        has_more = next_offset < len(raw_replies)
        return ResultPage(
            platform=self.platform,
            operation="replies",
            items=items,
            pagination=Pagination(cursor=str(next_offset) if has_more else None, has_more=has_more, detail="Offset into this thread's currently returned replies; the thread API will not be refreshed more than once per 10 seconds."),
            complete=not has_more,
            warnings=["Source: 4chan. Thread content may change; refreshes are limited to one per 10 seconds."],
            extensions={"parent_id": thread_id, "source_url": url, "source_attribution": "Source: 4chan"},
        )

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        del options
        try:
            payload = await self._api_json("/boards.json")
            if not isinstance(payload, dict) or not isinstance(payload.get("boards"), list):
                raise ReachError("parser_error", "The 4chan boards endpoint returned an invalid response.")
            return AdapterStatus(
                platform=self.platform,
                status="available",
                message="4chan public API responded to a live check.",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )
        except ReachError as exc:
            return AdapterStatus(
                platform=self.platform,
                status="degraded" if exc.code == "rate_limited" else "unavailable",
                message=f"4chan health check failed ({exc.code}).",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )


class LemmyAdapter(Adapter):
    """Read public Lemmy posts and comments from a caller-configured instance."""

    def __init__(self) -> None:
        super().__init__(adapter_capability("lemmy", ("search", "read", "replies")))

    @staticmethod
    def _instance(options: dict[str, Any] | None) -> tuple[str, frozenset[str]]:
        instance = (options or {}).get("instance_url")
        if not isinstance(instance, str) or not instance.strip():
            raise ReachError("invalid_input", "options.instance_url must be a public HTTPS Lemmy instance origin.")
        return validate_public_instance_url(instance)

    @staticmethod
    def _url_kind(url: str) -> tuple[str, str]:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise ReachError("invalid_url", "The Lemmy content URL is malformed.") from exc
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or port not in (None, 443):
            raise ReachError("invalid_url", "Lemmy object URLs must be public HTTPS URLs without credentials or a non-standard port.")
        segments = [part for part in parsed.path.split("/") if part]
        if len(segments) < 2 or segments[0] not in {"post", "comment"} or not _POSITIVE_ID_RE.fullmatch(segments[1]):
            raise ReachError("invalid_url", "Expected a Lemmy public post or comment URL.")
        # Lemmy resolves the object URL at the configured instance. Validate the
        # referenced origin too, so a URL passed for resolution cannot direct
        # the remote server at a local or private destination.
        validate_public_instance_url(f"https://{parsed.netloc}")
        return segments[0], segments[1]

    @staticmethod
    def _validate_page(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ReachError("parser_error", "The Lemmy API returned an invalid JSON object.")
        return payload

    @staticmethod
    def _deleted(view: dict[str, Any]) -> bool:
        for key in ("post", "comment"):
            obj = view.get(key)
            if isinstance(obj, dict) and (obj.get("deleted") is True or obj.get("removed") is True):
                return True
        return False

    def _record(self, view: dict[str, Any], base: str, expected: str | None = None) -> Record | None:
        kind: str
        obj: dict[str, Any]
        if isinstance(view.get("post"), dict):
            kind, obj = "post", view["post"]
        elif isinstance(view.get("comment"), dict):
            kind, obj = "comment", view["comment"]
        elif expected and expected in view and isinstance(view[expected], dict):
            kind, obj = expected, view[expected]
        else:
            return None
        if self._deleted(view):
            return None
        object_id = obj.get("id")
        if isinstance(object_id, bool) or not isinstance(object_id, int) or object_id <= 0:
            return None
        creator = view.get("creator")
        community = view.get("community")
        creator_name = creator.get("name") if isinstance(creator, dict) and isinstance(creator.get("name"), str) else None
        url = f"{base}/{kind}/{object_id}"
        if kind == "post":
            title = obj.get("name") if isinstance(obj.get("name"), str) else None
            text = obj.get("body") if isinstance(obj.get("body"), str) else None
            created = obj.get("published_at") if isinstance(obj.get("published_at"), str) else None
            counts = view.get("counts") if isinstance(view.get("counts"), dict) else {}
            metrics = {"score": counts.get("score"), "comment_count": counts.get("comments")}
            external_url = obj.get("url") if isinstance(obj.get("url"), str) else None
            if external_url:
                extensions: dict[str, Any] = {"external_url": external_url}
            else:
                extensions = {}
        else:
            title = None
            text = obj.get("content") if isinstance(obj.get("content"), str) else None
            created = obj.get("published_at") if isinstance(obj.get("published_at"), str) else None
            counts = view.get("counts") if isinstance(view.get("counts"), dict) else {}
            metrics = {"score": counts.get("score"), "child_count": obj.get("child_count")}
            extensions = {}
        if isinstance(community, dict):
            extensions["community"] = community.get("name")
        if isinstance(obj.get("ap_id"), str):
            extensions["ap_id"] = obj["ap_id"]
        extensions["instance_url"] = base
        return Record(
            platform=self.platform,
            type=kind,
            id=str(object_id),
            url=url,
            author=creator_name,
            title=title,
            text=text,
            created_at=created,
            metrics={key: val for key, val in metrics.items() if isinstance(val, (int, float)) and not isinstance(val, bool)},
            access=AccessInfo(mode="anonymous", complete=True),
            extensions=extensions,
        )

    @staticmethod
    def _search_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        items = payload.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        result: list[dict[str, Any]] = []
        for key in ("posts", "comments"):
            values = payload.get(key)
            if isinstance(values, list):
                result.extend(item for item in values if isinstance(item, dict))
        return result

    async def search(
        self, query: str, limit: int = 10, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        limit = _limit(limit)
        if not isinstance(query, str) or not query.strip():
            raise ReachError("invalid_input", "query must not be empty.")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 2048):
            raise ReachError("invalid_input", "Lemmy page_cursor must not exceed 2048 characters.")
        base, hosts = self._instance(options)
        payload = self._validate_page(await get_json(
            f"{base}/api/v4/search",
            allowed_hosts=hosts,
            public_instance=True,
            params={"search_term": query.strip(), "type_": "all", "limit": limit, "page_cursor": cursor},
        ))
        warnings: list[str] = []
        rows = self._search_items(payload)
        items: list[Record] = []
        for row in rows:
            record = self._record(row, base)
            if record is None:
                if not self._deleted(row):
                    warnings.append("A Lemmy result did not contain a normalized public post or comment and was skipped.")
                continue
            items.append(record)
        over_limit = len(items) > limit
        if over_limit:
            warnings.append("The Lemmy instance returned more records than requested; excess records were omitted.")
        next_cursor = payload.get("next_page") if isinstance(payload.get("next_page"), str) and payload.get("next_page") else None
        if next_cursor is None and isinstance(payload.get("next_cursor"), str) and payload.get("next_cursor"):
            next_cursor = payload["next_cursor"]
        return ResultPage(
            platform=self.platform,
            operation="search",
            items=items[:limit],
            pagination=Pagination(cursor=next_cursor, has_more=next_cursor is not None, detail="Lemmy page_cursor is passed back unchanged."),
            complete=not warnings and not over_limit and next_cursor is None,
            warnings=warnings,
            extensions={"instance_url": base},
        )

    async def _resolve_url(
        self, url: str, base: str, hosts: frozenset[str], *, options: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        kind, _ = self._url_kind(url)
        payload = self._validate_page(await get_json(
            f"{base}/api/v4/search",
            allowed_hosts=hosts,
            public_instance=True,
            params={"search_term": url, "type_": "posts" if kind == "post" else "comments", "limit": 1},
        ))
        candidates = self._search_items(payload)
        for candidate in candidates:
            obj = candidate.get(kind)
            if isinstance(obj, dict) and isinstance(obj.get("id"), int) and obj["id"] > 0 and not self._deleted(candidate):
                return kind, candidate
        raise ReachError("not_found", "The Lemmy object could not be resolved as public content.")

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        base, hosts = self._instance(options)
        kind, view = await self._resolve_url(url, base, hosts)
        record = self._record(view, base, kind)
        if record is None:
            raise ReachError("not_found", "The Lemmy object is no longer publicly available.")
        record.extensions["source_url"] = url
        return record

    async def replies(
        self, url: str, limit: int = 50, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        limit = _limit(limit)
        base, hosts = self._instance(options)
        kind, object_id = self._url_kind(url)
        if kind == "post":
            _, view = await self._resolve_url(url, base, hosts)
            post = view.get("post")
            if not isinstance(post, dict) or not isinstance(post.get("id"), int):
                raise ReachError("not_found", "The Lemmy post could not be resolved as public content.")
            relation = {"post_id": post["id"]}
            parent_id: str | int = post["id"]
            relation_name = "post"
        else:
            _, view = await self._resolve_url(url, base, hosts)
            comment = view.get("comment")
            if not isinstance(comment, dict) or not isinstance(comment.get("id"), int):
                raise ReachError("not_found", "The Lemmy comment could not be resolved as public content.")
            relation = {"parent_id": comment["id"], "post_id": comment.get("post_id")}
            parent_id = comment["id"]
            relation_name = "comment"
        payload = self._validate_page(await get_json(
            f"{base}/api/v4/comment/list",
            allowed_hosts=hosts,
            public_instance=True,
            params={**relation, "limit": limit, "page_cursor": cursor, "sort": "old", "type_": "all"},
        ))
        rows = payload.get("items", payload.get("comments", []))
        if not isinstance(rows, list):
            raise ReachError("parser_error", "The Lemmy comment list response did not contain an items list.")
        warnings: list[str] = []
        records: list[Record] = []
        for row in rows:
            if not isinstance(row, dict):
                warnings.append("A Lemmy reply was malformed and was skipped.")
                continue
            record = self._record(row, base, "comment")
            if record is None:
                if not self._deleted(row):
                    warnings.append("A Lemmy reply could not be normalized and was skipped.")
                continue
            records.append(record)
        over_limit = len(records) > limit
        if over_limit:
            warnings.append("The Lemmy instance returned more replies than requested; excess records were omitted.")
        next_cursor = payload.get("next_page") if isinstance(payload.get("next_page"), str) and payload.get("next_page") else None
        if next_cursor is None and isinstance(payload.get("next_cursor"), str) and payload.get("next_cursor"):
            next_cursor = payload["next_cursor"]
        return ResultPage(
            platform=self.platform,
            operation="replies",
            items=records[:limit],
            pagination=Pagination(cursor=next_cursor, has_more=next_cursor is not None, detail="Lemmy page_cursor is passed back unchanged."),
            complete=not warnings and not over_limit and next_cursor is None,
            warnings=warnings,
            extensions={"parent_id": str(parent_id), "parent_type": relation_name, "source_url": url, "instance_url": base},
        )

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        try:
            base, hosts = self._instance(options)
            payload = await get_json(
                f"{base}/api/v4/search",
                allowed_hosts=hosts,
                params={"search_term": "universal-reach-health-check", "type_": "all", "limit": 1},
                timeout=5.0,
                retries=0,
                public_instance=True,
            )
            if not isinstance(payload, dict):
                raise ReachError("parser_error", "The Lemmy search endpoint returned an invalid response.")
            return AdapterStatus(
                platform=self.platform,
                status="available",
                message="The configured Lemmy API responded to a live check.",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )
        except ReachError as exc:
            return AdapterStatus(
                platform=self.platform,
                status="degraded" if exc.code == "rate_limited" else "unavailable",
                message=f"Lemmy health check failed ({exc.code}).",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )


class DiscourseAdapter(Adapter):
    """Read only public Discourse search and topic JSON on a configured instance."""

    def __init__(self) -> None:
        super().__init__(adapter_capability("discourse", ("search", "read", "replies")))

    @staticmethod
    def _instance(options: dict[str, Any] | None) -> tuple[str, frozenset[str]]:
        instance = (options or {}).get("instance_url")
        if not isinstance(instance, str) or not instance.strip():
            raise ReachError("invalid_input", "options.instance_url must be a public HTTPS Discourse instance origin.")
        return validate_public_instance_url(instance)

    @staticmethod
    def _topic_id(url: str, base: str, hosts: frozenset[str]) -> str:
        validate_https_url(url, hosts)
        parsed = urlsplit(url)
        segments = [part for part in parsed.path.split("/") if part]
        if not segments or segments[0] != "t":
            raise ReachError("invalid_url", "Expected a public Discourse topic URL on the configured instance.")
        tail = segments[1:]
        topic_id: str | None = None
        if tail and _POSITIVE_ID_RE.fullmatch(tail[0]):
            topic_id = tail[0]
        elif len(tail) >= 2 and _POSITIVE_ID_RE.fullmatch(tail[1]):
            topic_id = tail[1]
        if topic_id is None or len(tail) > 3:
            raise ReachError("invalid_url", "The Discourse topic URL did not contain a valid topic ID.")
        return topic_id

    @staticmethod
    def _validate_payload(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ReachError("parser_error", "The Discourse API returned an invalid JSON object.")
        return payload

    @staticmethod
    def _visible(payload: dict[str, Any]) -> list[dict[str, Any]]:
        stream = payload.get("post_stream")
        if not isinstance(stream, dict):
            raise ReachError("parser_error", "The Discourse topic response did not contain a post stream.")
        posts = stream.get("posts")
        if not isinstance(posts, list):
            raise ReachError("parser_error", "The Discourse topic response did not contain a posts list.")
        return [post for post in posts if isinstance(post, dict)]

    def _record(
        self, post: dict[str, Any], *, base: str, topic_id: str,
        topic: dict[str, Any] | None = None, source_url: str | None = None,
    ) -> Record:
        post_id = post.get("id")
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0:
            raise ReachError("parser_error", "The Discourse API returned a post without a valid ID.")
        slug = post.get("topic_slug") or (topic or {}).get("slug") or str(topic_id)
        if not isinstance(slug, str):
            slug = str(topic_id)
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", slug).strip("-") or str(topic_id)
        post_number = post.get("post_number")
        url = f"{base}/t/{slug}/{topic_id}"
        if isinstance(post_number, int) and post_number > 1:
            url += f"/{post_number}"
        text = post.get("cooked") if isinstance(post.get("cooked"), str) else post.get("excerpt") if isinstance(post.get("excerpt"), str) else post.get("blurb")
        extensions: dict[str, Any] = {"topic_id": topic_id, "post_number": post_number}
        if source_url:
            extensions["source_url"] = source_url
        return Record(
            platform=self.platform,
            type="topic" if post_number == 1 else "reply",
            id=str(post_id),
            url=url,
            author=post.get("username") if isinstance(post.get("username"), str) else None,
            title=(topic or {}).get("title") if isinstance((topic or {}).get("title"), str) else post.get("topic_title") if isinstance(post.get("topic_title"), str) else None,
            text=_plain_text(text),
            created_at=post.get("created_at") if isinstance(post.get("created_at"), str) else None,
            metrics={key: value for key in ("like_count", "reply_count") if isinstance((value := post.get(key)), (int, float)) and not isinstance(value, bool)},
            access=AccessInfo(mode="anonymous", complete=isinstance(post.get("cooked"), str)),
            extensions=extensions,
        )

    async def search(
        self, query: str, limit: int = 10, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        limit = _limit(limit)
        if not isinstance(query, str) or not query.strip():
            raise ReachError("invalid_input", "query must not be empty.")
        base, hosts = self._instance(options)
        page = 1
        offset = 0
        if cursor is not None:
            match = re.fullmatch(r"([1-9][0-9]{0,5}):([0-9]{1,6})", cursor)
            if not match:
                raise ReachError("invalid_input", "The Discourse cursor must be a page:offset pair.")
            page, offset = int(match.group(1)), int(match.group(2))
        payload = self._validate_payload(await get_json(
            f"{base}/search.json",
            allowed_hosts=hosts,
            public_instance=True,
            params={"q": query.strip(), "page": page},
        ))
        rows = payload.get("posts")
        if not isinstance(rows, list):
            rows = []
        posts = [row for row in rows if isinstance(row, dict)]
        # Some instances return topics without matching post rows. Keep those
        # only as topic summaries; never turn hidden/private result placeholders
        # into content records.
        if not posts:
            topic_rows = payload.get("topics")
            if isinstance(topic_rows, list):
                posts = [
                    {"id": row.get("id"), "topic_id": row.get("id"), "topic_slug": row.get("slug"), "topic_title": row.get("title"), "post_number": 1, "created_at": row.get("created_at"), "username": row.get("last_poster_username"), "excerpt": row.get("excerpt")}
                    for row in topic_rows if isinstance(row, dict) and isinstance(row.get("id"), int)
                ]
        visible = posts[offset:offset + limit]
        records: list[Record] = []
        skipped = 0
        for post in visible:
            topic_id = post.get("topic_id") or post.get("id")
            if isinstance(topic_id, bool) or not isinstance(topic_id, int) or topic_id <= 0:
                skipped += 1
                continue
            try:
                records.append(self._record(post, base=base, topic_id=str(topic_id)))
            except ReachError:
                skipped += 1
        more_posts = payload.get("grouped_search_result", {}).get("more_posts") if isinstance(payload.get("grouped_search_result"), dict) else payload.get("more_posts")
        has_upstream_more = (
            more_posts.strip().casefold() not in {"", "false", "0", "none"}
            if isinstance(more_posts, str)
            else bool(more_posts)
        )
        next_offset = offset + len(visible)
        if next_offset < len(posts):
            next_cursor = f"{page}:{next_offset}"
        elif has_upstream_more:
            next_cursor = f"{page + 1}:0"
        else:
            next_cursor = None
        warnings = ["Some search results could not be normalized and were skipped."] if skipped else []
        if any(not record.access.complete for record in records):
            warnings.append("Some Discourse search results contain excerpts rather than complete post text.")
        return ResultPage(
            platform=self.platform,
            operation="search",
            items=records,
            pagination=Pagination(cursor=next_cursor, has_more=next_cursor is not None, detail="Search cursors contain the Discourse page and local offset."),
            complete=not warnings and next_cursor is None,
            warnings=warnings,
            extensions={"instance_url": base},
        )

    async def _topic(self, topic_id: str, base: str, hosts: frozenset[str]) -> dict[str, Any]:
        payload = self._validate_payload(await get_json(f"{base}/t/{topic_id}.json", allowed_hosts=hosts, public_instance=True))
        self._visible(payload)
        return payload

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        base, hosts = self._instance(options)
        topic_id = self._topic_id(url, base, hosts)
        payload = await self._topic(topic_id, base, hosts)
        posts = self._visible(payload)
        if not posts:
            raise ReachError("not_found", "The Discourse topic is not publicly available.")
        record = self._record(posts[0], base=base, topic_id=topic_id, topic=payload, source_url=url)
        record.type = "topic"
        record.id = topic_id
        record.url = url
        return record

    async def replies(
        self, url: str, limit: int = 50, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        limit = _limit(limit)
        offset = _offset(cursor)
        base, hosts = self._instance(options)
        topic_id = self._topic_id(url, base, hosts)
        payload = await self._topic(topic_id, base, hosts)
        posts = self._visible(payload)
        stream = payload.get("post_stream")
        stream_ids = stream.get("stream") if isinstance(stream, dict) else None
        raw_replies = [post for post in posts if post.get("post_number") != 1]
        selected = raw_replies[offset:offset + limit]
        records = [self._record(post, base=base, topic_id=topic_id, topic=payload, source_url=url) for post in selected]
        next_offset = offset + len(selected)
        more_visible = next_offset < len(raw_replies)
        stream_has_omitted = isinstance(stream_ids, list) and len(stream_ids) > len(posts)
        stream_is_partial = not isinstance(stream_ids, list) or stream_has_omitted
        # The documented get-specific-posts route requires an API key; the
        # adapter therefore never calls it and marks omitted stream entries.
        warnings = []
        if stream_is_partial:
            if isinstance(stream_ids, list):
                warnings.append("This public topic response contains only part of the post stream; additional replies require an API-key-only route and were not fetched.")
            else:
                warnings.append("The public topic response did not provide a complete post-stream index, so reply completeness cannot be verified.")
        return ResultPage(
            platform=self.platform,
            operation="replies",
            items=records,
            pagination=Pagination(cursor=str(next_offset) if more_visible else None, has_more=more_visible or stream_has_omitted, detail="Cursor is an offset into replies present in the public topic JSON response; omitted stream entries have no anonymous pagination route."),
            complete=not warnings and not more_visible,
            warnings=warnings,
            extensions={"parent_id": topic_id, "source_url": url, "instance_url": base, "visible_reply_count": len(raw_replies), "stream_post_count": len(stream_ids) if isinstance(stream_ids, list) else None},
        )

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        if not isinstance(options, dict) or not isinstance(options.get("instance_url"), str):
            return AdapterStatus(
                platform=self.platform,
                status="degraded",
                message="No single Discourse instance is selected for a live health check.",
                operations=list(self.capability.operations),
            )
        try:
            base, hosts = self._instance(options)
            payload = await get_json(
                f"{base}/latest.json",
                allowed_hosts=hosts,
                timeout=5.0,
                retries=0,
                public_instance=True,
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("topic_list"), dict):
                raise ReachError("parser_error", "The Discourse latest endpoint returned an invalid response.")
            return AdapterStatus(
                platform=self.platform,
                status="available",
                message="The configured Discourse instance responded to a live check.",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )
        except ReachError as exc:
            return AdapterStatus(
                platform=self.platform,
                status="degraded" if exc.code == "rate_limited" else "unavailable",
                message=f"Discourse health check failed ({exc.code}).",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )


__all__ = ["FourChanAdapter", "LemmyAdapter", "DiscourseAdapter"]
