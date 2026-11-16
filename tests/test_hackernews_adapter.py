from __future__ import annotations

import pytest

from universal_reach.adapters import hackernews
from universal_reach.adapters.hackernews import HackerNewsAdapter
from universal_reach.errors import ReachError


@pytest.fixture
def adapter() -> HackerNewsAdapter:
    return HackerNewsAdapter()


@pytest.mark.asyncio
async def test_read_normalizes_public_item(monkeypatch: pytest.MonkeyPatch, adapter: HackerNewsAdapter) -> None:
    async def fake_get_json(url: str, **kwargs: object) -> dict[str, object]:
        assert url.endswith("/item/123.json")
        assert kwargs["allowed_hosts"] == hackernews.API_HOSTS
        return {
            "id": 123,
            "type": "story",
            "by": "reader",
            "time": 1_700_000_000,
            "title": "Example story",
            "url": "https://example.org/article",
            "score": 12,
            "descendants": 3,
            "kids": [124, 125],
        }

    monkeypatch.setattr(hackernews, "get_json", fake_get_json)
    record = await adapter.read("https://news.ycombinator.com/item?id=123")

    assert record.platform == "hackernews"
    assert record.type == "story"
    assert record.id == "123"
    assert record.url == "https://news.ycombinator.com/item?id=123"
    assert record.author == "reader"
    assert record.title == "Example story"
    assert record.created_at == "2023-11-14T22:13:20Z"
    assert record.metrics["score"] == 12
    assert record.extensions["external_url"] == "https://example.org/article"
    assert record.extensions["comment_ids"] == ["124", "125"]
    assert record.access.mode == "anonymous"


@pytest.mark.asyncio
async def test_feed_normalizes_top_stories_and_paginates(monkeypatch: pytest.MonkeyPatch, adapter: HackerNewsAdapter) -> None:
    calls: list[str] = []

    async def fake_get_json(url: str, **kwargs: object) -> dict[str, object] | list[int]:
        calls.append(url)
        if url.endswith("/topstories.json"):
            return [101, 102, 103]
        item_id = int(url.rsplit("/item/", 1)[1].removesuffix(".json"))
        return {"id": item_id, "type": "story", "title": f"Story {item_id}", "by": "reader"}

    monkeypatch.setattr(hackernews, "get_json", fake_get_json)
    first = await adapter.feed("TOP", limit=2)
    second = await adapter.feed("top", limit=2, cursor=first.pagination.cursor)

    assert adapter.capability.operations[0] == "feed"
    assert [item.id for item in first.items] == ["101", "102"]
    assert first.pagination.cursor == "2"
    assert first.pagination.has_more is True
    assert first.complete is False
    assert first.extensions == {
        "category": "top",
        "source": "hackernews_firebase",
        "feed_entry_count": 3,
        "source_item_count": 3,
    }
    assert [item.id for item in second.items] == ["103"]
    assert second.pagination.cursor is None
    assert second.pagination.has_more is False
    assert second.complete is True
    assert calls.count(f"{hackernews.API_BASE}/topstories.json") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category,endpoint",
    [
        ("top", "topstories"),
        ("new", "newstories"),
        ("best", "beststories"),
        ("ask", "askstories"),
        ("show", "showstories"),
        ("job", "jobstories"),
    ],
)
async def test_feed_categories_use_official_firebase_lists(
    monkeypatch: pytest.MonkeyPatch,
    adapter: HackerNewsAdapter,
    category: str,
    endpoint: str,
) -> None:
    calls: list[str] = []

    async def fake_get_json(url: str, **kwargs: object) -> list[object]:
        calls.append(url)
        return []

    monkeypatch.setattr(hackernews, "get_json", fake_get_json)
    result = await adapter.feed(category)

    assert calls == [f"{hackernews.API_BASE}/{endpoint}.json"]
    assert result.extensions["category"] == category
    assert result.items == []


@pytest.mark.asyncio
async def test_feed_rejects_unknown_category_cursor_and_options_before_request(
    monkeypatch: pytest.MonkeyPatch,
    adapter: HackerNewsAdapter,
) -> None:
    async def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("invalid feed input must not reach the network")

    monkeypatch.setattr(hackernews, "get_json", fail_if_called)
    for category, cursor, options in (
        ("popular", None, None),
        ("top", 1, None),
        ("top", None, {"query": "ignored"}),
    ):
        with pytest.raises(ReachError):
            await adapter.feed(category, cursor=cursor, options=options)


