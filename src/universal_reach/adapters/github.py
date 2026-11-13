"""Public, keyless GitHub REST API integration."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_json
from universal_reach.models import AccessInfo, AdapterStatus, Pagination, Record, ResultPage

API_ROOT = "https://api.github.com"
API_HOSTS = frozenset({"api.github.com"})
URL_HOSTS = frozenset({"github.com", "api.github.com"})
API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
OPERATIONS = ("search", "read", "replies", "profile", "profile_posts")
_MAX_PAGE_SIZE = 100
_SEARCH_RESULT_WINDOW = 1000
_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")


class GitHubAdapter(Adapter):
    """Retrieve public repositories, issues, comments, users, and user repos."""

    def __init__(self) -> None:
        super().__init__(adapter_capability("github", OPERATIONS))

    async def search(
        self,
        query: str,
        limit: int = 10,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        query = query.strip() if isinstance(query, str) else ""
        if not query:
            raise ReachError("bad_input", "A non-empty repository search query is required.")
        page = _page_number(cursor)
        limit = _limit(limit)
        max_page = (_SEARCH_RESULT_WINDOW + limit - 1) // limit
        if page > max_page:
            raise ReachError(
                "bad_input",
                f"GitHub repository search exposes at most {_SEARCH_RESULT_WINDOW} results.",
                details={"maximum_page": max_page, "limit": limit},
            )

        payload = await self._request(
            f"{API_ROOT}/search/repositories",
            params={"q": query, "per_page": limit, "page": page},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ReachError("parser_error", "GitHub returned an invalid repository search response.")

        total = payload.get("total_count")
        total = total if isinstance(total, int) and not isinstance(total, bool) else None
        api_incomplete = payload.get("incomplete_results") is True
        items = [_repository_record(item) for item in _expect_object_list(payload["items"], "search")]
        has_more = (
            page * limit < min(total, _SEARCH_RESULT_WINDOW)
            if total is not None
            else len(payload["items"]) >= limit
        )
        capped = total is not None and total > _SEARCH_RESULT_WINDOW and not has_more
        warnings: list[str] = []
        detail = None
        if api_incomplete:
            warnings.append("GitHub marked this repository search as incomplete.")
        if capped:
            detail = f"GitHub search exposes only the first {_SEARCH_RESULT_WINDOW} matching repositories."
            warnings.append(detail)
        next_cursor = str(page + 1) if has_more else None
        return ResultPage(
            platform="github",
            operation="search",
            items=items,
            pagination=Pagination(cursor=next_cursor, has_more=has_more, detail=detail),
            complete=not has_more and not api_incomplete and not capped,
            warnings=warnings,
            extensions={"total_count": total, "incomplete_results": api_incomplete},
        )

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        target = _parse_github_url(url)
        owner = quote(target["owner"], safe="")
        repo = quote(target["repo"], safe="")
        if target["kind"] == "repository":
            payload = await self._request(f"{API_ROOT}/repos/{owner}/{repo}")
            return _repository_record(_expect_object(payload, "repository"))
        issue_number = target["number"]
        payload = await self._request(f"{API_ROOT}/repos/{owner}/{repo}/issues/{issue_number}")
        return _issue_record(_expect_object(payload, "issue"))

    async def replies(
        self,
        url: str,
        limit: int = 50,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        target = _parse_github_url(url)
        if target["kind"] != "issue":
            raise ReachError("bad_input", "GitHub replies require a public issue URL.")
        limit = _limit(limit)
        page = _page_number(cursor)
        owner = quote(target["owner"], safe="")
        repo = quote(target["repo"], safe="")
        payload = await self._request(
            f"{API_ROOT}/repos/{owner}/{repo}/issues/{target['number']}/comments",
            params={"per_page": limit, "page": page},
        )
        if not isinstance(payload, list):
            raise ReachError("parser_error", "GitHub returned an invalid issue comments response.")
        items = [_comment_record(item) for item in _expect_object_list(payload, "issue comments")]
        has_more = len(payload) >= limit
        return ResultPage(
            platform="github",
            operation="replies",
            items=items,
            pagination=Pagination(cursor=str(page + 1) if has_more else None, has_more=has_more),
            complete=not has_more,
        )

    async def profile(self, username: str, options: dict[str, Any] | None = None) -> Record:
        username = _validate_owner(username)
        payload = await self._request(f"{API_ROOT}/users/{quote(username, safe='')}")
        data = _expect_object(payload, "user")
        user_id = data.get("id") or data.get("node_id")
        if user_id is None:
            raise ReachError("parser_error", "GitHub returned a public user without an identifier.")
        login = _string_or_none(data.get("login"))
        return Record(
            platform="github",
            type="profile",
            id=str(user_id),
            url=_string_or_none(data.get("html_url")),
            author=login,
            title=_string_or_none(data.get("name")) or login,
            text=_string_or_none(data.get("bio")),
            created_at=_string_or_none(data.get("created_at")),
            metrics={
                "followers": _number_or_none(data.get("followers")),
                "following": _number_or_none(data.get("following")),
                "public_repositories": _number_or_none(data.get("public_repos")),
                "public_gists": _number_or_none(data.get("public_gists")),
            },
            access=AccessInfo(mode="anonymous", complete=True),
            extensions={
                key: data.get(key)
                for key in ("company", "location", "blog", "twitter_username", "hireable", "updated_at")
                if data.get(key) is not None
            },
        )

    async def profile_posts(
        self,
        username: str,
        limit: int = 20,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        username = _validate_owner(username)
        limit = _limit(limit)
        page = _page_number(cursor)
        payload = await self._request(
            f"{API_ROOT}/users/{quote(username, safe='')}/repos",
            params={"per_page": limit, "page": page, "sort": "updated", "type": "owner"},
        )
        if not isinstance(payload, list):
            raise ReachError("parser_error", "GitHub returned an invalid user repositories response.")
        items = [_repository_record(item) for item in _expect_object_list(payload, "user repositories")]
        has_more = len(payload) >= limit
        return ResultPage(
            platform="github",
            operation="profile_posts",
            items=items,
            pagination=Pagination(cursor=str(page + 1) if has_more else None, has_more=has_more),
            complete=not has_more,
        )

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        del options
        try:
            payload = await self._request(f"{API_ROOT}/rate_limit", timeout=5.0, retries=0)
            if not isinstance(payload, dict) or not isinstance(payload.get("resources"), dict):
                raise ReachError("parser_error", "GitHub returned an invalid rate-limit status response.")
            return AdapterStatus(
                platform=self.platform,
                status="available",
                message="GitHub REST API responded to a live check.",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )
        except ReachError as exc:
            return AdapterStatus(
                platform=self.platform,
                status="degraded" if exc.code == "rate_limited" else "unavailable",
                message=f"GitHub health check failed ({exc.code}).",
                operations=list(self.capability.operations),
                access_mode="anonymous",
            )

    async def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float = 12.0,
        retries: int = 1,
    ) -> Any:
        try:
            return await get_json(
                url,
                allowed_hosts=API_HOSTS,
                params=params,
                headers=API_HEADERS,
                timeout=timeout,
                retries=retries,
            )
        except ReachError as exc:
            # The shared HTTP layer cannot see GitHub's error body. For anonymous
            # GitHub requests, HTTP 403 commonly represents rate or abuse limits.
            if exc.code == "private_or_inaccessible" and exc.details.get("http_status") == 403:
                raise ReachError(
                    "rate_limited",
                    "GitHub rejected this anonymous request with HTTP 403. Rate limiting or abuse protection may be responsible; retry later.",
                    retryable=True,
                    details={"http_status": 403, "reason": "rate_limit_or_access_policy"},
                ) from exc
            raise


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_PAGE_SIZE:
        raise ReachError(
            "bad_input",
            f"Result limit must be an integer between 1 and {_MAX_PAGE_SIZE}.",
            details={"maximum": _MAX_PAGE_SIZE},
        )
    return value


def _page_number(cursor: str | None) -> int:
    if cursor is None:
        return 1
    if not isinstance(cursor, str) or len(cursor) > 6 or not cursor.isascii() or not cursor.isdigit() or int(cursor) < 1:
        raise ReachError("bad_input", "GitHub pagination cursor must be a positive page number.")
    return int(cursor)


def _validate_owner(username: str) -> str:
    if not isinstance(username, str):
        raise ReachError("bad_input", "A GitHub username is required.")
    username = username.strip()
    if not _OWNER_RE.fullmatch(username):
        raise ReachError("bad_input", "GitHub username contains unsupported characters.")
    return username


def _parse_github_url(url: str) -> dict[str, str]:
    if not isinstance(url, str) or not url.strip():
        raise ReachError("bad_input", "A GitHub repository or issue URL is required.")
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").casefold()
        port = parts.port
    except ValueError as exc:
        raise ReachError("bad_input", "The GitHub URL is malformed.") from exc
    if (
        parts.scheme.casefold() != "https"
        or host not in URL_HOSTS
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
    ):
        raise ReachError("bad_input", "Only HTTPS github.com and api.github.com URLs are accepted.")

    raw_segments = [segment for segment in parts.path.split("/") if segment]
    segments = [unquote(segment) for segment in raw_segments]
    if host == "api.github.com":
        if len(segments) < 3 or segments[0] != "repos":
            raise ReachError("bad_input", "Only public GitHub repository and issue API URLs are supported.")
        segments = segments[1:]
    if len(segments) not in (2, 4):
        raise ReachError("bad_input", "Only a repository URL or an issue URL is supported.")
    owner, repo = segments[:2]
    if not _OWNER_RE.fullmatch(owner) or not _REPO_RE.fullmatch(repo) or repo in (".", ".."):
        raise ReachError("bad_input", "The GitHub repository URL has an invalid owner or repository.")
    if len(segments) == 2:
        return {"kind": "repository", "owner": owner, "repo": repo}
    if (
        segments[2] != "issues"
        or not segments[3].isascii()
        or not segments[3].isdigit()
        or len(segments[3]) > 10
        or int(segments[3]) < 1
    ):
        raise ReachError("bad_input", "Only numbered GitHub issue URLs are supported.")
    return {"kind": "issue", "owner": owner, "repo": repo, "number": str(int(segments[3]))}


def _expect_object(value: Any, kind: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReachError("parser_error", f"GitHub returned an invalid {kind} response.")
    return value


def _expect_object_list(value: Any, kind: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ReachError("parser_error", f"GitHub returned an invalid {kind} response.")
    return value


def _repository_record(data: dict[str, Any]) -> Record:
    repo_id = data.get("id") or data.get("node_id")
    full_name = _string_or_none(data.get("full_name"))
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    author = _string_or_none(owner.get("login"))
    if repo_id is None or full_name is None:
        raise ReachError("parser_error", "GitHub returned a repository without an identifier or name.")
    license_data = data.get("license") if isinstance(data.get("license"), dict) else {}
    return Record(
        platform="github",
        type="repository",
        id=str(repo_id),
        url=_string_or_none(data.get("html_url")),
        author=author,
        title=full_name,
        text=_string_or_none(data.get("description")),
        created_at=_string_or_none(data.get("created_at")),
        metrics={
            "stars": _number_or_none(data.get("stargazers_count")),
            "forks": _number_or_none(data.get("forks_count")),
            "open_issues": _number_or_none(data.get("open_issues_count")),
        },
        access=AccessInfo(mode="anonymous", complete=True),
        extensions={
            key: data.get(key)
            for key in ("default_branch", "language", "updated_at", "archived", "topics")
            if data.get(key) is not None
        }
        | {"license": _string_or_none(license_data.get("spdx_id"))},
    )


def _issue_record(data: dict[str, Any]) -> Record:
    issue_id = data.get("id") or data.get("node_id")
    number = data.get("number")
    if issue_id is None or number is None:
        raise ReachError("parser_error", "GitHub returned an issue without an identifier.")
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    reactions = data.get("reactions") if isinstance(data.get("reactions"), dict) else {}
    labels = data.get("labels") if isinstance(data.get("labels"), list) else []
    record_type = "pull_request" if isinstance(data.get("pull_request"), dict) else "issue"
    return Record(
        platform="github",
        type=record_type,
        id=str(issue_id),
        url=_string_or_none(data.get("html_url")),
        author=_string_or_none(user.get("login")),
        title=_string_or_none(data.get("title")),
        text=_string_or_none(data.get("body")),
        created_at=_string_or_none(data.get("created_at")),
        metrics={
            "comments": _number_or_none(data.get("comments")),
            "reactions": _number_or_none(reactions.get("total_count")),
        },
        access=AccessInfo(mode="anonymous", complete=True),
        extensions={
            "number": number,
            "state": data.get("state"),
            "updated_at": data.get("updated_at"),
            "closed_at": data.get("closed_at"),
            "labels": [
                label.get("name")
                for label in labels
                if isinstance(label, dict) and isinstance(label.get("name"), str)
            ],
            "repository_url": data.get("repository_url"),
        },
    )


def _comment_record(data: dict[str, Any]) -> Record:
    comment_id = data.get("id") or data.get("node_id")
    if comment_id is None:
        raise ReachError("parser_error", "GitHub returned a comment without an identifier.")
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    return Record(
        platform="github",
        type="comment",
        id=str(comment_id),
        url=_string_or_none(data.get("html_url")),
        author=_string_or_none(user.get("login")),
        text=_string_or_none(data.get("body")),
        created_at=_string_or_none(data.get("created_at")),
        access=AccessInfo(mode="anonymous", complete=True),
        extensions={"updated_at": data.get("updated_at")},
    )


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _number_or_none(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
