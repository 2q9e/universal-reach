"""Deterministic tests for the bounded public Bogleheads Atom feed adapter."""

from __future__ import annotations

from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from universal_reach.adapters import bogleheads
from universal_reach.adapters.bogleheads import BogleheadsAdapter
from universal_reach.errors import ReachError, UnsupportedOperation


ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Topic posts</title>
  <entry>
    <id>https://www.bogleheads.org/forum/viewtopic.php?f=20&amp;t=30&amp;p=3201#p3201</id>
    <title>Reply title</title>
    <link rel="alternate" href="https://www.bogleheads.org/forum/viewtopic.php?f=20&amp;t=30&amp;p=3201#p3201" />
    <author><name>Ada</name></author>
    <published>2024-01-02T03:04:05-05:00</published>
    <content type="html">&lt;p&gt;First line&lt;/p&gt;&lt;p&gt;Second &amp;amp; final&lt;/p&gt;&lt;script&gt;ignore()&lt;/script&gt;</content>
  </entry>
</feed>"""


class FakeText:
    def __init__(self, response: str = ATOM, error: ReachError | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, **kwargs: Any) -> str:
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


class BogleheadsAdapterTests(IsolatedAsyncioTestCase):
    async def test_health_check_validates_public_atom_feed(self) -> None:
        fetch = FakeText()
        with patch.object(bogleheads, "get_text", fetch):
            result = await BogleheadsAdapter().health_check()
        self.assertEqual(result.status, "available")
        self.assertEqual(result.access_mode, "anonymous")
        self.assertEqual(fetch.calls[0][0], "https://www.bogleheads.org/forum/feed.php?f=1")

    async def test_read_fetches_only_official_topic_feed_and_returns_bounded_post(self) -> None:
        fetch = FakeText()
        source = "https://www.bogleheads.org/forum/viewtopic.php?f=20&t=30&p=3201#p3201"
        with patch.object(bogleheads, "get_text", fetch):
            post = await BogleheadsAdapter().read(source)

        self.assertEqual(len(fetch.calls), 1)
        url, kwargs = fetch.calls[0]
        self.assertEqual(url, "https://www.bogleheads.org/forum/feed.php?f=20&t=30")
        self.assertEqual(kwargs["allowed_hosts"], frozenset({"www.bogleheads.org", "bogleheads.org"}))
        self.assertEqual(kwargs["retries"], 0)
        self.assertEqual(kwargs["max_bytes"], 2_000_000)
        self.assertNotIn("headers", kwargs)
        self.assertEqual(post.platform, "bogleheads")
        self.assertEqual(post.type, "post")
        self.assertEqual(post.id, "3201")
        self.assertEqual(post.author, "Ada")
        self.assertEqual(post.title, "Reply title")
        self.assertEqual(post.text, "First line\nSecond & final")
        self.assertEqual(post.created_at, "2024-01-02T08:04:05Z")
        self.assertEqual(post.access.mode, "anonymous")
        self.assertFalse(post.access.complete)
        self.assertTrue(post.extensions["feed_limited"])

    async def test_post_fragment_is_supported_and_search_or_replies_are_not_claimed(self) -> None:
        fetch = FakeText()
        source = "https://www.bogleheads.org/forum/viewtopic.php?f=20&t=30#p3201"
        adapter = BogleheadsAdapter()
        with patch.object(bogleheads, "get_text", fetch):
            post = await adapter.read(source)

        self.assertEqual(post.id, "3201")
        self.assertEqual(adapter.capability.operations, ("read",))
        with self.assertRaises(UnsupportedOperation):
            await adapter.search("planning")
        with self.assertRaises(UnsupportedOperation):
            await adapter.replies(source)
        with self.assertRaises(UnsupportedOperation):
            await adapter.profile_posts("Ada")

    async def test_post_absent_from_bounded_feed_is_not_reported_as_globally_missing(self) -> None:
        fetch = FakeText()
        with patch.object(bogleheads, "get_text", fetch):
            with self.assertRaises(ReachError) as caught:
                await BogleheadsAdapter().read(
                    "https://www.bogleheads.org/forum/viewtopic.php?f=20&t=30&p=9999"
                )

        self.assertEqual(caught.exception.code, "not_found")
        self.assertEqual(caught.exception.details["scope"], "current topic Atom feed")

    async def test_invalid_or_incomplete_post_urls_are_rejected_before_fetch(self) -> None:
        fetch = FakeText()
        bad_urls = (
            "https://attacker.example/forum/viewtopic.php?f=20&t=30&p=3201",
            "http://www.bogleheads.org/forum/viewtopic.php?f=20&t=30&p=3201",
            "https://www.bogleheads.org/forum/viewtopic.php?f=20&t=30",
            "https://www.bogleheads.org/forum/viewtopic.php?f=20&f=21&t=30&p=3201",
        )
        with patch.object(bogleheads, "get_text", fetch):
            for url in bad_urls:
                with self.subTest(url=url), self.assertRaises(ReachError) as caught:
                    await BogleheadsAdapter().read(url)
                self.assertEqual(caught.exception.code, "invalid_url")
        self.assertEqual(fetch.calls, [])

    async def test_feed_parser_rejects_non_atom_and_entity_declarations(self) -> None:
        for body in (
            "<html><body>not a feed</body></html>",
            '<!DOCTYPE feed [<!ENTITY x "boom">]><feed/>',
        ):
            fetch = FakeText(response=body)
            with patch.object(bogleheads, "get_text", fetch):
                with self.subTest(body=body), self.assertRaises(ReachError) as caught:
                    await BogleheadsAdapter().read(
                        "https://www.bogleheads.org/forum/viewtopic.php?f=20&t=30&p=3201"
                    )
            self.assertEqual(caught.exception.code, "parser_error")

    async def test_public_feed_errors_are_preserved(self) -> None:
        upstream = ReachError("private_or_inaccessible", "Feed is not public", details={"http_status": 403})
        fetch = FakeText(error=upstream)
        with patch.object(bogleheads, "get_text", fetch):
            with self.assertRaises(ReachError) as caught:
                await BogleheadsAdapter().read(
                    "https://www.bogleheads.org/forum/viewtopic.php?f=20&t=30&p=3201"
                )
        self.assertIs(caught.exception, upstream)


if __name__ == "__main__":
    import unittest

    unittest.main()
