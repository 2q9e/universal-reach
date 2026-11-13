"""Deterministic tests for the anonymous Bluesky adapter."""

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from universal_reach.adapters import bluesky
from universal_reach.adapters.bluesky import BlueskyAdapter
from universal_reach.errors import ReachError


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def profile_payload(actor: str = "alice.bsky.social") -> dict[str, Any]:
    return {
        "did": "did:plc:ewvi7nxzyoun6zhxrhs64oiz",
        "handle": actor,
        "displayName": "Alice",
        "description": "Public profile",
        "followersCount": 12,
        "followsCount": 7,
        "postsCount": 4,
    }


def post_view(
    uri: str = "at://did:plc:ewvi7nxzyoun6zhxrhs64oiz/app.bsky.feed.post/3k2abc",
    *,
    text: str = "hello",
    replies: int = 0,
) -> dict[str, Any]:
    return {
        "uri": uri,
        "cid": "bafyreid",
        "author": {
            "did": "did:plc:ewvi7nxzyoun6zhxrhs64oiz",
            "handle": "alice.bsky.social",
            "displayName": "Alice",
        },
        "record": {"text": text, "createdAt": "2026-11-13T12:00:00.000Z"},
        "replyCount": replies,
        "likeCount": 3,
        "repostCount": 2,
        "quoteCount": 1,
    }


def thread_view(post: Mapping[str, Any], children: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "thread": {
            "$type": "app.bsky.feed.defs#threadViewPost",
            "post": dict(post),
            "replies": children or [],
        }
    }


