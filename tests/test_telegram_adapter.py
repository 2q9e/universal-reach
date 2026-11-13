from __future__ import annotations

import pytest

from universal_reach.adapters import telegram
from universal_reach.adapters.telegram import TelegramAdapter, _ensure_public_channel, _parse_preview
from universal_reach.errors import ReachError


def _post(post_id: int, body: str, *, media: str = "", views: str = "") -> str:
    return f"""
    <div class="tgme_widget_message_wrap">
      <div class="tgme_widget_message" data-post="news_room/{post_id}">
        <div class="tgme_widget_message_user">
          <a class="tgme_widget_message_owner_name"><span>News Room</span></a>
          <span class="tgme_widget_message_author">Editor</span>
        </div>
        <div class="tgme_widget_message_text js-message_text" dir="auto">{body}</div>
        {media}
        <a class="tgme_widget_message_date" href="https://t.me/news_room/{post_id}">
          <time datetime="2024-03-04T05:06:07+00:00">Mar 4</time>
        </a>
        <span class="tgme_widget_message_views">{views}</span>
      </div>
    </div>
    """


def _page(*posts: str, extra: str = "12.3K subscribers") -> str:
    return f"""
    <div class="tgme_page_title"><span>News Room</span></div>
    <div class="tgme_page_extra">{extra}</div>
    <div class="tgme_page_description">Public <b>updates</b> from the channel.</div>
    {''.join(posts)}
    """




def test_profile_parser_supports_current_channel_info_markup() -> None:
    page = _parse_preview(
        "telegram",
        '<div class="tgme_channel_info_header_title"><span>Telegram News</span></div>'
        '<div class="tgme_channel_info_counter">9.13M subscribers 16 photos</div>'
        '<div class="tgme_channel_info_description">Public updates</div>',
    )

    _ensure_public_channel(page, "telegram")
    assert page.title == "Telegram News"
    assert page.extra == "9.13M subscribers 16 photos"
    assert page.description == "Public updates"


@pytest.mark.asyncio
async def test_health_check_uses_public_channel_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_text(url: str, **kwargs: object) -> str:
        return _page()

    monkeypatch.setattr(telegram, "get_text", fake_get_text)
    result = await TelegramAdapter().health_check()
    assert result.status == "available"
    assert result.access_mode == "anonymous"


@pytest.mark.asyncio
async def test_profile_uses_anonymous_public_preview_and_maps_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_get_text(url: str, **kwargs: object) -> str:
        calls.append((url, kwargs))
        return _page()

    monkeypatch.setattr(telegram, "get_text", fake_get_text)
    adapter = TelegramAdapter()
    profile = await adapter.profile("@news_room")

    assert adapter.capability.operations == ("read", "profile", "profile_posts")
    assert adapter.capability.anonymous_access == "partial"
    assert calls == [(
        "https://t.me/s/news_room",
        {"allowed_hosts": frozenset({"t.me"}), "params": None, "timeout": 8.0, "retries": 0, "max_bytes": 1_500_000},
    )]
    assert profile.platform == "telegram"
    assert profile.type == "channel"
    assert profile.id == "news_room"
    assert profile.url == "https://t.me/news_room"
    assert profile.title == "News Room"
    assert profile.author == "News Room"
    assert profile.text == "Public updates from the channel."
    assert profile.metrics == {"subscribers_text": "12.3K subscribers"}
    assert profile.access.mode == "anonymous"
    assert profile.access.complete is True
    assert profile.extensions["preview_only"] is True


