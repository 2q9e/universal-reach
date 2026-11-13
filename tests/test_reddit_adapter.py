"""Deterministic mocked tests for Reddit's approved OAuth Data API adapter."""

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from universal_reach.adapters import reddit
from universal_reach.adapters.reddit import RedditAdapter
from universal_reach.errors import ReachError


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def listing(children: list[dict[str, Any]], after: str | None = None) -> dict[str, Any]:
    return {"kind": "Listing", "data": {"children": children, "after": after, "before": None}}


def post_data(
    post_id: str = "abc123",
    *,
    title: str = "A post",
    comments: int = 1,
    subreddit: str = "python",
) -> dict[str, Any]:
    return {
        "id": post_id,
        "name": f"t3_{post_id}",
        "title": title,
        "selftext": "body text",
        "author": "alice",
        "subreddit": subreddit,
        "permalink": f"/r/{subreddit}/comments/{post_id}/a_post/",
        "created_utc": 1_700_000_000,
        "score": 42,
        "num_comments": comments,
        "upvote_ratio": 0.9,
    }


def comment_node(comment_id: str = "def456", body: str = "A comment") -> dict[str, Any]:
    return {
        "kind": "t1",
        "data": {
            "id": comment_id,
            "name": f"t1_{comment_id}",
            "body": body,
            "author": "bob",
            "permalink": f"/r/python/comments/abc123/a_post/{comment_id}/",
            "created_utc": 1_700_000_001,
            "score": 3,
            "replies": "",
        },
    }


def comments_payload(
    post: Mapping[str, Any],
    comments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        listing([{"kind": "t3", "data": dict(post)}]),
        listing(comments),
    ]


def install_get(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[tuple[str, dict[str, Any], dict[str, str]]]:
    calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def fake_get_json(
        url: str,
        *,
        allowed_hosts: frozenset[str],
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        assert allowed_hosts == frozenset({"oauth.reddit.com"})
        calls.append((url, dict(params or {}), dict(headers or {})))
        return handler(url, dict(params or {}), dict(headers or {}))

    monkeypatch.setattr(reddit, "get_json", fake_get_json)
    return calls


def configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDDIT_API_APPROVED", "true")
    monkeypatch.setenv("REDDIT_ACCESS_TOKEN", "secret-test-token")
    monkeypatch.setenv("REDDIT_CLIENT_ID", "registered-app")
    monkeypatch.setenv("REDDIT_USERNAME", "alice")


def test_does_not_call_reddit_without_explicit_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDDIT_API_APPROVED", raising=False)
    monkeypatch.setenv("REDDIT_ACCESS_TOKEN", "secret-test-token")
    calls = install_get(monkeypatch, lambda *args: pytest.fail("no request should be made"))

    with pytest.raises(ReachError) as error:
        run(RedditAdapter().search("hello"))

    assert error.value.code == "reddit_approval_required"
    assert "secret-test-token" not in str(error.value)
    assert calls == []


def test_search_uses_approved_oauth_and_maps_listing_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)

    def handler(url: str, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        assert url == "https://oauth.reddit.com/search"
        assert params["q"] == "cats"
        assert params["limit"] == 1
        assert params["after"] == "t3_before"
        assert params["type"] == "link"
        assert headers["Authorization"] == "Bearer secret-test-token"
        assert headers["User-Agent"] == "web:registered-app:0.1.0 (by /u/alice)"
        return listing([{"kind": "t3", "data": post_data()}], after="t3_next")

    calls = install_get(monkeypatch, handler)
    result = run(RedditAdapter().search("cats", limit=1, cursor="t3_before"))

    assert calls[0][0].startswith("https://oauth.reddit.com/")
    assert result.complete is False
    assert result.pagination.cursor == "t3_next"
    assert result.pagination.has_more is True
    assert result.items[0].type == "post"
    assert result.items[0].access.mode == "authenticated"
    assert "secret-test-token" not in repr(result.to_dict())


def test_read_maps_post_and_nested_comment_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)
    comment = comment_node()

    def handler(url: str, params: dict[str, Any], headers: dict[str, str]) -> list[dict[str, Any]]:
        assert url == "https://oauth.reddit.com/comments/abc123"
        assert params["depth"] == 5
        return comments_payload(post_data(), [comment])

    install_get(monkeypatch, handler)
    record = run(RedditAdapter().read("https://www.reddit.com/r/python/comments/abc123/a_post/"))

    assert record.id == "t3_abc123"
    assert record.title == "A post"
    assert record.text == "body text"
    assert record.url == "https://www.reddit.com/r/python/comments/abc123/a_post/"
    assert record.replies[0].id == "t1_def456"
    assert record.replies[0].access.mode == "authenticated"


def test_replies_mark_morecomments_tree_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)
    more = {"kind": "more", "data": {"count": 2, "children": ["ghi789"]}}

    def handler(url: str, params: dict[str, Any], headers: dict[str, str]) -> list[dict[str, Any]]:
        assert params["limit"] == 10
        assert params["showmore"] is True
        return comments_payload(post_data(comments=4), [comment_node(), more])

    install_get(monkeypatch, handler)
    result = run(RedditAdapter().replies("https://redd.it/abc123", limit=10))

    assert result.complete is False
    assert result.pagination.has_more is True
    assert result.pagination.cursor is None
    assert len(result.items) == 1
    assert result.items[0].type == "comment"
    assert result.warnings


