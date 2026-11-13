from __future__ import annotations

import pytest

from universal_reach.adapters import youtube
from universal_reach.adapters.youtube import FEED_URL, YouTubeAdapter
from universal_reach.errors import ReachError, UnsupportedOperation


CHANNEL_ID = "UC1234567890abcdefghijkl"


def _feed(*entries: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
        "<title>YouTube video feed</title>"
        "<updated>2026-01-03T04:05:06Z</updated>"
        + "".join(entries)
        + "</feed>"
    )


def _entry(video_id: str, title: str, *, channel_id: str = CHANNEL_ID, published: str = "2026-01-02T03:04:05+00:00") -> str:
    return (
        "<entry>"
        f"<yt:videoId>{video_id}</yt:videoId>"
        f"<yt:channelId>{channel_id}</yt:channelId>"
        f"<title>{title}</title>"
        '<link rel="alternate" href="https://not-youtube.example/watch?v=ignore-me"/>'
        "<author><name>Example channel</name><uri>https://www.youtube.com/channel/"
        f"{channel_id}</uri></author>"
        f"<published>{published}</published>"
        "<updated>2026-01-03T04:05:06Z</updated>"
        "</entry>"
    )


@pytest.mark.asyncio
async def test_health_check_reads_public_channel_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_get_text(url: str, **kwargs: object) -> str:
        calls.append(kwargs)
        return _feed()

    monkeypatch.setattr(youtube, "get_text", fake_get_text)
    result = await YouTubeAdapter().health_check()
    assert result.status == "available"
    assert result.access_mode == "anonymous"
    assert calls[0]["params"] == {"channel_id": youtube._HEALTH_CHANNEL_ID}


@pytest.mark.asyncio
async def test_profile_posts_reads_documented_feed_and_normalizes_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_get_text(url: str, **kwargs: object) -> str:
        calls.append((url, kwargs))
        return _feed(_entry("vid_00000001", "First video"), _entry("vid_00000002", "Second video"))

    monkeypatch.setattr(youtube, "get_text", fake_get_text)
    adapter = YouTubeAdapter()

    result = await adapter.profile_posts(CHANNEL_ID, limit=1)

    assert adapter.platform == "youtube"
    assert adapter.capability.operations == ("profile_posts",)
    assert calls == [
        (
            FEED_URL,
            {
                "allowed_hosts": frozenset({"www.youtube.com"}),
                "params": {"channel_id": CHANNEL_ID},
                "max_bytes": 500_000,
            },
        )
    ]
    assert result.operation == "profile_posts"
    assert result.complete is False
    assert result.pagination.cursor == "1"
    assert result.pagination.has_more is True
    assert result.extensions["channel_id"] == CHANNEL_ID
    assert result.extensions["feed_entry_count"] == 2
    assert result.extensions["feed_title"] == "YouTube video feed"
    assert result.items[0].id == "vid_00000001"
    assert result.items[0].title == "First video"
    assert result.items[0].author == "Example channel"
    assert result.items[0].url == "https://www.youtube.com/watch?v=vid_00000001"
    assert result.items[0].created_at == "2026-01-02T03:04:05Z"
    assert result.items[0].extensions["channel_id"] == CHANNEL_ID
    assert result.items[0].access.mode == "anonymous"
    assert result.items[0].access.complete is False
    assert any("complete channel history" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_profile_posts_offset_cursor_returns_next_feed_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_text(url: str, **kwargs: object) -> str:
        return _feed(_entry("vid_00000001", "First"), _entry("vid_00000002", "Second"))

    monkeypatch.setattr(youtube, "get_text", fake_get_text)

    result = await YouTubeAdapter().profile_posts(CHANNEL_ID, limit=1, cursor="1")

    assert [item.id for item in result.items] == ["vid_00000002"]
    assert result.pagination.cursor is None
    assert result.pagination.has_more is False
    assert result.complete is False


@pytest.mark.asyncio
async def test_invalid_channel_id_and_pagination_fail_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected_get_text(url: str, **kwargs: object) -> str:
        pytest.fail("invalid input must not reach YouTube")

    monkeypatch.setattr(youtube, "get_text", unexpected_get_text)
    adapter = YouTubeAdapter()
    for channel_id in ("", "@handle", "https://www.youtube.com/channel/" + CHANNEL_ID, "UC!short"):
        with pytest.raises(ReachError) as error:
            await adapter.profile_posts(channel_id)
        assert error.value.code == "invalid_request"
    for cursor in ("1/2", "9" * 5000, "10001"):
        with pytest.raises(ReachError) as error:
            await adapter.profile_posts(CHANNEL_ID, cursor=cursor)
        assert error.value.code == "invalid_cursor"
    with pytest.raises(ReachError):
        await adapter.profile_posts(CHANNEL_ID, limit=0)


@pytest.mark.asyncio
async def test_feed_entries_are_channel_scoped_and_links_are_constructed_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_text(url: str, **kwargs: object) -> str:
        return _feed(
            _entry("vid_00000001", "Expected"),
            _entry("vid_00000002", "Other channel", channel_id="UCabcdefghijklmnopqrstuv"),
            _entry("vid_00000003", "Malformed timestamp", published="not-a-date"),
        )

    monkeypatch.setattr(youtube, "get_text", fake_get_text)

    result = await YouTubeAdapter().profile_posts(CHANNEL_ID)

    assert [item.id for item in result.items] == ["vid_00000001", "vid_00000003"]
    assert all(item.url.startswith("https://www.youtube.com/watch?v=") for item in result.items)
    assert result.items[1].created_at is None
    assert any("different or unidentified channel" in warning for warning in result.warnings)
    assert any("unreadable published timestamp" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_malformed_feed_is_reported_and_search_remains_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    async def malformed_get_text(url: str, **kwargs: object) -> str:
        return "<html>sign in</html>"

    monkeypatch.setattr(youtube, "get_text", malformed_get_text)
    with pytest.raises(ReachError) as error:
        await YouTubeAdapter().profile_posts(CHANNEL_ID)
    assert error.value.code == "parser_error"

    with pytest.raises(UnsupportedOperation):
        await YouTubeAdapter().search("cats")
