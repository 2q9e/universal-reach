"""Deterministic tests for anonymous oEmbed and read-only X API access."""

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from universal_reach.adapters import x
from universal_reach.adapters.x import XAdapter
from universal_reach.errors import ReachError


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def install_get(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_get_json(
        url: str,
        *,
        allowed_hosts: frozenset[str],
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 1,
        **kwargs: Any,
    ) -> Any:
        call = {
            "url": url,
            "allowed_hosts": allowed_hosts,
            "params": dict(params or {}),
            "headers": dict(headers or {}),
            "retries": retries,
        }
        calls.append(call)
        return await handler(call)

    monkeypatch.setattr(x, "get_json", fake_get_json)
    return calls


def enable_api(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "test-bearer-token-do-not-print"
    monkeypatch.setenv("X_API_BEARER_TOKEN", token)
    monkeypatch.setenv("X_API_ALLOW_PAID_READS", "true")
    return token


def post_payload(post_id: str = "1234567890") -> dict[str, Any]:
    return {
        "data": {
            "id": post_id,
            "author_id": "42",
            "text": "Public post text",
            "created_at": "2026-11-13T12:00:00.000Z",
            "conversation_id": post_id,
            "lang": "en",
            "public_metrics": {
                "like_count": 7,
                "reply_count": 2,
                "retweet_count": 3,
                "quote_count": 1,
                "bookmark_count": 4,
                "impression_count": 50,
            },
        },
        "includes": {
            "users": [{"id": "42", "username": "Alice_42", "name": "Alice", "verified": True}]
        },
    }


def search_payload(post_id: str = "1234567890") -> dict[str, Any]:
    payload = post_payload(post_id)
    return {
        "data": [payload["data"]],
        "includes": payload["includes"],
        "meta": {"result_count": 1},
    }


def test_adapter_advertises_search_and_read_only() -> None:
    adapter = XAdapter()
    assert adapter.platform == "x"
    assert adapter.capability.operations == ("search", "read")
    assert adapter.capability.anonymous_access == "partial"


def test_search_requires_env_token_and_paid_read_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_get(monkeypatch, lambda _call: pytest.fail("no request should be sent"))
    token = enable_api(monkeypatch)
    monkeypatch.setenv("X_API_ALLOW_PAID_READS", "false")

    with pytest.raises(ReachError) as caught:
        run(XAdapter().search("hello world"))

    assert caught.value.code == "x_paid_read_opt_in_required"
    assert "no request was sent" in caught.value.message
    assert token not in str(caught.value)
    assert calls == []


def test_missing_token_fails_closed_without_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_API_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("X_API_ALLOW_PAID_READS", "true")
    calls = install_get(monkeypatch, lambda _call: pytest.fail("no request should be sent"))

    with pytest.raises(ReachError) as caught:
        run(XAdapter().search("hello world"))

    assert caught.value.code == "x_auth_required"
    assert calls == []


def test_search_uses_official_recent_endpoint_and_manual_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    token = enable_api(monkeypatch)

    async def handler(call: dict[str, Any]) -> dict[str, Any]:
        assert call["url"] == "https://api.x.com/2/tweets/search/recent"
        assert call["allowed_hosts"] == frozenset({"api.x.com"})
        assert call["headers"] == {"Authorization": f"Bearer {token}"}
        assert call["retries"] == 0
        assert call["params"]["query"] == "public science"
        assert call["params"]["max_results"] == 10
        assert call["params"]["pagination_token"] == "previous-page-token"
        assert "post.fields" in call["params"]
        assert call["params"]["expansions"] == "author_id"
        assert call["params"]["user.fields"] == "username,name,verified"
        return {**search_payload(), "meta": {"next_token": "next-page-token", "result_count": 1}}

    calls = install_get(monkeypatch, handler)
    result = run(XAdapter().search("public science", limit=10, cursor="previous-page-token"))

    assert len(calls) == 1
    assert result.operation == "search"
    assert result.complete is False
    assert result.pagination.has_more is True
    assert result.pagination.cursor == "next-page-token"
    assert result.extensions["result_count"] == 1
    assert len(result.items) == 1
    item = result.items[0]
    assert item.id == "1234567890"
    assert item.url == "https://x.com/Alice_42/status/1234567890"
    assert item.author == "@Alice_42"
    assert item.text == "Public post text"
    assert item.metrics["like_count"] == 7
    assert item.metrics["retweet_count"] == 3
    assert item.extensions["conversation_id"] == "1234567890"
    assert item.access.mode == "authenticated"


def test_search_rejects_unsupported_low_limit_and_bad_query(monkeypatch: pytest.MonkeyPatch) -> None:
    install_get(monkeypatch, lambda _call: pytest.fail("validation must happen before a request"))
    enable_api(monkeypatch)

    with pytest.raises(ReachError) as low_limit:
        run(XAdapter().search("hello", limit=5))
    assert low_limit.value.code == "invalid_limit"
    assert low_limit.value.details["minimum"] == 10

    with pytest.raises(ReachError) as bad_query:
        run(XAdapter().search("  "))
    assert bad_query.value.code == "invalid_query"


def test_read_api_mode_uses_only_the_id_from_a_supported_x_url(monkeypatch: pytest.MonkeyPatch) -> None:
    token = enable_api(monkeypatch)

    async def handler(call: dict[str, Any]) -> dict[str, Any]:
        assert call["url"] == "https://api.x.com/2/tweets/1234567890"
        assert call["allowed_hosts"] == frozenset({"api.x.com"})
        assert call["headers"] == {"Authorization": f"Bearer {token}"}
        assert call["retries"] == 0
        assert "tweet.fields" not in call["params"]
        assert "post.fields" in call["params"]
        return post_payload()

    calls = install_get(monkeypatch, handler)
    record = run(XAdapter().read("https://twitter.com/alice/status/1234567890?s=20#ignored", {"auth_mode": "api"}))

    assert len(calls) == 1
    assert record.id == "1234567890"
    assert record.platform == "x"
    assert record.type == "post"
    assert record.access.mode == "authenticated"


def test_read_defaults_to_public_oembed_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_API_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("X_API_ALLOW_PAID_READS", raising=False)

    async def handler(call: dict[str, Any]) -> dict[str, Any]:
        assert call["url"] == x.OEMBED_URL
        assert call["allowed_hosts"] == x.OEMBED_HOSTS
        assert call["params"] == {
            "url": "https://x.com/i/web/status/1234567890",
            "omit_script": "true",
            "hide_thread": "true",
        }
        assert call["headers"] == {}
        assert call["retries"] == 0
        return {
            "type": "rich",
            "provider_name": "Twitter",
            "url": "https://twitter.com/Alice_42/status/1234567890",
            "author_name": "Alice",
            "author_url": "https://twitter.com/Alice_42",
            "html": '<blockquote><p>Public &amp; safe<br>next<script>secret()</script></p></blockquote><script>omit()</script>',
        }

    calls = install_get(monkeypatch, handler)
    record = run(XAdapter().read("https://x.com/alice/status/1234567890"))

    assert len(calls) == 1
    assert record.id == "1234567890"
    assert record.url == "https://x.com/i/web/status/1234567890"
    assert record.author == "@Alice_42"
    assert record.text == "Public & safe\nnext"
    assert record.access.mode == "anonymous"
    assert record.access.complete is False
    assert record.extensions == {"metadata_source": "x_oembed", "author_handle": "Alice_42"}
    assert "script" not in repr(record.to_dict())


def test_anonymous_oembed_rejects_mismatched_response_post(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_get(
        monkeypatch,
        lambda _call: asyncio.sleep(0, result={
            "type": "rich",
            "url": "https://x.com/other/status/999",
            "author_name": "Other",
            "html": "<blockquote><p>Wrong post</p></blockquote>",
        }),
    )

    with pytest.raises(ReachError) as caught:
        run(XAdapter().read("https://x.com/alice/status/123"))

    assert caught.value.code == "parser_error"
    assert len(calls) == 1


def test_read_options_select_paid_api_explicitly_and_reject_unknown_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_get(monkeypatch, lambda _call: pytest.fail("invalid mode must not make a request"))

    with pytest.raises(ReachError) as invalid_mode:
        run(XAdapter().read("https://x.com/alice/status/123", {"auth_mode": "browser"}))
    assert invalid_mode.value.code == "invalid_option"

    with pytest.raises(ReachError) as unknown_option:
        run(XAdapter().read("https://x.com/alice/status/123", {"cookies": "never"}))
    assert unknown_option.value.code == "invalid_option"
    assert calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://x.com/alice/status/123",
        "https://x.com.evil.example/alice/status/123",
        "https://user@x.com/alice/status/123",
        "https://x.com//alice/status/123",
        "https://x.com/alice//status/123",
        "https://x.com/alice/status/123/../456",
        "https://x.com/alice/status/not-an-id",
        "https://x.com/i/web/status/123%2F456",
        "https://x.com/alice/status/123:8443",
    ],
)
def test_read_rejects_unsupported_or_malformed_urls(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    enable_api(monkeypatch)
    install_get(monkeypatch, lambda _call: pytest.fail("invalid URL must not make a request"))

    with pytest.raises(ReachError) as caught:
        run(XAdapter().read(url))
    assert caught.value.code == "invalid_url"


def test_partial_search_response_keeps_results_and_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_api(monkeypatch)

    async def handler(_call: dict[str, Any]) -> dict[str, Any]:
        return {
            **search_payload(),
            "errors": [{"message": "one result was withheld"}],
            "meta": {"next_token": "next"},
        }

    install_get(monkeypatch, handler)
    result = run(XAdapter().search("hello"))

    assert len(result.items) == 1
    assert result.complete is False
    assert result.pagination.has_more is True
    assert result.warnings == ["X returned partial-result errors; some search results may be missing."]


def test_search_api_error_without_results_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_api(monkeypatch)

    async def handler(_call: dict[str, Any]) -> dict[str, Any]:
        return {"errors": [{"message": "private upstream detail"}]}

    install_get(monkeypatch, handler)
    with pytest.raises(ReachError) as caught:
        run(XAdapter().search("hello"))

    assert caught.value.code == "x_api_error"
    assert caught.value.details == {"error_count": 1}
    assert "private upstream detail" not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [(401, "x_authentication_failed", False), (403, "x_access_denied", False), (402, "x_credits_required", False), (429, "rate_limited", True)],
)
def test_http_auth_credit_and_rate_limit_errors_are_clear_and_secret_safe(
    status: int,
    code: str,
    retryable: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = enable_api(monkeypatch)

    async def fake_get_json(*args: Any, **kwargs: Any) -> Any:
        raise ReachError("upstream_error", "upstream response detail", retryable=retryable, details={"http_status": status})

    monkeypatch.setattr(x, "get_json", fake_get_json)
    with pytest.raises(ReachError) as caught:
        run(XAdapter().search("hello"))

    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert caught.value.details["http_status"] == status
    assert token not in str(caught.value)
    assert "upstream response detail" not in str(caught.value)


def test_health_check_never_makes_a_live_call(monkeypatch: pytest.MonkeyPatch) -> None:
    token = enable_api(monkeypatch)
    calls = install_get(monkeypatch, lambda _call: pytest.fail("health check must not call X"))

    status = run(XAdapter().health_check())

    assert status.status == "degraded"
    assert status.access_mode == "authenticated"
    assert "no live API call was made" in status.message
    assert token not in status.message
    assert calls == []


def test_health_check_reports_anonymous_read_mode_without_api_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_API_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("X_API_ALLOW_PAID_READS", raising=False)
    calls = install_get(monkeypatch, lambda _call: pytest.fail("health check must not call X"))

    status = run(XAdapter().health_check())

    assert status.status == "degraded"
    assert status.access_mode == "anonymous"
    assert "Anonymous exact-post reads use X oEmbed" in status.message
    assert calls == []
