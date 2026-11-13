from __future__ import annotations

from typing import Any

import pytest

from universal_reach.adapters import meta_graph, meta_facebook, meta_instagram, threads
from universal_reach.adapters.meta_facebook import FacebookPageAdapter
from universal_reach.adapters.meta_instagram import InstagramProfessionalAdapter
from universal_reach.adapters.threads import ThreadsAdapter
from universal_reach.errors import ReachError, UnsupportedOperation


PAGE_ID = "123456789012345"
IG_ID = "17841400008460056"
THREADS_ID = "987654321012345"
POST_ID = "987654321012346"


def _page_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_FACEBOOK_PAGE_ID", PAGE_ID)
    monkeypatch.setenv("META_FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")
    monkeypatch.setenv("META_FACEBOOK_PAGE_USERNAME", "examplepage")


def _instagram_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_INSTAGRAM_USER_ID", IG_ID)
    monkeypatch.setenv("META_INSTAGRAM_ACCESS_TOKEN", "test-instagram-token")
    monkeypatch.setenv("META_INSTAGRAM_USERNAME", "example.creator")


def _threads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_THREADS_ACCESS_TOKEN", "test-threads-token")
    monkeypatch.setenv("META_THREADS_USER_ID", THREADS_ID)
    monkeypatch.setenv("META_THREADS_USERNAME", "example.creator")