def test_profile_and_profile_posts_use_official_read_routes_and_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)
    responses = [
        {"kind": "t2", "data": {"id": "prof123", "name": "alice", "link_karma": 10, "comment_karma": 20, "created_utc": 1_700_000_000}},
        listing([{"kind": "t3", "data": post_data()}], after="t3_next"),
    ]

    def handler(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        if url.endswith("/user/alice/about"):
            return responses.pop(0)
        assert url == "https://oauth.reddit.com/user/alice/submitted"
        assert params["after"] == "t3_before"
        return responses.pop(0)

    calls = install_get(monkeypatch, handler)
    adapter = RedditAdapter()
    profile = run(adapter.profile("u/alice"))
    posts = run(adapter.profile_posts("alice", limit=1, cursor="t3_before"))

    assert profile.type == "profile"
    assert profile.id == "t2_prof123"
    assert profile.metrics["total_karma"] == 30
    assert posts.pagination.cursor == "t3_next"
    assert posts.items[0].id == "t3_abc123"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/r/python/comments/abc123/title",
        "http://www.reddit.com/r/python/comments/abc123/title",
        "https://www.reddit.com/r/python/comments/%2fetc/title",
        "https://www.reddit.com/r/python/comments/abc123/title?redirect=evil",
        "https://user@www.reddit.com/r/python/comments/abc123/title",
        "https://www.reddit.com/r/python/about",
    ],
)
def test_post_url_parser_rejects_external_or_malformed_urls(url: str) -> None:
    with pytest.raises(ReachError):
        run(RedditAdapter().read(url))


def test_api_auth_errors_are_structured_and_do_not_echo_token(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise ReachError("private_or_inaccessible", "denied", details={"http_status": 403})

    monkeypatch.setattr(reddit, "get_json", forbidden)
    with pytest.raises(ReachError) as error:
        run(RedditAdapter().search("hello"))

    assert error.value.code == "reddit_access_denied"
    assert error.value.details["http_status"] == 403
    assert "secret-test-token" not in str(error.value)


def test_rate_limit_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)

    async def throttled(*args: Any, **kwargs: Any) -> Any:
        raise ReachError("rate_limited", "slow down", retryable=True, details={"http_status": 429})

    monkeypatch.setattr(reddit, "get_json", throttled)
    with pytest.raises(ReachError) as error:
        run(RedditAdapter().search("hello"))

    assert error.value.code == "rate_limited"
    assert error.value.retryable is True
    assert error.value.details["http_status"] == 429