def install_get(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[tuple[str, dict[str, Any], frozenset[str]]]:
    calls: list[tuple[str, dict[str, Any], frozenset[str]]] = []

    async def fake_get_json(
        url: str,
        *,
        allowed_hosts: frozenset[str],
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        calls.append((url, dict(params or {}), allowed_hosts))
        return handler(url, dict(params or {}))

    monkeypatch.setattr(bluesky, "get_json", fake_get_json)
    return calls


def test_adapter_advertises_public_read_operations() -> None:
    adapter = BlueskyAdapter()
    assert adapter.platform == "bluesky"
    assert adapter.capability.operations == ("search", "read", "replies", "profile", "profile_posts")
    assert adapter.capability.anonymous_access == "yes"


def test_search_uses_public_appview_and_maps_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(url: str, params: dict[str, Any]) -> dict[str, Any]:
        assert url.endswith("/app.bsky.feed.searchPosts")
        assert params["q"] == "hello world"
        assert params["limit"] == 1
        assert params["cursor"] == "previous-token"
        return {"posts": [post_view()], "cursor": "next-token"}

    calls = install_get(monkeypatch, handler)
    result = run(BlueskyAdapter().search("hello world", limit=1, cursor="previous-token"))

    assert calls[0][0].startswith("https://public.api.bsky.app/xrpc/")
    assert calls[0][2] == frozenset({"public.api.bsky.app"})
    assert result.operation == "search"
    assert result.complete is False
    assert result.pagination.has_more is True
    assert result.pagination.cursor == "next-token"
    assert result.items[0].id.startswith("at://")
    assert result.items[0].url == "https://bsky.app/profile/alice.bsky.social/post/3k2abc"
    assert result.items[0].text == "hello"
    assert result.items[0].access.mode == "anonymous"


def test_read_resolves_handle_then_fetches_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    root = post_view(replies=1)
    child = {
        "$type": "app.bsky.feed.defs#threadViewPost",
        "post": post_view(
            "at://did:plc:ewvi7nxzyoun6zhxrhs64oiz/app.bsky.feed.post/3k2reply",
            text="reply",
        ),
    }

    def handler(url: str, params: dict[str, Any]) -> dict[str, Any]:
        if url.endswith("/app.bsky.actor.getProfile"):
            return profile_payload()
        assert url.endswith("/app.bsky.feed.getPostThread")
        assert params["uri"] == "at://did:plc:ewvi7nxzyoun6zhxrhs64oiz/app.bsky.feed.post/3k2abc"
        assert params["parentHeight"] == 0
        return thread_view(root, [child])

    install_get(monkeypatch, handler)
    record = run(BlueskyAdapter().read("https://bsky.app/profile/alice.bsky.social/post/3k2abc"))

    assert record.id == root["uri"]
    assert record.replies[0].text == "reply"


def test_replies_reports_thread_partial_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    root = post_view(replies=3)
    children = [
        {
            "$type": "app.bsky.feed.defs#threadViewPost",
            "post": post_view(
                "at://did:plc:ewvi7nxzyoun6zhxrhs64oiz/app.bsky.feed.post/3k2reply",
                text="reply",
            ),
        }
    ]

    def handler(url: str, params: dict[str, Any]) -> dict[str, Any]:
        if url.endswith("/app.bsky.actor.getProfile"):
            return profile_payload()
        assert params["depth"] == 6
        return thread_view(root, children)

    install_get(monkeypatch, handler)
    result = run(BlueskyAdapter().replies("https://bsky.social/profile/alice.bsky.social/post/3k2abc"))

    assert result.operation == "replies"
    assert result.complete is False
    assert result.pagination.cursor is None
    assert result.pagination.has_more is True
    assert "no continuation cursor" in (result.pagination.detail or "")
    assert result.items[0].text == "reply"
    assert result.warnings


def test_profile_and_profile_posts_use_did_and_page_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        profile_payload(),
        profile_payload(),
        {"feed": [{"post": post_view()}], "cursor": "next"},
    ]

    def handler(url: str, params: dict[str, Any]) -> dict[str, Any]:
        if url.endswith("/app.bsky.actor.getProfile"):
            return responses.pop(0)
        assert url.endswith("/app.bsky.feed.getAuthorFeed")
        assert params["actor"] == "did:plc:ewvi7nxzyoun6zhxrhs64oiz"
        assert params["filter"] == "posts_no_replies"
        return responses.pop(0)

    calls = install_get(monkeypatch, handler)
    adapter = BlueskyAdapter()
    profile = run(adapter.profile("@alice.bsky.social"))
    posts = run(adapter.profile_posts("https://bsky.app/profile/alice.bsky.social", limit=1))

    assert profile.type == "profile"
    assert profile.author == "@alice.bsky.social"
    assert profile.metrics["followers_count"] == 12
    assert posts.pagination.cursor == "next"
    assert posts.pagination.has_more is True
    assert len(calls) == 3


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/profile/alice.bsky.social/post/3k2abc",
        "http://bsky.app/profile/alice.bsky.social/post/3k2abc",
        "https://bsky.app/profile/alice.bsky.social/post/%2fetc",
        "https://bsky.app/profile/alice.bsky.social/post/3k2abc?redirect=evil",
        "https://user@bsky.app/profile/alice.bsky.social/post/3k2abc",
        "https://bsky.app/profile/alice.bsky.social/likes",
    ],
)
def test_post_url_parser_rejects_untrusted_or_malformed_urls(url: str) -> None:
    with pytest.raises(ReachError):
        run(BlueskyAdapter().read(url))


def test_profile_input_rejects_non_bluesky_url() -> None:
    with pytest.raises(ReachError):
        run(BlueskyAdapter().profile("https://evil.example/profile/alice.bsky.social"))


def test_public_403_and_rate_limit_have_clear_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise ReachError("private_or_inaccessible", "denied", details={"http_status": 403})

    monkeypatch.setattr(bluesky, "get_json", forbidden)
    with pytest.raises(ReachError) as denied:
        run(BlueskyAdapter().search("hello"))
    assert denied.value.code == "public_access_denied"
    assert denied.value.details["http_status"] == 403

    async def throttled(*args: Any, **kwargs: Any) -> Any:
        raise ReachError("rate_limited", "slow down", retryable=True, details={"http_status": 429})

    monkeypatch.setattr(bluesky, "get_json", throttled)
    with pytest.raises(ReachError) as limited:
        run(BlueskyAdapter().search("hello"))
    assert limited.value.code == "rate_limited"
    assert limited.value.retryable is True
    assert limited.value.details["http_status"] == 429
