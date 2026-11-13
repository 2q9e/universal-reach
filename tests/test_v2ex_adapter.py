"""Deterministic tests for the read-only V2EX API adapter."""

from __future__ import annotations

import os
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from universal_reach.adapters import v2ex
from universal_reach.adapters.v2ex import TOKEN_ENV, V2EXAdapter
from universal_reach.errors import ReachError, UnsupportedOperation


class FakeApi:
    def __init__(self, payload: Any = None, error: ReachError | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.payload


class V2EXAdapterTests(IsolatedAsyncioTestCase):
    async def test_topic_read_requires_opt_in_token_without_network_call(self) -> None:
        api = FakeApi(payload={"id": 42, "title": "A topic"})
        with patch.dict(os.environ, {TOKEN_ENV: ""}), patch.object(v2ex, "get_json", api):
            with self.assertRaises(ReachError) as caught:
                await V2EXAdapter().read("https://www.v2ex.com/t/42")

        self.assertEqual(caught.exception.code, "authentication_required")
        self.assertEqual(api.calls, [])
        self.assertNotIn(TOKEN_ENV, str(caught.exception.details.get("token", "")))

    async def test_topic_read_uses_bearer_token_and_normalizes_topic(self) -> None:
        secret = "local-test-token-do-not-reflect"
        api = FakeApi(
            {
                "id": 42,
                "title": "A topic",
                "content": "Topic body",
                "content_rendered": "<p>Topic body</p>",
                "created": 1_700_000_000,
                "replies": 3,
                "member": {"username": "Ada"},
                "node": {"name": "python"},
            }
        )
        with patch.dict(os.environ, {TOKEN_ENV: secret}), patch.object(v2ex, "get_json", api):
            result = await V2EXAdapter().read("https://v2ex.com/t/42/a-topic")

        call = api.calls[0]
        self.assertTrue(call["url"].endswith("/api/v2/topics/42"))
        self.assertEqual(call["headers"], {"Authorization": f"Bearer {secret}"})
        self.assertEqual(call["allowed_hosts"], frozenset({"www.v2ex.com"}))
        self.assertEqual(result.platform, "v2ex")
        self.assertEqual(result.type, "topic")
        self.assertEqual(result.id, "42")
        self.assertEqual(result.title, "A topic")
        self.assertEqual(result.text, "Topic body")
        self.assertEqual(result.author, "Ada")
        self.assertEqual(result.metrics["replies"], 3)
        self.assertEqual(result.created_at, "2023-11-14T22:13:20Z")
        self.assertEqual(result.access.mode, "authenticated")
        self.assertNotIn(secret, str(result.to_dict()))

    async def test_replies_use_documented_page_parameter_and_report_unknown_total(self) -> None:
        secret = "local-test-token"
        api = FakeApi(
            [
                {"id": 7, "content": "First reply", "created": 1_700_000_001, "member": {"username": "Grace"}},
                {"id": 8, "content": "Second reply", "thank": 2},
                {"id": 9, "content": "Third reply"},
            ]
        )
        with patch.dict(os.environ, {TOKEN_ENV: secret}), patch.object(v2ex, "get_json", api):
            page = await V2EXAdapter().replies(
                "https://www.v2ex.com/t/42",
                limit=2,
                cursor="3",
            )

        call = api.calls[0]
        self.assertTrue(call["url"].endswith("/api/v2/topics/42/replies"))
        self.assertEqual(call["params"], {"p": 3})
        self.assertEqual([reply.id for reply in page.items], ["7", "8"])
        self.assertEqual(page.items[0].author, "Grace")
        self.assertEqual(page.items[1].metrics["thanks"], 2)
        self.assertEqual(page.pagination.cursor, "4")
        self.assertTrue(page.pagination.has_more)
        self.assertFalse(page.complete)
        self.assertTrue(any("no total count or has_more" in warning for warning in page.warnings))
        self.assertTrue(any("local limit" in warning for warning in page.warnings))
        self.assertNotIn(secret, str(page.to_dict()))

    async def test_empty_reply_page_is_the_only_documented_completion_signal(self) -> None:
        api = FakeApi([])
        with patch.dict(os.environ, {TOKEN_ENV: "local-test-token"}), patch.object(v2ex, "get_json", api):
            page = await V2EXAdapter().replies("https://www.v2ex.com/t/42", cursor="2")

        self.assertEqual(api.calls[0]["params"], {"p": 2})
        self.assertEqual(page.items, [])
        self.assertIsNone(page.pagination.cursor)
        self.assertFalse(page.pagination.has_more)
        self.assertTrue(page.complete)

    async def test_expired_token_api_wrapper_is_sanitized(self) -> None:
        secret = "local-test-token-do-not-reflect"
        api = FakeApi({"success": False, "message": "Token expired"})
        with patch.dict(os.environ, {TOKEN_ENV: secret}), patch.object(v2ex, "get_json", api):
            with self.assertRaises(ReachError) as caught:
                await V2EXAdapter().read("https://www.v2ex.com/t/42")

        self.assertEqual(caught.exception.code, "authentication_required")
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, str(caught.exception.to_dict()))

    async def test_http_auth_error_is_mapped_without_reflecting_token(self) -> None:
        secret = "local-test-token-do-not-reflect"
        api = FakeApi(error=ReachError("private_or_inaccessible", "no access", details={"http_status": 401}))
        with patch.dict(os.environ, {TOKEN_ENV: secret}), patch.object(v2ex, "get_json", api):
            with self.assertRaises(ReachError) as caught:
                await V2EXAdapter().read("https://www.v2ex.com/t/42")

        self.assertEqual(caught.exception.code, "authentication_required")
        self.assertNotIn(secret, str(caught.exception.to_dict()))

    async def test_health_check_uses_public_profile_route(self) -> None:
        api = FakeApi({"status": "found", "id": 9, "username": "V2EX"})
        with patch.object(v2ex, "get_json", api):
            result = await V2EXAdapter().health_check()
        self.assertEqual(result.status, "available")
        self.assertEqual(result.access_mode, "anonymous")
        self.assertTrue(api.calls[0]["url"].endswith("/api/members/show.json"))

    async def test_public_profile_uses_legacy_documented_route_without_token(self) -> None:
        api = FakeApi(
            {
                "status": "found",
                "id": 9,
                "username": "Grace",
                "url": "https://www.v2ex.com/member/Grace",
                "website": "https://example.com",
                "location": "London",
                "tagline": "Compiler engineer",
                "bio": "A public bio",
            }
        )
        with patch.dict(os.environ, {TOKEN_ENV: ""}), patch.object(v2ex, "get_json", api):
            profile = await V2EXAdapter().profile("Grace")

        call = api.calls[0]
        self.assertTrue(call["url"].endswith("/api/members/show.json"))
        self.assertEqual(call["params"], {"username": "Grace"})
        self.assertNotIn("headers", call)
        self.assertEqual(profile.type, "profile")
        self.assertEqual(profile.id, "9")
        self.assertEqual(profile.author, "Grace")
        self.assertEqual(profile.title, "Compiler engineer")
        self.assertEqual(profile.text, "A public bio")
        self.assertEqual(profile.access.mode, "anonymous")

    async def test_search_and_profile_posts_are_not_claimed(self) -> None:
        adapter = V2EXAdapter()
        with self.assertRaises(UnsupportedOperation):
            await adapter.search("keyword")
        with self.assertRaises(UnsupportedOperation):
            await adapter.profile_posts("Grace")
        self.assertEqual(adapter.capability.operations, ("read", "replies", "profile"))

    async def test_oversized_cursors_and_upstream_counts_are_bounded(self) -> None:
        with self.assertRaises(ReachError) as caught:
            await V2EXAdapter().replies(
                "https://www.v2ex.com/t/42",
                cursor="9" * 5000,
            )
        self.assertEqual(caught.exception.code, "invalid_cursor")
        self.assertIsNone(V2EXAdapter._count("9" * 5000))
        with self.assertRaises(ReachError) as caught:
            await V2EXAdapter().replies("https://www.v2ex.com/t/42", cursor="100001")
        self.assertEqual(caught.exception.code, "invalid_cursor")

    async def test_invalid_topic_url_is_rejected_before_token_lookup(self) -> None:
        api = FakeApi(payload={})
        with patch.dict(os.environ, {TOKEN_ENV: ""}), patch.object(v2ex, "get_json", api):
            with self.assertRaises(ReachError) as caught:
                await V2EXAdapter().read("https://attacker.example/t/42")

        self.assertEqual(caught.exception.code, "invalid_url")
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
