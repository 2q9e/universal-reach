from __future__ import annotations

import pytest

from universal_reach.adapters import forem
from universal_reach.adapters.forem import ForemAdapter
from universal_reach.errors import ReachError


def _article(article_id: int, *, slug: str | None = None) -> dict[str, object]:
    return {
        "id": article_id,
        "title": f"Article {article_id}",
        "description": "A short public summary.",
        "slug": slug or f"article-{article_id}",
        "url": f"https://dev.to/alex/article-{article_id}",
        "published_at": "2026-01-02T03:04:05Z",
        "tag_list": ["python", "testing"],
        "public_reactions_count": 7,
        "comments_count": 2,
        "reading_time_minutes": 4,
        "cover_image": "https://media.example.test/cover.png",
        "user": {"username": "alex", "name": "Alex Example"},
    }


@pytest.mark.asyncio
async def test_search_uses_public_api_and_normalizes_summaries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_get_json(url: str, **kwargs: object) -> object:
        calls.append((url, kwargs))
        return [_article(1)]

    monkeypatch.setattr(forem, "get_json", fake_get_json)
    adapter = ForemAdapter()

    result = await adapter.search("  python async  ", limit=5, options={"top": 14})

    assert adapter.platform == "devto"
    assert adapter.capability.operations == ("search", "read", "profile", "profile_posts")
    assert calls == [
        (
            "https://dev.to/api/articles/search",
            {
                "allowed_hosts": frozenset({"dev.to"}),
                "params": {"q": "python async", "page": 1, "per_page": 5, "top": 14},
                "headers": {"Accept": "application/vnd.forem.api-v1+json"},
            },
        )
    ]
    assert result.operation == "search"
    assert result.complete is True
    assert result.pagination.cursor is None
    assert result.pagination.has_more is False
    assert result.items[0].id == "1"
    assert result.items[0].author == "alex"
    assert result.items[0].url == "https://dev.to/alex/article-1"
    assert result.items[0].text == "A short public summary."
    assert result.items[0].metrics == {"reactions": 7, "comments": 2, "reading_time_minutes": 4}
    assert result.items[0].extensions["tags"] == ["python", "testing"]
    assert result.items[0].access.mode == "anonymous"
    assert result.items[0].access.complete is False
    assert result.items[0].media[0].url == "https://media.example.test/cover.png"
    assert any("summaries" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_search_full_page_has_explicit_inferred_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_get_json(url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return [_article(3), _article(4)]

    monkeypatch.setattr(forem, "get_json", fake_get_json)
    result = await ForemAdapter().search("api", limit=2, cursor="3")

    assert captured["params"] == {"q": "api", "page": 3, "per_page": 2}
    assert result.complete is False
    assert result.pagination.cursor == "4"
    assert result.pagination.has_more is True
    assert "inferred" in (result.pagination.detail or "")
    assert any("total-page count" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_read_translates_dev_article_url_to_documented_api_route(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_get_json(url: str, **kwargs: object) -> object:
        calls.append((url, kwargs))
        return {**_article(17, slug="how-to-test"), "body_markdown": "# Full article"}

    monkeypatch.setattr(forem, "get_json", fake_get_json)
    record = await ForemAdapter().read("https://dev.to/alex/how-to-test/?utm_source=test")

    assert calls == [
        (
            "https://dev.to/api/articles/alex/how-to-test",
            {"allowed_hosts": frozenset({"dev.to"}), "headers": {"Accept": "application/vnd.forem.api-v1+json"}},
        )
    ]
    assert record.type == "article"
    assert record.id == "17"
    assert record.text == "# Full article"
    assert record.access.complete is True


@pytest.mark.asyncio
async def test_profile_uses_public_user_route_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_get_json(url: str, **kwargs: object) -> object:
        calls.append((url, kwargs))
        return {
            "id": 24,
            "username": "dev_user",
            "name": "Dev User",
            "summary": "Writes about Python.",
            "joined_at": "2020-03-04T00:00:00Z",
            "public_reactions_count": 90,
            "profile_image": "https://media.example.test/avatar.png",
        }

    monkeypatch.setattr(forem, "get_json", fake_get_json)
    record = await ForemAdapter().profile("dev_user")

    assert calls == [
        (
            "https://dev.to/api/users/dev_user",
            {"allowed_hosts": frozenset({"dev.to"}), "headers": {"Accept": "application/vnd.forem.api-v1+json"}},
        )
    ]
    assert record.type == "profile"
    assert record.id == "24"
    assert record.author == "dev_user"
    assert record.title == "Dev User"
    assert record.text == "Writes about Python."
    assert record.metrics["public_reactions_count"] == 90
    assert record.access.complete is True


@pytest.mark.asyncio
async def test_profile_posts_uses_public_username_filter_and_page_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_get_json(url: str, **kwargs: object) -> object:
        captured["url"] = url
        captured.update(kwargs)
        return [_article(1), _article(2)]

    monkeypatch.setattr(forem, "get_json", fake_get_json)
    result = await ForemAdapter().profile_posts("alex", limit=2, cursor="2")

    assert captured["url"] == "https://dev.to/api/articles"
    assert captured["params"] == {"username": "alex", "page": 2, "per_page": 2}
    assert result.operation == "profile_posts"
    assert result.pagination.has_more is True
    assert result.pagination.cursor == "3"
    assert result.complete is False


@pytest.mark.asyncio
async def test_read_rejects_non_dev_article_urls_without_upstream_call(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected_get_json(url: str, **kwargs: object) -> object:
        pytest.fail("invalid URLs must not reach the API client")

    monkeypatch.setattr(forem, "get_json", unexpected_get_json)
    adapter = ForemAdapter()
    for url in (
        "http://dev.to/alex/post",
        "https://dev.to.evil.example/alex/post",
        "https://user@dev.to/alex/post",
        "https://dev.to/alex/a%2Fb",
        "https://dev.to/alex",
    ):
        with pytest.raises(ReachError) as error:
            await adapter.read(url)
        assert error.value.code in {"invalid_url", "unsupported_url"}


@pytest.mark.asyncio
async def test_upstream_errors_are_preserved_and_bad_payloads_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_get_json(url: str, **kwargs: object) -> object:
        raise ReachError("rate_limited", "slow down", retryable=True)

    monkeypatch.setattr(forem, "get_json", failing_get_json)
    with pytest.raises(ReachError) as error:
        await ForemAdapter().search("python")
    assert error.value.code == "rate_limited"
    assert error.value.retryable is True

    async def malformed_get_json(url: str, **kwargs: object) -> object:
        return {"articles": []}

    monkeypatch.setattr(forem, "get_json", malformed_get_json)
    with pytest.raises(ReachError) as error:
        await ForemAdapter().search("python")
    assert error.value.code == "parser_error"


@pytest.mark.asyncio
async def test_invalid_pagination_and_limits_fail_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected_get_json(url: str, **kwargs: object) -> object:
        pytest.fail("invalid request parameters must not reach the API client")

    monkeypatch.setattr(forem, "get_json", unexpected_get_json)
    adapter = ForemAdapter()
    for args in (("python", 0, None), ("python", 101, None), ("python", 10, "0"), ("python", 10, "1/2")):
        with pytest.raises(ReachError):
            await adapter.search(args[0], limit=args[1], cursor=args[2])
    with pytest.raises(ReachError):
        await adapter.search("python", options={"top": True})
