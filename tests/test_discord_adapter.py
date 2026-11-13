"""Deterministic tests for the read-only official Discord Bot API adapter."""

from __future__ import annotations

import os
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from universal_reach.adapters import discord
from universal_reach.adapters.discord import DiscordAdapter, TOKEN_ENV
from universal_reach.errors import ReachError


GUILD_ID = "111111111111111111"
CHANNEL_ID = "222222222222222222"
TARGET_ID = "333333333333333333"
MESSAGE_URL = f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}/{TARGET_ID}"


def message(message_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": message_id,
        "channel_id": CHANNEL_ID,
        "content": "A Discord message",
        "timestamp": "2025-01-02T03:04:05.000Z",
        "author": {"id": "444444444444444444", "username": "ada", "global_name": "Ada"},
        **extra,
    }


class FakeApi:
    def __init__(
        self,
        payload: Any = None,
        error: ReachError | None = None,
        responses: list[Any] | None = None,
    ) -> None:
        self.payload = payload
        self.error = error
        self.responses = list(responses) if responses is not None else None
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        if self.responses is not None:
            return self.responses.pop(0)
        return self.payload


class DiscordAdapterTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = DiscordAdapter(channel_ids={CHANNEL_ID})

    async def test_adapter_requires_an_explicit_channel_allowlist(self) -> None:
        with self.assertRaises(ValueError):
            DiscordAdapter(channel_ids=[])
        with self.assertRaises(ValueError):
            DiscordAdapter(channel_ids=["not-a-snowflake"])
        with self.assertRaises(ValueError):
            DiscordAdapter(channel_ids=CHANNEL_ID)
        with self.assertRaises(ValueError):
            DiscordAdapter(channel_ids=CHANNEL_ID.encode("ascii"))

    async def test_no_token_fails_before_any_api_call(self) -> None:
        api = FakeApi(message(TARGET_ID))
        with patch.dict(os.environ, {TOKEN_ENV: ""}), patch.object(discord, "get_json", api):
            with self.assertRaises(ReachError) as caught:
                await self.adapter.read(MESSAGE_URL)

        self.assertEqual(caught.exception.code, "authentication_required")
        self.assertEqual(api.calls, [])
        self.assertNotIn(TOKEN_ENV, str(caught.exception.details.get("token", "")))

    async def test_read_exact_message_uses_bot_token_and_normalizes_record(self) -> None:
        secret = "discord-test-bot-token-do-not-reflect"
        payload = message(
            TARGET_ID,
            attachments=[
                {"id": "555555555555555555", "filename": "image.png", "url": "https://cdn.discordapp.com/image.png", "content_type": "image/png", "size": 123}
            ],
            pinned=True,
            message_reference={"type": 0, "message_id": "666666666666666666", "channel_id": CHANNEL_ID},
        )
        api = FakeApi(payload)
        with patch.dict(os.environ, {TOKEN_ENV: secret}), patch.object(discord, "get_json", api):
            result = await self.adapter.read(MESSAGE_URL)

        call = api.calls[0]
        self.assertTrue(call["url"].endswith(f"/api/v10/channels/{CHANNEL_ID}/messages/{TARGET_ID}"))
        self.assertEqual(call["allowed_hosts"], frozenset({"discord.com"}))
        self.assertEqual(call["headers"], {"Authorization": f"Bot {secret}"})
        self.assertEqual(result.platform, "discord")
        self.assertEqual(result.type, "message")
        self.assertEqual(result.id, TARGET_ID)
        self.assertEqual(result.author, "Ada")
        self.assertEqual(result.text, "A Discord message")
        self.assertEqual(result.created_at, "2025-01-02T03:04:05Z")
        self.assertEqual(result.media[0].title, "image.png")
        self.assertEqual(result.extensions["message_reference"]["message_id"], "666666666666666666")
        self.assertTrue(result.access.complete)
        self.assertNotIn(secret, str(result.to_dict()))

    async def test_replies_page_uses_channel_history_and_filters_target_references(self) -> None:
        secret = "discord-test-bot-token"
        first = message(
            "333333333333333340",
            content="Direct reply",
            message_reference={"type": 0, "message_id": TARGET_ID, "channel_id": CHANNEL_ID},
        )
        unrelated = message("333333333333333339", message_reference={"type": 0, "message_id": "555555555555555555", "channel_id": CHANNEL_ID})
        api = FakeApi([first, unrelated])
        with patch.dict(os.environ, {TOKEN_ENV: secret}), patch.object(discord, "get_json", api):
            result = await self.adapter.replies(MESSAGE_URL, limit=2)

        call = api.calls[0]
        self.assertTrue(call["url"].endswith(f"/api/v10/channels/{CHANNEL_ID}/messages"))
        self.assertEqual(call["params"], {"limit": 2, "after": TARGET_ID})
        self.assertEqual([item.text for item in result.items], ["Direct reply"])
        self.assertEqual(result.items[0].type, "message")
        self.assertEqual(result.pagination.cursor, unrelated["id"])
        self.assertTrue(result.pagination.has_more)
        self.assertFalse(result.complete)
        self.assertIn("full page", result.warnings[0])
        self.assertNotIn(secret, str(result.to_dict()))

    async def test_replies_continuation_uses_before_cursor_and_stops_at_target(self) -> None:
        newest_reply = message(
            "333333333333333340",
            message_reference={"type": 0, "message_id": TARGET_ID, "channel_id": CHANNEL_ID},
        )
        unrelated = message("333333333333333339")
        older_reply = message(
            "333333333333333338",
            message_reference={"type": 0, "message_id": TARGET_ID, "channel_id": CHANNEL_ID},
        )
        too_old = message("333333333333333332")
        api = FakeApi(responses=[[newest_reply, unrelated], [older_reply, too_old]])
        with patch.dict(os.environ, {TOKEN_ENV: "local-token"}), patch.object(discord, "get_json", api):
            first = await self.adapter.replies(MESSAGE_URL, limit=2)
            second = await self.adapter.replies(MESSAGE_URL, limit=2, cursor=first.pagination.cursor)

        self.assertEqual(api.calls[0]["params"], {"limit": 2, "after": TARGET_ID})
        self.assertEqual(api.calls[1]["params"], {"limit": 2, "before": unrelated["id"]})
        self.assertEqual(first.pagination.cursor, unrelated["id"])
        self.assertEqual([item.id for item in second.items], [older_reply["id"]])
        self.assertFalse(second.pagination.has_more)
        self.assertTrue(second.complete)

    async def test_continuation_cursor_is_passed_as_before(self) -> None:
        secret = "local-token"
        cursor_id = "333333333333333340"
        api = FakeApi([message("333333333333333339")])
        with patch.dict(os.environ, {TOKEN_ENV: secret}), patch.object(discord, "get_json", api):
            page = await self.adapter.replies(MESSAGE_URL, limit=5, cursor=cursor_id)
        self.assertEqual(api.calls[0]["params"], {"limit": 5, "before": cursor_id})
        self.assertFalse(page.pagination.has_more)

    async def test_empty_history_page_is_not_reported_as_complete(self) -> None:
        api = FakeApi([])
        with patch.dict(os.environ, {TOKEN_ENV: "local-token"}), patch.object(discord, "get_json", api):
            page = await self.adapter.replies(MESSAGE_URL)
        self.assertFalse(page.complete)
        self.assertFalse(page.pagination.has_more)
        self.assertTrue(any("READ_MESSAGE_HISTORY" in warning for warning in page.warnings))

    async def test_permission_and_auth_errors_are_mapped_without_secret_reflection(self) -> None:
        secret = "discord-test-bot-token-do-not-reflect"
        for status, code in ((401, "authentication_required"), (403, "permission_denied")):
            api = FakeApi(error=ReachError("private_or_inaccessible", "blocked", details={"http_status": status}))
            with patch.dict(os.environ, {TOKEN_ENV: secret}), patch.object(discord, "get_json", api):
                with self.assertRaises(ReachError) as caught:
                    await self.adapter.read(MESSAGE_URL)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(secret, str(caught.exception.to_dict()))

    async def test_channel_allowlist_blocks_before_network(self) -> None:
        other_channel = "777777777777777777"
        api = FakeApi(message(TARGET_ID))
        with patch.dict(os.environ, {TOKEN_ENV: "local-token"}), patch.object(discord, "get_json", api):
            with self.assertRaises(ReachError) as caught:
                await self.adapter.read(f"https://discord.com/channels/{GUILD_ID}/{other_channel}/{TARGET_ID}")
        self.assertEqual(caught.exception.code, "channel_not_allowed")
        self.assertEqual(api.calls, [])

    async def test_non_discord_url_is_rejected_before_network(self) -> None:
        api = FakeApi(message(TARGET_ID))
        with patch.dict(os.environ, {TOKEN_ENV: "local-token"}), patch.object(discord, "get_json", api):
            with self.assertRaises(ReachError) as caught:
                await self.adapter.read(f"https://attacker.example/channels/{GUILD_ID}/{CHANNEL_ID}/{TARGET_ID}")
        self.assertEqual(caught.exception.code, "invalid_url")
        self.assertEqual(api.calls, [])

    async def test_missing_message_content_is_marked_incomplete(self) -> None:
        api = FakeApi(message(TARGET_ID, content=""))
        with patch.dict(os.environ, {TOKEN_ENV: "local-token"}), patch.object(discord, "get_json", api):
            record = await self.adapter.read(MESSAGE_URL)
        self.assertFalse(record.access.complete)
        self.assertTrue(record.extensions["content_may_be_suppressed"])
        self.assertIn("MESSAGE_CONTENT", record.extensions["content_note"])


if __name__ == "__main__":
    import unittest

    unittest.main()
