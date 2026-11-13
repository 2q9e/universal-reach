"""Deterministic tests for the anonymous Stack Exchange API adapter."""

from __future__ import annotations

from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from universal_reach.adapters import stackexchange
from universal_reach.adapters.stackexchange import StackExchangeAdapter
from universal_reach.errors import ReachError


def question(question_id: int = 42, **extra: Any) -> dict[str, Any]:
    return {
        "question_id": question_id,
        "title": "A public question",
        "body": "<p>Question body</p>",
        "link": f"https://stackoverflow.com/questions/{question_id}/a-public-question",
        "creation_date": 1_700_000_000,
        "score": 5,
        "view_count": 123,
        "answer_count": 1,
        "owner": {"display_name": "Ada"},
        "tags": ["python"],
        **extra,
    }


class FakeApi:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"url": url, **kwargs})
        return self.handler(url, kwargs)


class StackExchangeAdapterTests(IsolatedAsyncioTestCase):
    async def test_search_uses_v23_site_paging_and_normalizes_questions(self) -> None:
        api = FakeApi(lambda _url, _kwargs: {"items": [question()], "has_more": True, "quota_remaining": 299})
        adapter = StackExchangeAdapter(platform="stackoverflow")

        with patch.object(stackexchange, "get_json", api):
            result = await adapter.search("asyncio", limit=500, cursor="2", options={"tagged": ["python", "asyncio"]})

        call = api.calls[0]
        self.assertTrue(call["url"].endswith("/2.3/search/advanced"))
        self.assertEqual(call["params"]["site"], "stackoverflow")
        self.assertEqual(call["params"]["q"], "asyncio")
        self.assertEqual(call["params"]["page"], 2)
        self.assertEqual(call["params"]["pagesize"], 100)
        self.assertEqual(call["params"]["tagged"], "python;asyncio")
        self.assertEqual(call["headers"], {"Accept-Encoding": "gzip"})
        self.assertTrue(call["assume_gzip"])
        self.assertEqual(call["json_error_statuses"], frozenset({400}))
        self.assertNotIn("key", call["params"])
        self.assertNotIn("access_token", call["params"])
        self.assertEqual(result.items[0].platform, "stackoverflow")
        self.assertEqual(result.items[0].id, "42")
        self.assertEqual(result.items[0].author, "Ada")
        self.assertEqual(result.items[0].text, "<p>Question body</p>")
        self.assertEqual(result.pagination.cursor, "3")
        self.assertFalse(result.complete)

    async def test_generic_site_requires_explicit_site_and_read_checks_host(self) -> None:
        adapter = StackExchangeAdapter(platform="stackexchange")
        with self.assertRaises(ReachError) as missing:
            await adapter.search("query")
        self.assertEqual(missing.exception.code, "invalid_input")

        api = FakeApi(lambda _url, _kwargs: {"items": [question()]})
        with patch.object(stackexchange, "get_json", api):
            record = await adapter.read("https://math.stackexchange.com/questions/42/example", options={"site": "math"})
        self.assertEqual(api.calls[0]["params"]["site"], "math")
        self.assertEqual(record.platform, "stackexchange")

        with self.assertRaises(ReachError):
            await adapter.read("https://math.stackexchange.com/questions/42/example", options={"site": "askubuntu"})

    async def test_read_question_uses_api_and_preserves_source_url(self) -> None:
        api = FakeApi(lambda _url, _kwargs: {"items": [question()]})
        adapter = StackExchangeAdapter()
        source_url = "https://stackoverflow.com/q/42"

        with patch.object(stackexchange, "get_json", api):
            record = await adapter.read(source_url)

        self.assertTrue(api.calls[0]["url"].endswith("/2.3/questions/42"))
        self.assertEqual(api.calls[0]["params"], {"site": "stackoverflow", "filter": "withbody"})
        self.assertEqual(record.url, source_url)
        self.assertEqual(record.extensions["api_link"], question()["link"])
        self.assertEqual(record.created_at, "2023-11-14T22:13:20Z")
        self.assertTrue(record.access.complete)

    async def test_replies_include_answers_question_comments_and_answer_comments(self) -> None:
        def handler(url: str, _kwargs: dict[str, Any]) -> dict[str, Any]:
            if url.endswith("/questions/42/answers"):
                return {"items": [{"answer_id": 7, "question_id": 42, "body": "Answer body", "link": "https://stackoverflow.com/a/7", "creation_date": 1_700_000_100, "owner": {"display_name": "Grace"}}], "has_more": False}
            if url.endswith("/questions/42/comments"):
                return {"items": [{"comment_id": 50, "post_id": 42, "body": "Question comment", "creation_date": 1_700_000_200, "owner": {"display_name": "Lin"}}], "has_more": False}
            if url.endswith("/answers/7/comments"):
                return {"items": [{"comment_id": 51, "post_id": 7, "body": "Answer comment", "creation_date": 1_700_000_300, "owner": {"display_name": "Edsger"}}], "has_more": False}
            raise AssertionError(f"Unexpected endpoint: {url}")

        api = FakeApi(handler)
        adapter = StackExchangeAdapter()
        with patch.object(stackexchange, "get_json", api):
            result = await adapter.replies("https://stackoverflow.com/questions/42/example")

        self.assertEqual([item.type for item in result.items], ["answer", "comment"])
        self.assertEqual(result.items[0].replies[0].id, "51")
        self.assertEqual(result.items[1].id, "50")
        self.assertTrue(result.complete)

    async def test_profile_and_posts_use_documented_user_endpoints(self) -> None:
        user = {"user_id": 9, "display_name": "Grace Hopper", "link": "https://stackoverflow.com/users/9/grace-hopper", "about_me": "A public bio", "creation_date": 1_600_000_000, "reputation": 900}

        def handler(url: str, _kwargs: dict[str, Any]) -> dict[str, Any]:
            if url.endswith("/users"):
                return {"items": [user], "has_more": False}
            if url.endswith("/users/9/posts"):
                return {"items": [{"post_id": 42, "post_type": "question", "question_id": 42, "title": "A public post", "body": "Post body", "link": "https://stackoverflow.com/questions/42/a-public-post", "owner": user, "creation_date": 1_700_000_000}], "has_more": False}
            raise AssertionError(f"Unexpected endpoint: {url}")

        api = FakeApi(handler)
        adapter = StackExchangeAdapter()
        with patch.object(stackexchange, "get_json", api):
            profile = await adapter.profile("Grace Hopper")
            posts = await adapter.profile_posts("Grace Hopper", limit=12)
        self.assertEqual(profile.type, "profile")
        self.assertEqual(profile.id, "9")
        self.assertEqual(profile.text, "A public bio")
        self.assertEqual(posts.items[0].type, "question")
        self.assertEqual(posts.items[0].author, "Grace Hopper")
        self.assertEqual(api.calls[2]["params"]["site"], "stackoverflow")

    async def test_anonymous_page_ceiling_is_reported(self) -> None:
        api = FakeApi(lambda _url, _kwargs: {"items": [question()], "has_more": True})
        adapter = StackExchangeAdapter()
        with patch.object(stackexchange, "get_json", api):
            result = await adapter.search("query", cursor="25")
        self.assertFalse(result.complete)
        self.assertFalse(result.pagination.has_more)
        self.assertIsNone(result.pagination.cursor)
        self.assertIn("capped at page 25", result.warnings[0])
        with self.assertRaises(ReachError):
            await adapter.search("query", cursor="26")

    async def test_api_error_wrapper_and_method_backoff_are_honored(self) -> None:
        responses = [
            {"error_id": 502, "error_name": "throttle_violation", "error_message": "Wait before retrying."},
            {"items": [question()], "backoff": 3},
            {"items": [question()]},
        ]
        api = FakeApi(lambda _url, _kwargs: responses.pop(0))
        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        adapter = StackExchangeAdapter(sleep=fake_sleep)
        with patch.object(stackexchange, "get_json", api):
            with self.assertRaises(ReachError) as error:
                await adapter.search("query")
            self.assertEqual(error.exception.code, "rate_limited")
            self.assertTrue(error.exception.retryable)
            first = await adapter.read("https://stackoverflow.com/questions/42/example")
            second = await adapter.read("https://stackoverflow.com/questions/42/example")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(delays), 1)
        self.assertGreater(delays[0], 0)
        self.assertLessEqual(delays[0], 3)


if __name__ == "__main__":
    import unittest

    unittest.main()
