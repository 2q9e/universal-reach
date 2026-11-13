from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import pytest

from universal_reach.adapters import public_forums
from universal_reach.adapters.public_forums import DiscourseAdapter, FourChanAdapter, LemmyAdapter
from universal_reach.errors import ReachError, UnsupportedOperation
from universal_reach import security


@pytest.fixture(autouse=True)
def reset_fourchan_rate_state() -> None:
    public_forums._FOURCHAN_LAST_REQUEST_AT = None
    public_forums._FOURCHAN_THREAD_REQUESTS.clear()


def allow_test_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    def validate(url: str) -> tuple[str, frozenset[str]]:
        base = url.strip().rstrip("/")
        host = (urlsplit(base).hostname or "").lower()
        return base, frozenset({host})

    monkeypatch.setattr(public_forums, "validate_public_instance_url", validate)


@pytest.mark.asyncio
async def test_fourchan_catalog_search_is_board_scoped_and_discloses_source(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((url, kwargs))
        return [{"threads": [
            {"no": 10, "sub": "Coffee talk", "com": "A <b>public</b> catalog preview", "replies": 4, "time": 1700000000},
            {"no": 11, "sub": "Music", "com": "No match"},
        ]}]

    monkeypatch.setattr(public_forums, "get_json", fake_get_json)
    page = await FourChanAdapter().search("public", options={"board": "g"})

    assert len(calls) == 1
    assert calls[0][0] == "https://a.4cdn.org/g/catalog.json"
    assert calls[0][1]["allowed_hosts"] == public_forums.FOURCHAN_API_HOSTS
    assert calls[0][1]["retries"] == 0
    assert [record.id for record in page.items] == ["10"]
    assert page.items[0].text == "A public catalog preview"
    assert page.items[0].extensions["attribution"] == "Source: 4chan"
    assert page.items[0].extensions["source_url"] == "https://boards.4chan.org/g/thread/10"
    assert page.complete is False
    assert "no global full-text search" in page.warnings[0]


@pytest.mark.asyncio
async def test_fourchan_thread_read_normalizes_and_attributes_public_source(monkeypatch: pytest.MonkeyPatch) -> None:
    received: dict[str, Any] = {}

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        received.update(url=url, **kwargs)
        return {"posts": [{"no": 123, "name": "Anonymous", "sub": "A topic", "com": "<p>Hello &amp; welcome</p>", "time": 1700000000, "replies": 1}]}

    monkeypatch.setattr(public_forums, "get_json", fake_get_json)
    record = await FourChanAdapter().read(
        "https://boards.4channel.org/g/thread/123/example#p123",
        {"if_modified_since": "Wed, 21 Oct 2015 07:28:00 GMT"},
    )

    assert received["url"] == "https://a.4cdn.org/g/thread/123.json"
    assert received["headers"] == {"If-Modified-Since": "Wed, 21 Oct 2015 07:28:00 GMT"}
    assert record.type == "thread"
    assert record.id == "123"
    assert record.url.endswith("#p123")
    assert record.title == "A topic"
    assert record.text == "Hello & welcome"
    assert record.created_at == "2023-11-14T22:13:20Z"
    assert record.extensions["attribution"] == "Source: 4chan"
    assert record.access.mode == "anonymous"


@pytest.mark.asyncio
async def test_fourchan_replies_are_normalized_and_paginated(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        return {"posts": [
            {"no": 50, "sub": "Topic", "com": "OP"},
            {"no": 51, "name": "Guest", "com": "<b>first</b> reply"},
            {"no": 52, "com": "second reply"},
        ]}

    monkeypatch.setattr(public_forums, "get_json", fake_get_json)
    page = await FourChanAdapter().replies("https://boards.4chan.org/g/thread/50", limit=1)

    assert [record.id for record in page.items] == ["51"]
    assert page.items[0].text == "first reply"
    assert page.items[0].author == "Guest"
    assert page.items[0].url.endswith("#p51")
    assert page.pagination.cursor == "1"
    assert page.pagination.has_more is True
    assert page.complete is False


@pytest.mark.asyncio
async def test_fourchan_api_enforces_global_and_thread_refresh_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(public_forums.time, "monotonic", lambda: 100.5)
    public_forums._FOURCHAN_LAST_REQUEST_AT = 100.0
    adapter = FourChanAdapter()
    with pytest.raises(ReachError) as global_limit:
        adapter._reserve_request("g/201")
    assert global_limit.value.code == "rate_limited"
    assert global_limit.value.details["retry_after_seconds"] == 0.5

    public_forums._FOURCHAN_LAST_REQUEST_AT = 99.0
    public_forums._FOURCHAN_THREAD_REQUESTS["g/201"] = 95.0
    with pytest.raises(ReachError) as thread_limit:
        adapter._reserve_request("g/201")
    assert thread_limit.value.code == "rate_limited"
    assert thread_limit.value.details["retry_after_seconds"] == 4.5


@pytest.mark.asyncio
async def test_fourchan_rejects_bad_urls_and_requires_board_option(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("invalid input must not reach the network")

    monkeypatch.setattr(public_forums, "get_json", fail_if_called)
    with pytest.raises(ReachError) as bad_board:
        await FourChanAdapter().search("term", options={"board": "../etc"})
    assert bad_board.value.code == "invalid_input"
    with pytest.raises(ReachError) as bad_url:
        await FourChanAdapter().read("https://example.org/g/thread/123")
    assert bad_url.value.code == "invalid_url"


@pytest.mark.asyncio
async def test_lemmy_search_uses_exact_host_allowlist_and_normalizes_public_views(monkeypatch: pytest.MonkeyPatch) -> None:
    allow_test_instances(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((url, kwargs))
        return {
            "posts": [{
                "post": {"id": 7, "name": "Public question", "body": "Body", "published_at": "2025-01-02T03:04:05Z", "url": "https://example.org/story"},
                "creator": {"name": "reader"},
                "community": {"name": "general"},
                "counts": {"score": 8, "comments": 2},
            }],
            "comments": [{"comment": {"id": 8, "content": "Comment", "published_at": "2025-01-03T00:00:00Z", "post_id": 7}, "creator": {"name": "reply"}}],
            "next_page": "opaque-cursor",
        }

    monkeypatch.setattr(public_forums, "get_json", fake_get_json)
    page = await LemmyAdapter().search("question", limit=10, cursor="older-cursor", options={"instance_url": "https://lemmy.example/"})

    assert calls[0][0] == "https://lemmy.example/api/v4/search"
    assert calls[0][1]["allowed_hosts"] == frozenset({"lemmy.example"})
    assert calls[0][1]["params"]["page_cursor"] == "older-cursor"
    assert calls[0][1]["params"]["type_"] == "all"
    assert [record.type for record in page.items] == ["post", "comment"]
    assert page.items[0].author == "reader"
    assert page.items[0].extensions["external_url"] == "https://example.org/story"
    assert page.items[1].text == "Comment"
    assert page.pagination.cursor == "opaque-cursor"
    assert page.complete is False


@pytest.mark.asyncio
async def test_lemmy_resolves_public_post_urls_and_comment_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    allow_test_instances(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((url, kwargs))
        if url.endswith("/api/v4/search"):
            assert kwargs["params"]["search_term"] == "https://remote.example/post/123"
            assert kwargs["params"]["type_"] == "posts"
            return {"posts": [{"post": {"id": 77, "name": "Resolved post", "body": "Public body", "url": "https://remote.example/post/123"}, "creator": {"name": "poster"}}]}
        assert url.endswith("/api/v4/comment/list")
        assert kwargs["params"]["post_id"] == 77
        return {"items": [{"comment": {"id": 88, "post_id": 77, "content": "Reply", "published_at": "2025-01-01T00:00:00Z"}, "creator": {"name": "commenter"}}]}

    monkeypatch.setattr(public_forums, "get_json", fake_get_json)
    adapter = LemmyAdapter()
    post = await adapter.read("https://remote.example/post/123", {"instance_url": "https://lemmy.example"})
    replies = await adapter.replies("https://remote.example/post/123", options={"instance_url": "https://lemmy.example"})

    assert post.title == "Resolved post"
    assert post.extensions["source_url"] == "https://remote.example/post/123"
    assert [row.text for row in replies.items] == ["Reply"]
    assert replies.extensions["parent_type"] == "post"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_lemmy_returns_partial_page_for_malformed_items(monkeypatch: pytest.MonkeyPatch) -> None:
    allow_test_instances(monkeypatch)

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        return {"items": [{"unrecognized": True}, {"comment": {"id": 1, "content": "visible", "post_id": 3}, "creator": {"name": "user"}}]}

    monkeypatch.setattr(public_forums, "get_json", fake_get_json)
    page = await LemmyAdapter().search("term", options={"instance_url": "https://lemmy.example"})

    assert len(page.items) == 1
    assert page.complete is False
    assert "skipped" in page.warnings[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1", "https://127.0.0.1", "https://user:pass@lemmy.example", "https://lemmy.example/path"])
async def test_lemmy_rejects_unsafe_or_malformed_instance_before_upstream(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unsafe instance must not reach upstream")

    monkeypatch.setattr(public_forums, "get_json", fail_if_called)
    with pytest.raises(ReachError):
        await LemmyAdapter().search("term", options={"instance_url": url})


@pytest.mark.asyncio
async def test_lemmy_rejects_private_object_url_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    def public_dns(host: str, port: int, *, type: int) -> list[tuple[Any, ...]]:
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(security.socket, "getaddrinfo", public_dns)

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("private object URL must not be resolved")

    monkeypatch.setattr(public_forums, "get_json", fail_if_called)
    with pytest.raises(ReachError) as exc_info:
        await LemmyAdapter().read("https://127.0.0.1/post/4", {"instance_url": "https://lemmy.example"})
    assert exc_info.value.code == "invalid_url"


@pytest.mark.asyncio
async def test_discourse_public_search_returns_partial_snippets_with_page_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    allow_test_instances(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((url, kwargs))
        return {
            "posts": [
                {"id": 301, "topic_id": 41, "topic_slug": "public-topic", "topic_title": "Public topic", "post_number": 2, "username": "alice", "blurb": "snippet"},
                {"id": 302, "topic_id": 42, "topic_slug": "another", "topic_title": "Another topic", "post_number": 3, "username": "bob", "blurb": "another snippet"},
            ],
            "grouped_search_result": {"more_posts": "true"},
        }

    monkeypatch.setattr(public_forums, "get_json", fake_get_json)
    page = await DiscourseAdapter().search("public topic", limit=1, options={"instance_url": "https://forum.example"})

    assert calls[0][0] == "https://forum.example/search.json"
    assert calls[0][1]["allowed_hosts"] == frozenset({"forum.example"})
    assert calls[0][1]["params"] == {"q": "public topic", "page": 1}
    assert "headers" not in calls[0][1]
    assert len(page.items) == 1
    assert page.items[0].text == "snippet"
    assert page.items[0].access.complete is False
    assert page.pagination.cursor == "1:1"
    assert page.complete is False
    assert page.warnings


@pytest.mark.asyncio
async def test_discourse_topic_read_uses_public_json_and_normalizes_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    allow_test_instances(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((url, kwargs))
        return {
            "id": 77,
            "slug": "visible-topic",
            "title": "Public topic title",
            "post_stream": {
                "posts": [{"id": 700, "post_number": 1, "topic_slug": "visible-topic", "username": "author", "cooked": "<p>First post</p>", "created_at": "2025-02-01T00:00:00Z"}],
                "stream": [700],
            },
        }

    monkeypatch.setattr(public_forums, "get_json", fake_get_json)
    record = await DiscourseAdapter().read("https://forum.example/t/visible-topic/77", {"instance_url": "https://forum.example"})

    assert calls[0][0] == "https://forum.example/t/77.json"
    assert calls[0][1]["allowed_hosts"] == frozenset({"forum.example"})
    assert "headers" not in calls[0][1]
    assert record.type == "topic"
    assert record.id == "77"
    assert record.title == "Public topic title"
    assert record.text == "First post"
    assert record.url == "https://forum.example/t/visible-topic/77"
    assert record.extensions["source_url"] == record.url
    assert record.access.complete is True


@pytest.mark.asyncio
async def test_discourse_replies_report_partial_public_topic_stream_without_key_route(monkeypatch: pytest.MonkeyPatch) -> None:
    allow_test_instances(monkeypatch)
    calls: list[str] = []

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(url)
        return {
            "id": 77,
            "title": "Public topic",
            "post_stream": {
                "posts": [
                    {"id": 700, "post_number": 1, "username": "author", "cooked": "Opening"},
                    {"id": 701, "post_number": 2, "username": "reply", "cooked": "Visible reply"},
                ],
                "stream": [700, 701, 702],
            },
        }

    monkeypatch.setattr(public_forums, "get_json", fake_get_json)
    page = await DiscourseAdapter().replies("https://forum.example/t/77", options={"instance_url": "https://forum.example"})

    assert calls == ["https://forum.example/t/77.json"]
    assert [record.text for record in page.items] == ["Visible reply"]
    assert page.complete is False
    assert "part of the post stream" in page.warnings[0]
    assert "/posts.json" not in " ".join(calls)


@pytest.mark.asyncio
async def test_discourse_rejects_unsafe_instance_and_cross_instance_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("invalid instance or URL must not reach upstream")

    monkeypatch.setattr(public_forums, "get_json", fail_if_called)
    with pytest.raises(ReachError):
        await DiscourseAdapter().search("term", options={"instance_url": "https://127.0.0.1"})

    allow_test_instances(monkeypatch)
    with pytest.raises(ReachError) as exc_info:
        await DiscourseAdapter().read("https://other.example/t/77", {"instance_url": "https://forum.example"})
    assert exc_info.value.code == "invalid_url"


@pytest.mark.asyncio
async def test_forum_adapters_expose_only_supported_operations() -> None:
    for adapter in (FourChanAdapter(), LemmyAdapter(), DiscourseAdapter()):
        assert adapter.capability.status == "partial"
        assert adapter.capability.operations == ("search", "read", "replies")
        with pytest.raises(UnsupportedOperation):
            await adapter.profile("reader")