@pytest.mark.asyncio
@pytest.mark.parametrize("item", [None, {"id": 123, "type": "story", "deleted": True}, {"id": 123, "type": "story", "dead": True}])
async def test_read_returns_not_found_for_missing_or_deleted_items(
    monkeypatch: pytest.MonkeyPatch,
    adapter: HackerNewsAdapter,
    item: dict[str, object] | None,
) -> None:
    async def fake_get_json(url: str, **kwargs: object) -> dict[str, object] | None:
        return item

    monkeypatch.setattr(hackernews, "get_json", fake_get_json)
    with pytest.raises(ReachError) as exc_info:
        await adapter.read("https://news.ycombinator.com/item?id=123")
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_replies_are_bounded_and_keep_next_offset(
    monkeypatch: pytest.MonkeyPatch,
    adapter: HackerNewsAdapter,
) -> None:
    requested: list[str] = []

    async def fake_get_json(url: str, **kwargs: object) -> dict[str, object] | None:
        requested.append(url)
        if url.endswith("/item/123.json"):
            return {"id": 123, "type": "story", "kids": [201, 202, 203]}
        item_id = url.rsplit("/item/", 1)[1].removesuffix(".json")
        return {"id": int(item_id), "type": "comment", "by": f"user-{item_id}", "text": "reply"}

    monkeypatch.setattr(hackernews, "get_json", fake_get_json)
    page = await adapter.replies("https://news.ycombinator.com/item?id=123", limit=2)

    assert [item.id for item in page.items] == ["201", "202"]
    assert page.pagination.cursor == "2"
    assert page.pagination.has_more is True
    assert page.complete is False
    assert all("203" not in url for url in requested)


@pytest.mark.asyncio
async def test_replies_report_missing_comment_as_partial(
    monkeypatch: pytest.MonkeyPatch,
    adapter: HackerNewsAdapter,
) -> None:
    async def fake_get_json(url: str, **kwargs: object) -> dict[str, object] | None:
        if url.endswith("/item/123.json"):
            return {"id": 123, "type": "story", "kids": [201]}
        return None

    monkeypatch.setattr(hackernews, "get_json", fake_get_json)
    page = await adapter.replies("https://news.ycombinator.com/item?id=123")

    assert page.items == []
    assert page.complete is False
    assert page.warnings == ["Comment 201 is missing or deleted."]


@pytest.mark.asyncio
async def test_profile_and_profile_posts(monkeypatch: pytest.MonkeyPatch, adapter: HackerNewsAdapter) -> None:
    async def fake_get_json(url: str, **kwargs: object) -> dict[str, object] | list[object] | None:
        if "/user/reader.json" in url:
            return {"id": "reader", "created": 1_600_000_000, "karma": 42, "about": "HN user", "submitted": [301, 302]}
        if "/item/301.json" in url:
            return {"id": 301, "type": "story", "by": "reader", "title": "First"}
        if "/item/302.json" in url:
            return {"id": 302, "type": "comment", "by": "reader", "text": "Second"}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(hackernews, "get_json", fake_get_json)
    profile = await adapter.profile("reader")
    page = await adapter.profile_posts("reader", limit=1)

    assert profile.author == "reader"
    assert profile.metrics["karma"] == 42
    assert profile.created_at == "2020-09-13T12:26:40Z"
    assert page.items[0].id == "301"
    assert page.pagination.cursor == "1"
    assert page.pagination.has_more is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://news.ycombinator.com/item?id=123",
        "https://example.com/item?id=123",
        "https://news.ycombinator.com/user?id=123",
        "https://news.ycombinator.com/item?id=../etc/passwd",
    ],
)
async def test_invalid_urls_are_rejected_without_network(
    monkeypatch: pytest.MonkeyPatch,
    adapter: HackerNewsAdapter,
    url: str,
) -> None:
    async def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("network must not be called for an invalid URL")

    monkeypatch.setattr(hackernews, "get_json", fail_if_called)
    with pytest.raises(ReachError) as exc_info:
        await adapter.read(url)
    assert exc_info.value.code in {"invalid_url"}


