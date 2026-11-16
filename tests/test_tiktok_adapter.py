from __future__ import annotations

import pytest

from universal_reach.adapters import tiktok
from universal_reach.adapters.tiktok import API_HOSTS, USER_INFO_URL, VIDEO_LIST_URL, TikTokAdapter
from universal_reach.errors import ReachError, UnsupportedOperation


OPEN_ID = "723f24d7-e717-40f8-a2b6-cb8464cd23b4"
ACCESS_TOKEN = "test-access-token-not-real"


def _user_payload(open_id: str = OPEN_ID) -> dict[str, object]:
    return {
        "data": {"user": {"open_id": open_id, "display_name": "Tik Toker", "avatar_url": "https://p19-sign.tiktokcdn-us.com/avatar.jpg"}},
        "error": {"code": "ok", "message": "", "log_id": "test-log"},
    }


def _video(video_id: str = "7080213458555737986") -> dict[str, object]:
    return {
        "id": video_id,
        "create_time": 1_650_000_000,
        "title": "A public video",
        "video_description": "A short description",
        "share_url": f"https://www.tiktok.com/@creator/video/{video_id}",
        "cover_image_url": "https://p16-sign.tiktokcdn-us.com/cover.jpg",
        "embed_link": f"https://www.tiktok.com/embed/v2/{video_id}",
        "like_count": 3,
        "comment_count": 4,
        "share_count": 5,
        "view_count": 6,
    }


def _success(payload: object) -> dict[str, object]:
    return {"data": payload, "error": {"code": "ok", "message": "", "log_id": "list-log"}}


@pytest.mark.asyncio
async def test_public_read_uses_anonymous_oembed_and_normalizes_safe_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_get_json(url: str, **kwargs: object) -> object:
        calls.append((url, kwargs))
        return {
            "type": "video",
            "provider_name": "TikTok",
            "embed_product_id": "7080213458555737986",
            "title": "A public video",
            "author_name": "Creator",
            "thumbnail_url": "https://p16-sign.tiktokcdn-us.com/cover.jpg",
            "thumbnail_width": "325",
            "thumbnail_height": 578,
            "html": '<iframe src="javascript:alert(1)"></iframe>',
        }

    monkeypatch.setattr(tiktok, "get_json", fake_get_json)

    result = await TikTokAdapter().read("https://www.tiktok.com/@creator/video/7080213458555737986")

    assert calls == [
        (
            tiktok.OEMBED_URL,
            {
                "allowed_hosts": tiktok.OEMBED_HOSTS,
                "params": {"url": "https://www.tiktok.com/@creator/video/7080213458555737986"},
            },
        )
    ]
    assert result.platform == "tiktok"
    assert result.type == "video"
    assert result.id == "7080213458555737986"
    assert result.author == "Creator"
    assert result.title == "A public video"
    assert result.media[0].url == "https://p16-sign.tiktokcdn-us.com/cover.jpg"
    assert result.media[0].metadata == {"width": 325, "height": 578}
    assert result.access.mode == "anonymous"
    assert result.access.complete is False
    assert "javascript:" not in repr(result.to_dict())
    assert "iframe" not in repr(result.to_dict())


@pytest.mark.asyncio
async def test_public_read_rejects_unsafe_or_unsupported_urls_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected_get_json(url: str, **kwargs: object) -> object:
        pytest.fail("invalid TikTok URLs must be rejected before making a request")

    monkeypatch.setattr(tiktok, "get_json", unexpected_get_json)
    adapter = TikTokAdapter()
    for url in (
        "http://www.tiktok.com/@creator/video/123",
        "https://www.tiktok.com.evil.test/@creator/video/123",
        "https://vm.tiktok.com/short-code",
        "https://www.tiktok.com/@creator/live/123",
        "https://www.tiktok.com/@creator/video/not-numeric",
        "https://user:pass@www.tiktok.com/@creator/video/123",
    ):
        with pytest.raises(ReachError):
            await adapter.read(url)


@pytest.mark.asyncio
async def test_public_read_rejects_wrong_oembed_identity_and_ignores_html(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_json(url: str, **kwargs: object) -> object:
        return {"type": "video", "provider_name": "TikTok", "embed_product_id": "999"}

    monkeypatch.setattr(tiktok, "get_json", fake_get_json)
    with pytest.raises(ReachError) as error:
        await TikTokAdapter().read("https://www.tiktok.com/@creator/video/123")
    assert error.value.code == "parser_error"


@pytest.mark.asyncio
async def test_profile_verifies_token_owner_and_normalizes_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", ACCESS_TOKEN)
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_get_json(url: str, **kwargs: object) -> object:
        calls.append((url, kwargs))
        return _user_payload()

    monkeypatch.setattr(tiktok, "get_json", fake_get_json)

    profile = await TikTokAdapter().profile(OPEN_ID)

    assert calls == [
        (
            USER_INFO_URL,
            {
                "allowed_hosts": API_HOSTS,
                "params": {"fields": "open_id,display_name,avatar_url"},
                "headers": {"Authorization": f"Bearer {ACCESS_TOKEN}"},
                "json_error_statuses": frozenset({400, 401, 403, 429, 500, 502, 503}),
            },
        )
    ]
    assert profile.platform == "tiktok"
    assert profile.type == "profile"
    assert profile.id == OPEN_ID
    assert profile.title == "Tik Toker"
    assert profile.author == "Tik Toker"
    assert profile.media[0].url == "https://p19-sign.tiktokcdn-us.com/avatar.jpg"
    assert profile.access.mode == "authenticated"
    assert ACCESS_TOKEN not in repr(profile.to_dict())


@pytest.mark.asyncio
async def test_profile_posts_verifies_owner_then_uses_documented_cursor_and_maps_video_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", ACCESS_TOKEN)
    get_calls: list[tuple[str, dict[str, object]]] = []
    post_calls: list[tuple[str, dict[str, object]]] = []

    async def fake_get_json(url: str, **kwargs: object) -> object:
        get_calls.append((url, kwargs))
        return _user_payload()

    async def fake_post_json(url: str, **kwargs: object) -> object:
        post_calls.append((url, kwargs))
        return _success({"videos": [_video()], "cursor": 1_650_000_001_000, "has_more": True})

    monkeypatch.setattr(tiktok, "get_json", fake_get_json)
    monkeypatch.setattr(tiktok, "_post_json", fake_post_json)

    result = await TikTokAdapter().profile_posts(OPEN_ID, limit=10, cursor="1650000000000")

    assert len(get_calls) == 1
    assert post_calls == [
        (
            VIDEO_LIST_URL,
            {
                "params": {"fields": tiktok._VIDEO_FIELDS},
                "headers": {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"},
                "body": {"max_count": 10, "cursor": 1_650_000_000_000},
            },
        )
    ]
    assert result.operation == "profile_posts"
    assert result.complete is False
    assert result.pagination.has_more is True
    assert result.pagination.cursor == "1650000001000"
    record = result.items[0]
    assert record.id == "7080213458555737986"
    assert record.url == "https://www.tiktok.com/@creator/video/7080213458555737986"
    assert record.title == "A public video"
    assert record.text == "A short description"
    assert record.created_at == "2022-04-15T05:20:00Z"
    assert record.metrics == {"likes": 3, "comments": 4, "shares": 5, "views": 6}
    assert record.media[0].url == "https://p16-sign.tiktokcdn-us.com/cover.jpg"
    assert record.extensions["embed_link"] == "https://www.tiktok.com/embed/v2/7080213458555737986"
    assert record.access.mode == "authenticated"
    assert ACCESS_TOKEN not in repr(result.to_dict())


@pytest.mark.asyncio
async def test_api_max_count_is_enforced_and_page_completeness_uses_has_more(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", ACCESS_TOKEN)
    async def fake_get_json(url: str, **kwargs: object) -> object:
        return _user_payload()

    captured: dict[str, object] = {}

    async def fake_post_json(url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return _success({"videos": [_video()], "cursor": 0, "has_more": False})

    monkeypatch.setattr(tiktok, "get_json", fake_get_json)
    monkeypatch.setattr(tiktok, "_post_json", fake_post_json)

    result = await TikTokAdapter().profile_posts(OPEN_ID, limit=100)

    assert captured["body"] == {"max_count": 20}
    assert result.complete is True
    assert result.pagination.has_more is False
    assert result.pagination.cursor is None
    assert any("capped" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_owner_mismatch_stops_before_listing_videos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", ACCESS_TOKEN)
    async def fake_get_json(url: str, **kwargs: object) -> object:
        return _user_payload("different-owner")

    async def unexpected_post_json(url: str, **kwargs: object) -> object:
        pytest.fail("video listing must not run for a mismatched owner")

    monkeypatch.setattr(tiktok, "get_json", fake_get_json)
    monkeypatch.setattr(tiktok, "_post_json", unexpected_post_json)

    with pytest.raises(ReachError) as error:
        await TikTokAdapter().profile_posts(OPEN_ID)
    assert error.value.code == "account_mismatch"


@pytest.mark.asyncio
async def test_invalid_owner_token_limit_and_cursor_fail_before_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIKTOK_ACCESS_TOKEN", raising=False)
    async def unexpected_get_json(url: str, **kwargs: object) -> object:
        pytest.fail("invalid input must not reach TikTok")

    monkeypatch.setattr(tiktok, "get_json", unexpected_get_json)
    adapter = TikTokAdapter()
    for owner in ("", "@creator", "id with spaces"):
        with pytest.raises(ReachError):
            await adapter.profile(owner, {"access_token": ACCESS_TOKEN})
    for options in (None, {}):
        with pytest.raises(ReachError):
            await adapter.profile(OPEN_ID, options)
    with pytest.raises(ReachError) as token_argument:
        await adapter.profile(OPEN_ID, {"access_token": ACCESS_TOKEN})
    assert token_argument.value.code == "invalid_argument"
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", ACCESS_TOKEN)
    with pytest.raises(ReachError) as error:
        await adapter.profile_posts(OPEN_ID, limit=0)
    assert error.value.code == "invalid_request"
    with pytest.raises(ReachError) as error:
        await adapter.profile_posts(OPEN_ID, cursor="1/2")
    assert error.value.code == "invalid_cursor"


@pytest.mark.asyncio
async def test_provider_scope_errors_are_structured_and_search_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", ACCESS_TOKEN)
    async def fake_get_json(url: str, **kwargs: object) -> object:
        return {
            "data": {},
            "error": {"code": "scope_not_authorized", "message": "Authorize video.list", "log_id": "scope-log"},
        }

    monkeypatch.setattr(tiktok, "get_json", fake_get_json)
    with pytest.raises(ReachError) as error:
        await TikTokAdapter().profile_posts(OPEN_ID)
    assert error.value.code == "permission_denied"
    assert error.value.details == {"provider_code": "scope_not_authorized", "log_id": "scope-log"}
    with pytest.raises(UnsupportedOperation):
        await TikTokAdapter().search("cats")


def test_malformed_response_and_unsafe_urls_are_not_normalized() -> None:
    with pytest.raises(ReachError) as error:
        tiktok._response_data({"error": {"code": "ok"}}, "test")
    assert error.value.code == "parser_error"

    record = tiktok._video_record(
        {**_video(), "share_url": "https://www.tiktok.com.evil.test/video/123", "embed_link": "javascript:alert(1)"},
        OPEN_ID,
    )
    assert record.url is None
    assert record.extensions["embed_link"] is None