@pytest.mark.asyncio
async def test_facebook_reads_only_configured_pages_published_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    _page_env(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((url, kwargs))
        if url.endswith("/me"):
            return {"id": PAGE_ID}
        return {
            "data": [
                {
                    "id": "page_post_1",
                    "message": "Page announcement",
                    "created_time": "2026-11-12T12:00:00+0000",
                    "permalink_url": "https://www.facebook.com/examplepage/posts/1",
                    "full_picture": "https://scontent.example-cdn.test/image.jpg",
                    "shares": {"count": 3},
                    "is_published": True,
                    "from": {"name": "Example Page"},
                },
                {"id": "page_post_2", "message": "Older"},
            ],
            "paging": {"cursors": {"after": "after-token"}, "next": "https://graph.facebook.com/next"},
        }

    monkeypatch.setattr(meta_graph, "get_json", fake_get_json)
    adapter = FacebookPageAdapter()
    result = await adapter.profile_posts("@examplepage", limit=1)

    assert adapter.capability.status == "restricted"
    assert adapter.capability.operations == ("profile_posts",)
    assert calls[0][0] == f"{meta_facebook.META_GRAPH_ROOT}/me"
    assert calls[0][1]["allowed_hosts"] == frozenset({"graph.facebook.com"})
    assert calls[0][1]["headers"] == {"Authorization": "Bearer test-page-token"}
    assert calls[0][1]["params"] == {"fields": "id"}
    assert calls[1][0] == f"{meta_facebook.META_GRAPH_ROOT}/{PAGE_ID}/published_posts"
    assert calls[1][1]["params"] == {
        "fields": "id,message,created_time,permalink_url,full_picture,shares,is_published,from",
        "limit": 1,
    }
    assert result.operation == "profile_posts"
    assert result.complete is False
    assert result.pagination.cursor == "after-token"
    assert result.pagination.has_more is True
    assert len(result.items) == 1
    assert result.items[0].text == "Page announcement"
    assert result.items[0].author == "Example Page"
    assert result.items[0].metrics == {"shares": 3}
    assert result.items[0].access.mode == "authenticated"
    assert any("600 ranked" in warning for warning in result.warnings)
    with pytest.raises(UnsupportedOperation):
        await adapter.search("anything")


@pytest.mark.asyncio
async def test_facebook_missing_configuration_and_account_mismatch_make_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected_get_json(url: str, **kwargs: Any) -> Any:
        pytest.fail("configuration and account checks must happen before a request")

    monkeypatch.setattr(meta_graph, "get_json", unexpected_get_json)
    monkeypatch.delenv("META_FACEBOOK_PAGE_ID", raising=False)
    with pytest.raises(ReachError) as missing:
        await FacebookPageAdapter().profile_posts(PAGE_ID)
    assert missing.value.code == "authentication_required"

    _page_env(monkeypatch)
    with pytest.raises(ReachError) as mismatch:
        await FacebookPageAdapter().profile_posts("some-other-page")
    assert mismatch.value.code == "account_mismatch"


@pytest.mark.asyncio
async def test_facebook_page_id_must_match_page_token_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    _page_env(monkeypatch)
    calls: list[str] = []

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(url)
        return {"id": "111111111111111"}

    monkeypatch.setattr(meta_graph, "get_json", fake_get_json)
    with pytest.raises(ReachError) as caught:
        await FacebookPageAdapter().profile_posts(PAGE_ID)
    assert caught.value.code == "account_mismatch"
    assert calls == [f"{meta_facebook.META_GRAPH_ROOT}/me"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("graph_code", "expected_code"),
    [(190, "authentication_required"), (200, "permission_required"), (80001, "rate_limited")],
)
async def test_facebook_maps_graph_errors_without_returning_upstream_messages(
    monkeypatch: pytest.MonkeyPatch, graph_code: int, expected_code: str
) -> None:
    _page_env(monkeypatch)

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        return {"error": {"code": graph_code, "message": "secret details must not leak"}}

    monkeypatch.setattr(meta_graph, "get_json", fake_get_json)
    with pytest.raises(ReachError) as caught:
        await FacebookPageAdapter().profile_posts(PAGE_ID)
    assert caught.value.code == expected_code
    assert "secret details" not in caught.value.message
    if graph_code == 80001:
        assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_instagram_reads_owner_profile_and_normalizes_professional_media(monkeypatch: pytest.MonkeyPatch) -> None:
    _instagram_env(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((url, kwargs))
        if url.endswith(f"/{IG_ID}"):
            return {
                "id": IG_ID,
                "username": "example.creator",
                "name": "Example Creator",
                "biography": "A professional creator account",
                "website": "https://creator.example",
                "followers_count": 12,
                "profile_picture_url": "https://scontent.example-cdn.test/avatar.jpg",
            }
        return {
            "data": [
                {
                    "id": "ig_media_1",
                    "caption": "New reel",
                    "media_type": "VIDEO",
                    "media_url": None,
                    "thumbnail_url": "https://scontent.example-cdn.test/thumbnail.jpg",
                    "permalink": "https://www.instagram.com/reel/abc123/",
                    "timestamp": "2026-11-12T15:00:00+0000",
                    "username": "example.creator",
                    "like_count": 4,
                    "comments_count": 2,
                }
            ],
            "paging": {"cursors": {"after": "ig-next"}},
        }

    monkeypatch.setattr(meta_graph, "get_json", fake_get_json)
    adapter = InstagramProfessionalAdapter()
    profile = await adapter.profile("example.creator")
    posts = await adapter.profile_posts("@example.creator", limit=1)

    assert adapter.capability.status == "restricted"
    assert adapter.capability.operations == ("profile", "profile_posts")
    assert profile.id == IG_ID
    assert profile.author == "@example.creator"
    assert profile.text == "A professional creator account"
    assert profile.metrics["followers"] == 12
    assert posts.items[0].id == "ig_media_1"
    assert posts.items[0].text == "New reel"
    assert posts.items[0].media[0].kind == "video"
    assert posts.items[0].media[0].url.endswith("thumbnail.jpg")
    assert posts.items[0].metrics == {"likes": 4, "comments": 2}
    assert posts.pagination.cursor == "ig-next"
    assert posts.complete is False
    assert calls[0][1]["params"]["fields"] == meta_instagram._PROFILE_FIELDS
    assert calls[1][0] == f"{meta_instagram.META_GRAPH_ROOT}/{IG_ID}/media"
    assert calls[1][1]["headers"] == {"Authorization": "Bearer test-instagram-token"}
    assert any("10,000" in warning for warning in posts.warnings)


@pytest.mark.asyncio
async def test_instagram_rejects_unconfigured_accounts_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _instagram_env(monkeypatch)

    async def unexpected_get_json(url: str, **kwargs: Any) -> Any:
        pytest.fail("external Instagram profile inputs must be rejected before a request")

    monkeypatch.setattr(meta_graph, "get_json", unexpected_get_json)
    with pytest.raises(ReachError) as caught:
        await InstagramProfessionalAdapter().profile_posts("another.creator")
    assert caught.value.code == "account_mismatch"


@pytest.mark.asyncio
async def test_threads_profile_posts_use_only_me_and_after_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    _threads_env(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((url, kwargs))
        if url.endswith("/me"):
            return {
                "id": THREADS_ID,
                "username": "example.creator",
                "name": "Example Creator",
                "threads_biography": "Threads profile bio",
                "threads_profile_picture_url": "https://scontent.example-cdn.test/avatar.jpg",
            }
        return {
            "data": [
                {
                    "id": POST_ID,
                    "media_product_type": "THREADS",
                    "media_type": "TEXT_POST",
                    "permalink": "https://www.threads.com/@example.creator/post/shortcode1",
                    "owner": {"id": THREADS_ID},
                    "username": "example.creator",
                    "text": "Owner-authored post",
                    "timestamp": "2026-11-12T15:00:00+0000",
                    "shortcode": "shortcode1",
                    "has_replies": True,
                    "is_quote_post": False,
                }
            ],
            "paging": {"cursors": {"after": "threads-next"}},
        }

    monkeypatch.setattr(meta_graph, "get_json", fake_get_json)
    adapter = ThreadsAdapter()
    profile = await adapter.profile("example.creator")
    result = await adapter.profile_posts("@example.creator", limit=1, cursor="previous")

    assert adapter.capability.status == "restricted"
    assert adapter.capability.operations == ("read", "profile", "profile_posts", "replies")
    assert profile.id == THREADS_ID
    assert profile.text == "Threads profile bio"
    assert profile.author == "@example.creator"
    assert result.items[0].text == "Owner-authored post"
    assert result.items[0].url.endswith("/shortcode1")
    assert result.items[0].access.mode == "authenticated"
    assert result.pagination.cursor == "threads-next"
    assert result.pagination.has_more is True
    assert calls[0][0] == f"{threads.THREADS_GRAPH_ROOT}/me"
    assert calls[2][0] == f"{threads.THREADS_GRAPH_ROOT}/me/threads"
    assert calls[2][1]["params"]["after"] == "previous"
    assert calls[2][1]["headers"] == {"Authorization": "Bearer test-threads-token"}


@pytest.mark.asyncio
async def test_threads_read_and_replies_verify_owned_post_before_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    _threads_env(monkeypatch)
    calls: list[str] = []

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(url)
        if url.endswith("/me"):
            return {"id": THREADS_ID, "username": "example.creator"}
        if url.endswith(f"/{POST_ID}"):
            return {
                "id": POST_ID,
                "owner": {"id": THREADS_ID},
                "username": "example.creator",
                "shortcode": "shortcode1",
                "text": "Owner post",
                "permalink": "https://www.threads.net/@example.creator/post/shortcode1",
                "media_type": "TEXT_POST",
            }
        return {
            "data": [
                {
                    "id": "reply_1",
                    "text": "Reply text",
                    "timestamp": "2026-11-13T08:00:00+0000",
                    "permalink": "https://www.threads.net/@responder/post/replyshort",
                    "username": "responder",
                    "is_reply": True,
                    "root_post": {"id": POST_ID},
                    "replied_to": {"id": POST_ID},
                }
            ],
            "paging": {},
        }

    monkeypatch.setattr(meta_graph, "get_json", fake_get_json)
    adapter = ThreadsAdapter()
    post = await adapter.read(
        "https://www.threads.com/@example.creator/post/shortcode1",
        options={"thread_id": POST_ID},
    )
    reply_page = await adapter.replies(
        post.url or "",
        options={"thread_id": POST_ID},
    )

    assert post.id == POST_ID
    assert post.text == "Owner post"
    assert reply_page.items[0].type == "reply"
    assert reply_page.items[0].text == "Reply text"
    assert reply_page.extensions["scope"] == "authorized_user_owned_post_replies"
    assert calls.count(f"{threads.THREADS_GRAPH_ROOT}/{POST_ID}") == 2
    assert calls[-1] == f"{threads.THREADS_GRAPH_ROOT}/{POST_ID}/replies"


@pytest.mark.asyncio
async def test_threads_rejects_other_hosts_unowned_posts_and_missing_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _threads_env(monkeypatch)
    calls: list[str] = []

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(url)
        if url.endswith("/me"):
            return {"id": THREADS_ID, "username": "example.creator"}
        return {"id": POST_ID, "owner": {"id": "111111111111111"}, "username": "other"}

    monkeypatch.setattr(meta_graph, "get_json", fake_get_json)
    adapter = ThreadsAdapter()
    with pytest.raises(ReachError) as bad_host:
        await adapter.read("https://threads.invalid/@example.creator/post/shortcode", options={"thread_id": POST_ID})
    assert bad_host.value.code == "invalid_url"
    assert calls == []

    with pytest.raises(ReachError) as missing_id:
        await adapter.read("https://www.threads.com/@example.creator/post/shortcode")
    assert missing_id.value.code == "invalid_request"
    assert calls == []

    with pytest.raises(ReachError) as other_owner:
        await adapter.replies(
            "https://www.threads.com/@example.creator/post/shortcode",
            options={"thread_id": POST_ID},
        )
    assert other_owner.value.code == "private_or_inaccessible"
    assert calls[-1] == f"{threads.THREADS_GRAPH_ROOT}/{POST_ID}"
    assert not any(url.endswith("/replies") for url in calls)


@pytest.mark.asyncio
async def test_threads_rejects_api_id_that_does_not_match_url_shortcode(monkeypatch: pytest.MonkeyPatch) -> None:
    _threads_env(monkeypatch)

    async def fake_get_json(url: str, **kwargs: Any) -> dict[str, Any]:
        if url.endswith("/me"):
            return {"id": THREADS_ID, "username": "example.creator"}
        return {"id": POST_ID, "owner": {"id": THREADS_ID}, "shortcode": "actual-shortcode"}

    monkeypatch.setattr(meta_graph, "get_json", fake_get_json)
    with pytest.raises(ReachError) as caught:
        await ThreadsAdapter().read(
            "https://www.threads.com/@example.creator/post/different-shortcode",
            options={"thread_id": POST_ID},
        )

    assert caught.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_threads_invalid_account_and_pagination_do_not_call_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    _threads_env(monkeypatch)

    async def unexpected_get_json(url: str, **kwargs: Any) -> Any:
        pytest.fail("invalid Threads input must not reach Meta")

    monkeypatch.setattr(meta_graph, "get_json", unexpected_get_json)
    adapter = ThreadsAdapter()
    with pytest.raises(ReachError) as bad_username:
        await adapter.profile_posts("bad username", limit=1)
    assert bad_username.value.code == "invalid_request"
    with pytest.raises(ReachError) as bad_limit:
        await adapter.profile_posts("example.creator", limit=0)
    assert bad_limit.value.code == "invalid_limit"
    with pytest.raises(ReachError) as bad_cursor:
        await adapter.replies(
            "https://www.threads.com/@example.creator/post/shortcode",
            cursor="bad cursor",
            options={"thread_id": POST_ID},
        )
    assert bad_cursor.value.code == "invalid_cursor"
