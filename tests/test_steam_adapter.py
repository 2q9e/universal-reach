"""Mocked tests for the documented public Steam announcement RSS feed."""

from __future__ import annotations

from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from universal_reach.adapters import steam
from universal_reach.adapters.steam import SteamAdapter
from universal_reach.errors import ReachError


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Game announcements</title>
    <item>
      <title>Major update &amp; patch notes</title>
      <link>https://steamcommunity.com/games/440/announcements/detail/123</link>
      <guid isPermaLink="false">event-123</guid>
      <pubDate>Tue, 02 Jan 2024 03:04:05 -0500</pubDate>
      <dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">Valve</dc:creator>
      <content:encoded><![CDATA[<p>First line</p><p>Second &amp; final<script>ignore()</script></p>]]></content:encoded>
    </item>
    <item>
      <title>Another event</title>
      <link>https://steamcommunity.com/games/440/announcements/detail/456</link>
      <guid>event-456</guid>
      <description>ordinary feed text</description>
      <pubDate>Wed, 03 Jan 2024 08:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>"""


class FakeText:
    def __init__(self, response: str = RSS, error: ReachError | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, **kwargs: Any) -> str:
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


class SteamAdapterTests(IsolatedAsyncioTestCase):
    async def test_searches_only_the_selected_public_game_feed(self) -> None:
        fetch = FakeText()
        with patch.object(steam, "get_text", fetch):
            result = await SteamAdapter().search("patch", options={"app_id": "440"})

        self.assertEqual(len(fetch.calls), 1)
        url, kwargs = fetch.calls[0]
        self.assertEqual(url, "https://steamcommunity.com/games/440/rss")
        self.assertEqual(kwargs["allowed_hosts"], frozenset({"steamcommunity.com", "www.steamcommunity.com"}))
        self.assertEqual(kwargs["retries"], 0)
        self.assertEqual(kwargs["max_bytes"], 1_000_000)
        self.assertEqual(result.platform, "steam")
        self.assertFalse(result.complete)
        self.assertEqual(len(result.items), 1)
        post = result.items[0]
        self.assertEqual(post.type, "announcement")
        self.assertEqual(post.id, "event-123")
        self.assertEqual(post.url, "https://steamcommunity.com/games/440/announcements/detail/123")
        self.assertEqual(post.title, "Major update & patch notes")
        self.assertEqual(post.author, "Valve")
        self.assertEqual(post.text, "First line\nSecond & final")
        self.assertEqual(post.created_at, "2024-01-02T08:04:05Z")
        self.assertEqual(post.access.mode, "anonymous")
        self.assertFalse(post.access.complete)
        self.assertTrue(post.extensions["feed_limited"])

    async def test_cursor_paginates_only_the_current_feed_matches(self) -> None:
        fetch = FakeText()
        adapter = SteamAdapter()
        with patch.object(steam, "get_text", fetch):
            first = await adapter.search("e", limit=1, options={"app_id": 440})
            second = await adapter.search("e", limit=1, cursor=first.pagination.cursor, options={"app_id": 440})

        self.assertEqual(first.items[0].id, "event-123")
        self.assertTrue(first.pagination.has_more)
        self.assertEqual(first.pagination.cursor, "1")
        self.assertEqual(second.items[0].id, "event-456")
        self.assertFalse(second.pagination.has_more)
        self.assertIsNone(second.pagination.cursor)
        self.assertIn("current per-game RSS feed", first.pagination.detail or "")

    async def test_invalid_inputs_are_rejected_before_fetch(self) -> None:
        fetch = FakeText()
        adapter = SteamAdapter()
        with patch.object(steam, "get_text", fetch):
            bad_options = ({}, {"app_id": True}, {"app_id": "0"}, {"app_id": "4294967296"}, {"app_id": "44/0"})
            for options in bad_options:
                with self.subTest(options=options), self.assertRaises(ReachError) as caught:
                    await adapter.search("patch", options=options)
                self.assertEqual(caught.exception.code, "invalid_input")
            for query in ("", "   ", "x" * 501):
                with self.subTest(query_length=len(query)), self.assertRaises(ReachError):
                    await adapter.search(query, options={"app_id": "440"})
            with self.assertRaises(ReachError):
                await adapter.search("patch", limit=True, options={"app_id": "440"})
            with self.assertRaises(ReachError):
                await adapter.search("patch", cursor="-1", options={"app_id": "440"})
        self.assertEqual(fetch.calls, [])

    async def test_non_rss_and_entity_declarations_are_rejected(self) -> None:
        for body in (
            "<html>not a feed</html>",
            '<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel/></rss>',
            "<rss><channel></rss>",
        ):
            fetch = FakeText(response=body)
            with patch.object(steam, "get_text", fetch):
                with self.subTest(body=body), self.assertRaises(ReachError) as caught:
                    await SteamAdapter().search("patch", options={"app_id": "440"})
            self.assertEqual(caught.exception.code, "parser_error")

    async def test_feed_failure_is_preserved(self) -> None:
        upstream = ReachError("private_or_inaccessible", "Feed is not public", details={"http_status": 403})
        fetch = FakeText(error=upstream)
        with patch.object(steam, "get_text", fetch):
            with self.assertRaises(ReachError) as caught:
                await SteamAdapter().search("patch", options={"app_id": "440"})
        self.assertIs(caught.exception, upstream)

    async def test_health_check_uses_documented_feed_by_default_and_is_anonymous(self) -> None:
        fetch = FakeText()
        with patch.object(steam, "get_text", fetch):
            default = await SteamAdapter().health_check()
            configured = await SteamAdapter().health_check({"app_id": "440"})
        self.assertEqual(default.status, "available")
        self.assertEqual(default.access_mode, "anonymous")
        self.assertEqual(configured.status, "available")
        self.assertEqual(configured.access_mode, "anonymous")
        self.assertEqual([call[0] for call in fetch.calls], [
            "https://steamcommunity.com/games/489830/rss",
            "https://steamcommunity.com/games/440/rss",
        ])


if __name__ == "__main__":
    import unittest

    unittest.main()