@pytest.mark.asyncio
async def test_profile_input_is_validated(adapter: HackerNewsAdapter) -> None:
    with pytest.raises(ReachError) as exc_info:
        await adapter.profile("bad/user")
    assert exc_info.value.code == "invalid_input"


@pytest.mark.asyncio
async def test_search_uses_public_algolia_index_and_paginates(
    monkeypatch: pytest.MonkeyPatch,
    adapter: HackerNewsAdapter,
) -> None:
    async def fake_get_json(url: str, **kwargs: object) -> dict[str, object]:
        assert url == f"{hackernews.SEARCH_BASE}/search_by_date"
        assert kwargs["allowed_hosts"] == hackernews.SEARCH_HOSTS
        assert kwargs["params"] == {
            "query": "rust async",
            "page": 1,
            "hitsPerPage": 20,
            "tags": "story,(ask_hn,show_hn)",
            "numericFilters": "points>10,created_at_i>=123",
        }
        return {
            "hits": [{
                "objectID": "456",
                "_tags": ["story", "author_pg"],
                "title": "A Rust story",
                "story_text": "Story body",
                "url": "https://example.org/rust",
                "author": "pg",
                "created_at": "2024-02-03T04:05:06.000Z",
                "points": 12,
                "num_comments": 4,
            }],
            "nbHits": 120,
            "nbPages": 6,
            "hitsPerPage": 20,
        }

    monkeypatch.setattr(hackernews, "get_json", fake_get_json)
    page = await adapter.search(
        " rust async ",
        limit=20,
        cursor="1",
        options={
            "sort": "recent",
            "tags": "story,(ask_hn,show_hn)",
            "numeric_filters": ["points>10", "created_at_i>=123"],
        },
    )

    assert page.operation == "search"
    assert page.pagination.cursor == "2"
    assert page.pagination.has_more is True
    assert page.complete is False
    assert page.extensions["source"] == "hn_search_algolia"
    assert page.extensions["total_matches"] == 120
    record = page.items[0]
    assert record.id == "456"
    assert record.type == "story"
    assert record.url == "https://news.ycombinator.com/item?id=456"
    assert record.title == "A Rust story"
    assert record.text == "Story body"
    assert record.metrics == {"score": 12, "descendants": 4}
    assert record.extensions["external_url"] == "https://example.org/rust"
    assert record.access.mode == "anonymous"


@pytest.mark.asyncio
async def test_search_normalizes_comment_hits_and_reports_index_cap(
    monkeypatch: pytest.MonkeyPatch,
    adapter: HackerNewsAdapter,
) -> None:
    async def fake_get_json(url: str, **kwargs: object) -> dict[str, object]:
        return {
            "hits": [{
                "objectID": "789",
                "_tags": ["comment", "author_reader"],
                "comment_text": "A reply",
                "story_title": "Parent story",
                "story_url": "https://example.org/parent",
                "author": "reader",
                "created_at": "2025-01-01T00:00:00Z",
                "story_id": 123,
                "parent_id": 456,
                "points": 2,
            }],
            "nbHits": 1001,
            "nbPages": 10,
            "hitsPerPage": 100,
        }

    monkeypatch.setattr(hackernews, "get_json", fake_get_json)
    page = await adapter.search("reply", limit=100)

    assert page.items[0].type == "comment"
    assert page.items[0].title == "Parent story"
    assert page.items[0].text == "A reply"
    assert page.items[0].extensions["story_id"] == "123"
    assert page.items[0].extensions["parent_id"] == "456"
    assert page.complete is False
    assert page.warnings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,cursor,options",
    [
        ("", None, None),
        ("x" * 501, None, None),
        ("topic", "-1", None),
        ("topic", "1000", None),
        ("topic", None, {"sort": "popular"}),
        ("topic", None, {"tags": "story))"}),
        ("topic", None, {"numeric_filters": "author_pg>5"}),
    ],
)
async def test_search_rejects_invalid_inputs_without_network(
    monkeypatch: pytest.MonkeyPatch,
    adapter: HackerNewsAdapter,
    query: str,
    cursor: str | None,
    options: dict[str, object] | None,
) -> None:
    async def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("network must not be called for invalid search input")

    monkeypatch.setattr(hackernews, "get_json", fail_if_called)
    with pytest.raises(ReachError):
        await adapter.search(query, cursor=cursor, options=options)
