"""Anonymous, read-only Stack Exchange API v2.3 adapter."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_json
from universal_reach.models import AccessInfo, AdapterStatus, Pagination, Record, ResultPage

API_ROOT = "https://api.stackexchange.com/2.3"
API_HOSTS = frozenset({"api.stackexchange.com"})
MAX_ANONYMOUS_PAGE = 25
MAX_PAGE_SIZE = 100
OPERATIONS = ("search", "read", "replies", "profile", "profile_posts")

_SITE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$", re.IGNORECASE)
_QUESTION_PATH_ID = re.compile(r"^(?:questions|q)/(\d+)(?:/|$)")
_SEARCH_OPTIONS = frozenset(
    {
        "site",
        "sort",
        "order",
        "tagged",
        "nottagged",
        "title",
        "body",
        "user",
        "answers",
        "accepted",
        "closed",
        "migrated",
        "notice",
        "views",
        "wiki",
        "fromdate",
        "todate",
        "min",
        "max",
    }
)


class StackExchangeAdapter(Adapter):
    """Read public questions, replies, and profiles through Stack Exchange API v2.3.

    ``stackoverflow`` uses the Stack Overflow API site parameter by default.
    The generic ``stackexchange`` platform requires ``options={"site": ...}``
    on every operation so the adapter never guesses which community to query.
    """

    url_hosts = frozenset(
        {
            "stackoverflow.com",
            "stackapps.com",
            "superuser.com",
            "serverfault.com",
            "askubuntu.com",
            "mathoverflow.net",
        }
    )

    def __init__(
        self,
        platform: str = "stackoverflow",
        *,
        timeout: float = 12.0,
        retries: int = 1,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        platform_id = platform.casefold()
        if platform_id not in {"stackoverflow", "stackexchange"}:
            raise ValueError("platform must be 'stackoverflow' or 'stackexchange'")
        super().__init__(adapter_capability(platform_id, OPERATIONS))
        self.url_hosts = (
            frozenset({"stackoverflow.com"})
            if platform_id == "stackoverflow"
            else frozenset(
                {
                    "stackexchange.com",
                    "stackapps.com",
                    "superuser.com",
                    "serverfault.com",
                    "askubuntu.com",
                    "mathoverflow.net",
                }
            )
        )
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep or asyncio.sleep
        self._backoff_until: dict[str, float] = {}

    @staticmethod
    def _options(options: dict[str, Any] | None) -> dict[str, Any]:
        if options is None:
            return {}
        if not isinstance(options, dict):
            raise ReachError("invalid_input", "Platform options must be an object.")
        return options

    def _site(self, options: dict[str, Any] | None) -> str:
        values = self._options(options)
        raw = values.get("site")
        if self.platform == "stackoverflow":
            if raw is not None and (not isinstance(raw, str) or raw.strip().casefold() != "stackoverflow"):
                raise ReachError(
                    "invalid_input",
                    "The stackoverflow adapter always uses the Stack Overflow site; use platform='stackexchange' to select another site.",
                    details={"platform": "stackoverflow", "site": raw},
                )
            raw = "stackoverflow"
        if not isinstance(raw, str) or not raw.strip():
            raise ReachError(
                "invalid_input",
                "The stackexchange adapter requires options.site (for example, 'math' or 'superuser').",
                details={"platform": self.platform, "required_option": "site"},
            )
        site = raw.strip().casefold()
        if not _SITE_PATTERN.fullmatch(site) or ".." in site:
            raise ReachError("invalid_input", "The Stack Exchange site identifier is invalid.")
        return site

    @staticmethod
    def _page(cursor: str | None) -> int:
        if cursor is None or cursor == "":
            return 1
        if isinstance(cursor, bool):
            raise ReachError("invalid_input", "The pagination cursor must be a page number.")
        if not isinstance(cursor, (str, int)) or (isinstance(cursor, str) and not cursor.isdecimal()):
            raise ReachError("invalid_input", "The pagination cursor must be a page number.")
        try:
            page = int(cursor)
        except (TypeError, ValueError) as exc:
            raise ReachError("invalid_input", "The pagination cursor must be a page number.") from exc
        if page < 1 or page > MAX_ANONYMOUS_PAGE:
            raise ReachError(
                "invalid_input",
                f"Anonymous Stack Exchange pagination supports pages 1 through {MAX_ANONYMOUS_PAGE}.",
                details={"max_page": MAX_ANONYMOUS_PAGE},
            )
        return page

    @staticmethod
    def _page_size(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ReachError("invalid_input", "The result limit must be a positive integer.")
        return min(limit, MAX_PAGE_SIZE)

    async def _items(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        delay = self._backoff_until.get(method, 0.0) - time.monotonic()
        if delay > 0:
            await self._sleep(delay)

        payload = await get_json(
            f"{API_ROOT}/{path.lstrip('/')}",
            allowed_hosts=API_HOSTS,
            params=params,
            headers={"Accept-Encoding": "gzip"},
            timeout=self.timeout,
            retries=self.retries,
            assume_gzip=True,
            json_error_statuses=frozenset({400}),
        )
        if not isinstance(payload, dict):
            raise ReachError("parser_error", "Stack Exchange returned an invalid response wrapper.")

        error_id = payload.get("error_id")
        if error_id is not None:
            error_name = payload.get("error_name")
            error_message = payload.get("error_message")
            is_throttle = error_name in {"throttle_violation", "rate_limited"}
            raise ReachError(
                "rate_limited" if is_throttle else "upstream_error",
                str(error_message or error_name or "Stack Exchange API request failed."),
                retryable=is_throttle,
                details={"api_error_id": error_id, "api_error_name": error_name},
            )

        backoff = payload.get("backoff")
        if isinstance(backoff, (int, float)) and not isinstance(backoff, bool) and backoff > 0:
            self._backoff_until[method] = max(
                self._backoff_until.get(method, 0.0), time.monotonic() + float(backoff)
            )

        items = payload.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ReachError("parser_error", "Stack Exchange returned an invalid items array.")
        return items, payload

    @staticmethod
    def _iso_date(value: Any) -> str | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None

    @staticmethod
    def _owner_name(item: dict[str, Any]) -> str | None:
        owner = item.get("owner")
        if isinstance(owner, dict):
            name = owner.get("display_name")
            return name if isinstance(name, str) else None
        return None

    @staticmethod
    def _metrics(item: dict[str, Any], keys: tuple[str, ...]) -> dict[str, int | float | str | None]:
        return {key: item[key] for key in keys if key in item and not isinstance(item[key], bool)}

    @staticmethod
    def _extensions(item: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
        return {key: item[key] for key in keys if key in item}

    def _question_record(self, item: dict[str, Any], site: str, fallback_url: str | None = None) -> Record:
        question_id = item.get("question_id")
        if question_id is None:
            question_id = item.get("post_id")
        if isinstance(question_id, bool) or not isinstance(question_id, int) or question_id < 1:
            raise ReachError("parser_error", "Stack Exchange returned a question without a valid question ID.")
        return Record(
            platform=self.platform,
            type="question",
            id=str(question_id),
            url=item.get("link") if isinstance(item.get("link"), str) else fallback_url,
            author=self._owner_name(item),
            title=item.get("title") if isinstance(item.get("title"), str) else None,
            text=item.get("body") if isinstance(item.get("body"), str) else None,
            created_at=self._iso_date(item.get("creation_date")),
            metrics=self._metrics(item, ("score", "view_count", "answer_count", "comment_count")),
            access=AccessInfo(mode="anonymous", complete="body" in item),
            extensions={
                "site": site,
                **self._extensions(
                    item,
                    (
                        "tags",
                        "accepted_answer_id",
                        "is_answered",
                        "content_license",
                        "site_url",
                        "post_type",
                    ),
                ),
            },
        )

    def _answer_record(self, item: dict[str, Any], site: str) -> Record:
        answer_id = item.get("answer_id")
        if answer_id is None:
            answer_id = item.get("post_id")
        if isinstance(answer_id, bool) or not isinstance(answer_id, int) or answer_id < 1:
            raise ReachError("parser_error", "Stack Exchange returned an answer without a valid answer ID.")
        return Record(
            platform=self.platform,
            type="answer",
            id=str(answer_id),
            url=item.get("link") if isinstance(item.get("link"), str) else None,
            author=self._owner_name(item),
            text=item.get("body") if isinstance(item.get("body"), str) else None,
            created_at=self._iso_date(item.get("creation_date")),
            metrics=self._metrics(item, ("score", "comment_count")),
            access=AccessInfo(mode="anonymous", complete="body" in item),
            extensions={
                "site": site,
                **self._extensions(item, ("question_id", "accepted", "content_license", "post_type")),
            },
        )

    def _comment_record(self, item: dict[str, Any], site: str, fallback_url: str | None = None) -> Record:
        comment_id = item.get("comment_id")
        if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id < 1:
            raise ReachError("parser_error", "Stack Exchange returned a comment without a valid comment ID.")
        return Record(
            platform=self.platform,
            type="comment",
            id=str(comment_id),
            url=item.get("link") if isinstance(item.get("link"), str) else fallback_url,
            author=self._owner_name(item),
            text=item.get("body") if isinstance(item.get("body"), str) else None,
            created_at=self._iso_date(item.get("creation_date")),
            metrics=self._metrics(item, ("score",)),
            access=AccessInfo(mode="anonymous", complete="body" in item),
            extensions={"site": site, **self._extensions(item, ("post_id", "reply_to_user", "content_license"))},
        )

    def _post_record(self, item: dict[str, Any], site: str) -> Record:
        post_type = item.get("post_type")
        record_type = post_type if post_type in {"question", "answer"} else "post"
        post_id = item.get("post_id")
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id < 1:
            raise ReachError("parser_error", "Stack Exchange returned a post without a valid post ID.")
        return Record(
            platform=self.platform,
            type=record_type,
            id=str(post_id),
            url=item.get("link") if isinstance(item.get("link"), str) else None,
            author=self._owner_name(item),
            title=item.get("title") if isinstance(item.get("title"), str) else None,
            text=item.get("body") if isinstance(item.get("body"), str) else None,
            created_at=self._iso_date(item.get("creation_date")),
            metrics=self._metrics(item, ("score", "comment_count")),
            access=AccessInfo(mode="anonymous", complete="body" in item),
            extensions={
                "site": site,
                **self._extensions(item, ("post_type", "question_id", "answer_id", "content_license")),
            },
        )

    @staticmethod
    def _pagination(payload: dict[str, Any], page: int) -> tuple[Pagination, bool, list[str]]:
        has_more = bool(payload.get("has_more"))
        can_continue = has_more and page < MAX_ANONYMOUS_PAGE
        warnings: list[str] = []
        detail = None
        if has_more and not can_continue:
            detail = f"Additional results exist, but anonymous access is capped at page {MAX_ANONYMOUS_PAGE}."
            warnings.append(detail)
        return (
            Pagination(cursor=str(page + 1) if can_continue else None, has_more=can_continue, detail=detail),
            not has_more,
            warnings,
        )

    @staticmethod
    def _search_query_options(options: dict[str, Any]) -> dict[str, Any]:
        unknown = set(options) - _SEARCH_OPTIONS
        if unknown:
            raise ReachError(
                "invalid_input",
                "Unsupported Stack Exchange search option.",
                details={"options": sorted(unknown)},
            )
        params: dict[str, Any] = {}
        for key in _SEARCH_OPTIONS - {"site", "sort", "order"}:
            if key in options:
                value = options[key]
                if key in {"tagged", "nottagged"} and isinstance(value, (list, tuple)):
                    value = ";".join(str(part) for part in value)
                params[key] = value
        return params

    @staticmethod
    def _validate_options(options: dict[str, Any], allowed: set[str] | frozenset[str]) -> None:
        unknown = set(options) - set(allowed)
        if unknown:
            raise ReachError(
                "invalid_input",
                "Unsupported Stack Exchange adapter option.",
                details={"options": sorted(unknown)},
            )

    async def search(
        self,
        query: str,
        limit: int = 10,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        values = self._options(options)
        self._validate_options(values, _SEARCH_OPTIONS)
        if not isinstance(query, str) or not query.strip():
            raise ReachError("invalid_input", "Search query must be a non-empty string.")
        page = self._page(cursor)
        page_size = self._page_size(limit)
        sort = values.get("sort", "relevance")
        order = values.get("order", "desc")
        if not isinstance(sort, str) or sort not in {"activity", "creation", "votes", "relevance"}:
            raise ReachError("invalid_input", "Stack Exchange search sort must be activity, creation, votes, or relevance.")
        if not isinstance(order, str) or order not in {"asc", "desc"}:
            raise ReachError("invalid_input", "Stack Exchange order must be 'asc' or 'desc'.")
        params: dict[str, Any] = {
            "site": self._site(values),
            "q": query,
            "page": page,
            "pagesize": page_size,
            "sort": sort,
            "order": order,
            "filter": "withbody",
            **self._search_query_options(values),
        }
        items, wrapper = await self._items("search/advanced", "search/advanced", params)
        pagination, complete, warnings = self._pagination(wrapper, page)
        site = params["site"]
        return ResultPage(
            platform=self.platform,
            operation="search",
            items=[self._question_record(item, site) for item in items],
            pagination=pagination,
            complete=complete,
            warnings=warnings,
            extensions={key: wrapper[key] for key in ("quota_remaining", "quota_max") if key in wrapper},
        )

    def _question_id_from_url(self, url: str, site: str) -> int:
        if not isinstance(url, str):
            raise ReachError("invalid_input", "Question URL must be a string.")
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise ReachError("invalid_input", "Question URL is malformed.") from exc
        try:
            host = (parsed.hostname or "").lower().removeprefix("www.")
            port = parsed.port
        except ValueError as exc:
            raise ReachError("invalid_input", "Question URL is malformed.") from exc
        if parsed.scheme not in {"https", "http"} or not host or parsed.username or parsed.password or port:
            raise ReachError("invalid_input", "Question URL must be an ordinary Stack Exchange question URL.")
        if not self._host_matches_site(host, site):
            raise ReachError(
                "invalid_input",
                "Question URL does not belong to the selected Stack Exchange site.",
                details={"host": host, "site": site},
            )
        match = _QUESTION_PATH_ID.match(parsed.path.lstrip("/"))
        if not match:
            raise ReachError("invalid_input", "URL must identify a question using /questions/{id} or /q/{id}.")
        return int(match.group(1))

    def _host_matches_site(self, host: str, site: str) -> bool:
        if self.platform == "stackoverflow":
            return host == "stackoverflow.com"
        if "." in site:
            return host == site.removeprefix("www.")
        if host == f"{site}.stackexchange.com":
            return True
        if host == site:
            return True
        return host in {f"{site}.com", f"{site}.net", f"{site}.org"}

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        values = self._options(options)
        self._validate_options(values, {"site"})
        site = self._site(values)
        question_id = self._question_id_from_url(url, site)
        items, _ = await self._items(
            "questions/{ids}",
            f"questions/{question_id}",
            {"site": site, "filter": "withbody"},
        )
        if not items:
            raise ReachError("not_found", "The public Stack Exchange question was not found.", details={"question_id": question_id, "site": site})
        record = self._question_record(items[0], site, fallback_url=url)
        if record.url and record.url != url:
            record.extensions["api_link"] = record.url
        record.url = url
        return record

    async def replies(
        self,
        url: str,
        limit: int = 50,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        values = self._options(options)
        self._validate_options(values, {"site"})
        site = self._site(values)
        question_id = self._question_id_from_url(url, site)
        page = self._page(cursor)
        page_size = self._page_size(limit)
        base_params = {"site": site, "page": page, "pagesize": page_size, "filter": "withbody"}

        answers, answers_wrapper = await self._items(
            "questions/{ids}/answers",
            f"questions/{question_id}/answers",
            dict(base_params),
        )
        question_comments, question_comments_wrapper = await self._items(
            "questions/{ids}/comments",
            f"questions/{question_id}/comments",
            dict(base_params),
        )

        answer_comments: list[dict[str, Any]] = []
        answer_comments_wrapper: dict[str, Any] = {"has_more": False}
        answer_ids = [
            int(item["answer_id"])
            for item in answers
            if isinstance(item.get("answer_id"), int) and not isinstance(item.get("answer_id"), bool)
        ][:MAX_PAGE_SIZE]
        if answer_ids:
            answer_comments, answer_comments_wrapper = await self._items(
                "answers/{ids}/comments",
                f"answers/{';'.join(str(item) for item in answer_ids)}/comments",
                {"site": site, "page": page, "pagesize": page_size, "filter": "withbody"},
            )

        comments_by_answer: dict[int, list[Record]] = {}
        answer_urls = {
            item["answer_id"]: item.get("link")
            for item in answers
            if isinstance(item.get("answer_id"), int) and isinstance(item.get("link"), str)
        }
        for item in answer_comments:
            answer_id = item.get("post_id")
            if isinstance(answer_id, int) and not isinstance(answer_id, bool):
                comments_by_answer.setdefault(answer_id, []).append(
                    self._comment_record(item, site, answer_urls.get(answer_id, url))
                )

        answer_records = [self._answer_record(item, site) for item in answers]
        for answer, raw in zip(answer_records, answers):
            answer_id = raw.get("answer_id")
            if isinstance(answer_id, int):
                answer.replies = comments_by_answer.get(answer_id, [])

        question_comment_records = [self._comment_record(item, site, url) for item in question_comments]
        more_payloads = (answers_wrapper, question_comments_wrapper, answer_comments_wrapper)
        has_more = any(bool(wrapper.get("has_more")) for wrapper in more_payloads)
        can_continue = has_more and page < MAX_ANONYMOUS_PAGE
        warnings: list[str] = []
        detail = None
        if has_more and not can_continue:
            detail = f"Additional replies exist, but anonymous access is capped at page {MAX_ANONYMOUS_PAGE}."
            warnings.append(detail)
        elif has_more:
            warnings.append("Additional answers or comments are available on the next page.")
        return ResultPage(
            platform=self.platform,
            operation="replies",
            items=[*answer_records, *question_comment_records],
            pagination=Pagination(cursor=str(page + 1) if can_continue else None, has_more=can_continue, detail=detail),
            complete=not has_more,
            warnings=warnings,
            extensions={"site": site, "question_id": question_id},
        )

    async def _resolve_user(self, username: str, site: str) -> dict[str, Any]:
        if not isinstance(username, str) or not username.strip():
            raise ReachError("invalid_input", "A Stack Exchange user name or user ID is required.")
        value = username.strip()
        if value.isdecimal():
            user_id = int(value)
            if user_id < 1:
                raise ReachError("invalid_input", "User ID must be a positive integer.")
            path = f"users/{user_id}"
            items, _ = await self._items("users/{ids}", path, {"site": site, "filter": "withbody"})
            if not items:
                raise ReachError("not_found", "The public Stack Exchange user was not found.", details={"user_id": user_id, "site": site})
            return items[0]

        items, _ = await self._items(
            "users",
            "users",
            {"site": site, "inname": value, "page": 1, "pagesize": MAX_PAGE_SIZE, "filter": "withbody"},
        )
        exact = [item for item in items if str(item.get("display_name", "")).casefold() == value.casefold()]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ReachError(
                "ambiguous_user",
                "More than one public user has this display name; use a numeric user ID.",
                details={"site": site, "user_ids": [item.get("user_id") for item in exact]},
            )
        if not items:
            raise ReachError("not_found", "No public Stack Exchange user matched this name.", details={"site": site})
        raise ReachError(
            "not_found",
            "No exact public Stack Exchange display-name match was found; use the user's numeric ID.",
            details={
                "site": site,
                "matches": [
                    {"user_id": item.get("user_id"), "display_name": item.get("display_name")}
                    for item in items[:5]
                ],
            },
        )

    def _user_record(self, item: dict[str, Any], site: str) -> Record:
        user_id = item.get("user_id")
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
            raise ReachError("parser_error", "Stack Exchange returned a profile without a valid user ID.")
        return Record(
            platform=self.platform,
            type="profile",
            id=str(user_id),
            url=item.get("link") if isinstance(item.get("link"), str) else None,
            author=item.get("display_name") if isinstance(item.get("display_name"), str) else None,
            text=item.get("about_me") if isinstance(item.get("about_me"), str) else None,
            created_at=self._iso_date(item.get("creation_date")),
            metrics=self._metrics(item, ("reputation", "question_count", "answer_count", "view_count", "accept_rate")),
            access=AccessInfo(mode="anonymous", complete=True),
            extensions={
                "site": site,
                **self._extensions(
                    item,
                    (
                        "account_id",
                        "user_type",
                        "location",
                        "website_url",
                        "profile_image",
                        "last_access_date",
                        "badge_counts",
                    ),
                ),
            },
        )

    async def profile(self, username: str, options: dict[str, Any] | None = None) -> Record:
        values = self._options(options)
        self._validate_options(values, {"site"})
        site = self._site(values)
        user = await self._resolve_user(username, site)
        return self._user_record(user, site)

    async def profile_posts(
        self,
        username: str,
        limit: int = 20,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        values = self._options(options)
        self._validate_options(values, {"site", "sort", "order"})
        site = self._site(values)
        page = self._page(cursor)
        page_size = self._page_size(limit)
        user = await self._resolve_user(username, site)
        user_id = user.get("user_id")
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise ReachError("parser_error", "Stack Exchange did not return a numeric user ID.")
        sort = values.get("sort", "activity")
        order = values.get("order", "desc")
        if not isinstance(sort, str) or sort not in {"activity", "creation", "votes"}:
            raise ReachError("invalid_input", "User post sort must be activity, creation, or votes.")
        if not isinstance(order, str) or order not in {"asc", "desc"}:
            raise ReachError("invalid_input", "Stack Exchange order must be 'asc' or 'desc'.")
        items, wrapper = await self._items(
            "users/{ids}/posts",
            f"users/{user_id}/posts",
            {
                "site": site,
                "page": page,
                "pagesize": page_size,
                "sort": sort,
                "order": order,
                "filter": "withbody",
            },
        )
        pagination, complete, warnings = self._pagination(wrapper, page)
        return ResultPage(
            platform=self.platform,
            operation="profile_posts",
            items=[self._post_record(item, site) for item in items],
            pagination=pagination,
            complete=complete,
            warnings=warnings,
            extensions={"site": site, "user_id": user_id},
        )

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        del options
        try:
            payload = await get_json(
                f"{API_ROOT}/sites",
                allowed_hosts=API_HOSTS,
                params={"page": 1, "pagesize": 1},
                headers={"Accept-Encoding": "gzip"},
                timeout=5.0,
                retries=0,
                assume_gzip=True,
                json_error_statuses=frozenset({400}),
            )
            if not isinstance(payload, dict):
                raise ReachError("parser_error", "Stack Exchange returned an invalid sites response.")
            if payload.get("error_id") is not None:
                error_name = payload.get("error_name")
                raise ReachError(
                    "rate_limited" if error_name == "throttle_violation" else "upstream_error",
                    "Stack Exchange API returned an error during its health check.",
                    retryable=error_name == "throttle_violation",
                )
            if not isinstance(payload.get("items"), list):
                raise ReachError("parser_error", "Stack Exchange returned an invalid sites response.")
            return AdapterStatus(
                platform=self.platform,
                status="available",
                message="Stack Exchange API responded to a live check.",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )
        except ReachError as exc:
            return AdapterStatus(
                platform=self.platform,
                status="degraded" if exc.code == "rate_limited" else "unavailable",
                message=f"Stack Exchange health check failed ({exc.code}).",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )
