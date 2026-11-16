from __future__ import annotations

from typing import Any

import pytest

from universal_reach.adapters import pinterest
from universal_reach.adapters.pinterest import API_HOSTS, API_ROOT, PinterestAdapter
from universal_reach.errors import ReachError


TOKEN = "test-pinterest-access-token-not-real"
IMAGE_URL = "https://i.pinimg.com/1200x/ab/cd/image.jpg"


def pin_payload(pin_id: str = "123456789012345678") -> dict[str, Any]:
    return {
        "id": pin_id,
        "created_at": "2026-11-16T09:30:00Z",
        "title": "A saved idea",
        "description": "A short Pin description",
        "link": "https://example.org/source",
        "board_owner": {"username": "pin-owner"},
        "media": {
            "media_type": "image",
            "images": {"1200x": {"url": IMAGE_URL, "width": 1200, "height": 800}},
        },
    }


def account_payload() -> dict[str, Any]:
    return {
        "id": "7654321",
        "username": "my_account",
        "business_name": "My Pinterest",
        "about": "Account bio",
        "account_type": "BUSINESS",
        "profile_image": "https://i.pinimg.com/profile.jpg",
        "follower_count": 12,
        "following_count": 34,
        "board_count": 5,
        "pin_count": 89,
        "monthly_views": 1200,
    }


def install_get(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_get_json(url: str, **kwargs: Any) -> Any:
        call = {"url": url, **kwargs}
        calls.append(call)
        return await handler(call)

    monkeypatch.setattr(pinterest, "get_json", fake_get_json)
    return calls


def expected_auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}


@pytest.mark.asyncio
async def test_pin_read_uses_official_authenticated_api_and_normalizes_safe_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINTEREST_ACCESS_TOKEN", TOKEN)

    async def handler(call: dict[str, Any]) -> dict[str, Any]:
        assert call == {
            "url": f"{API_ROOT}/pins/123456789012345678",
            "allowed_hosts": API_HOSTS,
            "params": None,
            "headers": expected_auth(),
            "retries": 0,
        }
        return pin_payload()

    calls = install_get(monkeypatch, handler)
    record = await PinterestAdapter().read("https://www.pinterest.com/pin/123456789012345678/")

    assert len(calls) == 1
    assert record.platform == "pinterest"
    assert record.type == "pin"
    assert record.id == "123456789012345678"
    assert record.url == "https://www.pinterest.com/pin/123456789012345678/"
    assert record.author == "pin-owner"
    assert record.title == "A saved idea"
    assert record.text == "A short Pin description"
    assert record.media[0].url == IMAGE_URL
    assert record.media[0].metadata == {"width": 1200, "height": 800}
    assert record.access.mode == "authenticated"
    assert record.extensions["metadata_source"] == "pinterest_api_v5"
    assert TOKEN not in repr(record.to_dict())


@pytest.mark.asyncio
async def test_pin_read_rejects_unsupported_urls_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINTEREST_ACCESS_TOKEN", TOKEN)
    calls = install_get(monkeypatch, lambda _call: pytest.fail("invalid URL must not reach Pinterest"))

    for url in (
        "http://www.pinterest.com/pin/123",
        "https://pinterest.com.evil.example/pin/123",
        "https://fake.pinterest.com/pin/123",
        "https://user:pass@www.pinterest.com/pin/123",
        "https://www.pinterest.com/pin/123%2F456",
        "https://www.pinterest.com/pin/not-numeric",
        "https://pin.it/short-code",
    ):
        with pytest.raises(ReachError) as error:
            await PinterestAdapter().read(url)
        assert error.value.code == "invalid_url"

    assert calls == []


@pytest.mark.asyncio
async def test_missing_token_and_unsupported_account_lookup_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PINTEREST_ACCESS_TOKEN", raising=False)
    calls = install_get(monkeypatch, lambda _call: pytest.fail("missing token must not reach Pinterest"))
    adapter = PinterestAdapter()

    with pytest.raises(ReachError) as missing_token:
        await adapter.read("https://www.pinterest.com/pin/123")
    assert missing_token.value.code == "pinterest_auth_required"

    with pytest.raises(ReachError) as wrong_user:
        await adapter.profile("somebody_else")
    assert wrong_user.value.code == "unsupported_profile"

    with pytest.raises(ReachError) as wrong_posts_user:
        await adapter.profile_posts("somebody_else")
    assert wrong_posts_user.value.code == "unsupported_profile"
    assert calls == []


