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