@pytest.mark.asyncio
async def test_profile_posts_paginates_by_public_message_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_get_text(url: str, **kwargs: object) -> str:
        calls.append((url, kwargs))
        if kwargs.get("params") is None:
            return _page(_post(43, "Newest"), _post(42, "Older"))
        assert kwargs["params"] == {"before": "43"}
        return _page(_post(41, "Older still"))

    monkeypatch.setattr(telegram, "get_text", fake_get_text)
    adapter = TelegramAdapter()
    first = await adapter.profile_posts("news_room", limit=1)
    second = await adapter.profile_posts("news_room", limit=1, cursor=first.pagination.cursor)

    assert [item.id for item in first.items] == ["43"]
    assert first.items[0].type == "channel_post"
    assert first.items[0].url == "https://t.me/news_room/43"
    assert first.items[0].author == "Editor"
    assert first.items[0].created_at == "2024-03-04T05:06:07Z"
    assert first.items[0].access.complete is True
    assert first.pagination.cursor == "43"
    assert first.pagination.has_more is True
    assert first.complete is False
    assert [item.id for item in second.items] == ["41"]
    assert second.pagination.cursor is None
    assert second.pagination.has_more is False
    assert second.complete is True
    assert calls[1][0] == "https://t.me/s/news_room"


@pytest.mark.asyncio
async def test_read_maps_exact_post_and_uses_before_to_reach_it(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_get_text(url: str, **kwargs: object) -> str:
        calls.append(kwargs)
        return _page(_post(42, "Exact <b>post</b>", views="1.2K"))

    monkeypatch.setattr(telegram, "get_text", fake_get_text)
    post = await TelegramAdapter().read("https://t.me/news_room/42")

    assert calls[0]["params"] == {"before": "43"}
    assert post.id == "42"
    assert post.url == "https://t.me/news_room/42"
    assert post.author == "Editor"
    assert post.text == "Exact post"
    assert post.metrics == {"views_text": "1.2K"}
    assert post.extensions["channel_username"] == "news_room"
    assert post.access.mode == "anonymous"
    assert post.access.complete is True


@pytest.mark.asyncio
async def test_preview_hidden_message_and_media_are_marked_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_text(url: str, **kwargs: object) -> str:
        return _page(_post(42, "Please open Telegram to view this post", media='<div class="tgme_widget_message_photo_wrap"></div>'))

    monkeypatch.setattr(telegram, "get_text", fake_get_text)
    result = await TelegramAdapter().profile_posts("news_room", limit=5)

    item = result.items[0]
    assert item.text is None
    assert item.access.complete is False
    assert item.extensions["preview_limited"] is True
    assert item.extensions["media_present_but_not_retrieved"] is True
    assert result.complete is False
    assert result.warnings


@pytest.mark.asyncio
async def test_group_preview_is_not_treated_as_a_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_text(url: str, **kwargs: object) -> str:
        return _page(extra="2.1K members")

    monkeypatch.setattr(telegram, "get_text", fake_get_text)
    with pytest.raises(ReachError) as caught:
        await TelegramAdapter().profile("news_room")
    assert caught.value.code == "unsupported_url"


@pytest.mark.asyncio
async def test_invalid_inputs_and_urls_fail_before_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_if_called(*args: object, **kwargs: object) -> str:
        raise AssertionError("network must not be called for invalid input")

    monkeypatch.setattr(telegram, "get_text", fail_if_called)
    adapter = TelegramAdapter()

    with pytest.raises(ReachError) as caught_username:
        await adapter.profile("bad/name")
    assert caught_username.value.code == "invalid_input"

    with pytest.raises(ReachError) as caught_limit:
        await adapter.profile_posts("news_room", limit=True)
    assert caught_limit.value.code == "invalid_input"

    with pytest.raises(ReachError) as caught_cursor:
        await adapter.profile_posts("news_room", cursor="../private")
    assert caught_cursor.value.code == "invalid_input"

    for bad_url in (
        "http://t.me/news_room/42",
        "https://example.com/news_room/42",
        "https://t.me/news_room/0",
        "https://t.me/news_room/42/extra",
        "https://t.me:8443/news_room/42",
        "https://t.me/+123456789",
    ):
        with pytest.raises(ReachError):
            await adapter.read(bad_url)


@pytest.mark.asyncio
async def test_read_returns_not_found_when_post_is_not_in_public_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_text(url: str, **kwargs: object) -> str:
        return _page(_post(41, "A different post"))

    monkeypatch.setattr(telegram, "get_text", fake_get_text)
    with pytest.raises(ReachError) as caught:
        await TelegramAdapter().read("https://t.me/news_room/42")
    assert caught.value.code == "not_found"