@pytest.mark.asyncio
async def test_profile_reads_only_token_owner_and_maps_account_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINTEREST_ACCESS_TOKEN", TOKEN)

    async def handler(call: dict[str, Any]) -> dict[str, Any]:
        assert call == {
            "url": f"{API_ROOT}/user_account",
            "allowed_hosts": API_HOSTS,
            "params": None,
            "headers": expected_auth(),
            "retries": 0,
        }
        return account_payload()

    calls = install_get(monkeypatch, handler)
    record = await PinterestAdapter().profile("me")

    assert len(calls) == 1
    assert record.id == "7654321"
    assert record.author == "my_account"
    assert record.title == "My Pinterest"
    assert record.text == "Account bio"
    assert record.metrics == {"followers": 12, "following": 34, "boards": 5, "pins": 89, "monthly_views": 1200}
    assert record.media[0].url == "https://i.pinimg.com/profile.jpg"
    assert record.access.mode == "authenticated"


@pytest.mark.asyncio
async def test_profile_posts_uses_official_bookmark_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PINTEREST_ACCESS_TOKEN", TOKEN)

    async def handler(call: dict[str, Any]) -> dict[str, Any]:
        assert call == {
            "url": f"{API_ROOT}/pins",
            "allowed_hosts": API_HOSTS,
            "params": {"page_size": 250, "bookmark": "bookmark-from-prev-page"},
            "headers": expected_auth(),
            "retries": 0,
        }
        return {"items": [pin_payload(), pin_payload("123456789012345679")], "bookmark": "bookmark-next-page"}

    calls = install_get(monkeypatch, handler)
    result = await PinterestAdapter().profile_posts("me", limit=250, cursor="bookmark-from-prev-page")

    assert len(calls) == 1
    assert result.operation == "profile_posts"
    assert result.complete is False
    assert result.pagination.has_more is True
    assert result.pagination.cursor == "bookmark-next-page"
    assert [item.id for item in result.items] == ["123456789012345678", "123456789012345679"]
    assert all(item.access.mode == "authenticated" for item in result.items)
    assert TOKEN not in repr(result.to_dict())


@pytest.mark.asyncio
async def test_invalid_options_limits_cursors_and_bad_token_fail_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PINTEREST_ACCESS_TOKEN", raising=False)
    calls = install_get(monkeypatch, lambda _call: pytest.fail("invalid input must not reach Pinterest"))
    adapter = PinterestAdapter()

    for options in ({"cookies": "not-supported"}, [], "bad"):
        with pytest.raises(ReachError) as error:
            await adapter.read("https://www.pinterest.com/pin/123", options)  # type: ignore[arg-type]
        assert error.value.code == "invalid_option"

    monkeypatch.setenv("PINTEREST_ACCESS_TOKEN", "token with spaces")
    with pytest.raises(ReachError) as bad_token:
        await adapter.profile("me")
    assert bad_token.value.code == "pinterest_auth_invalid"

    monkeypatch.setenv("PINTEREST_ACCESS_TOKEN", TOKEN)
    with pytest.raises(ReachError) as bad_limit:
        await adapter.profile_posts("me", limit=251)
    assert bad_limit.value.code == "invalid_limit"
    with pytest.raises(ReachError) as bad_cursor:
        await adapter.profile_posts("me", cursor=" ")
    assert bad_cursor.value.code == "invalid_cursor"
    assert calls == []


@pytest.mark.parametrize(
    ("http_status", "expected_code"),
    (
        (401, "pinterest_authentication_failed"),
        (403, "pinterest_access_denied"),
        (404, "not_found"),
        (429, "rate_limited"),
    ),
)
@pytest.mark.asyncio
async def test_api_errors_are_mapped_to_precise_codes(
    monkeypatch: pytest.MonkeyPatch,
    http_status: int,
    expected_code: str,
) -> None:
    monkeypatch.setenv("PINTEREST_ACCESS_TOKEN", TOKEN)

    async def failed(_call: dict[str, Any]) -> Any:
        raise ReachError("upstream_error", "upstream error", details={"http_status": http_status})

    install_get(monkeypatch, failed)
    with pytest.raises(ReachError) as error:
        await PinterestAdapter().read("https://www.pinterest.com/pin/123")
    assert error.value.code == expected_code


@pytest.mark.asyncio
async def test_health_check_is_local_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PINTEREST_ACCESS_TOKEN", raising=False)
    install_get(monkeypatch, lambda _call: pytest.fail("health check must not request without a token"))

    no_token = await PinterestAdapter().health_check()
    assert no_token.status == "unavailable"
    assert "no request was sent" in no_token.message


def test_pin_media_urls_are_restricted_to_https_pinimg_hosts() -> None:
    assert pinterest._pinterest_media_url(IMAGE_URL) == IMAGE_URL
    for url in (
        "http://i.pinimg.com/image.jpg",
        "https://pinimg.com.evil.test/image.jpg",
        "https://i.pinimg.com.evil.test/image.jpg",
        "https://user@i.pinimg.com/image.jpg",
        "https://i.pinimg.com:8443/image.jpg",
    ):
        assert pinterest._pinterest_media_url(url) is None
