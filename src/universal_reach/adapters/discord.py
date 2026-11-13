"""Read-only Discord message adapter using the official Bot HTTP API.

The adapter accepts only an environment-sourced bot token and an explicit
channel allowlist. It never uses user credentials, the Gateway, or write APIs.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_json
from universal_reach.models import AccessInfo, AdapterStatus, MediaItem, Pagination, Record, ResultPage
from universal_reach.security import validate_https_url


API_ROOT = "https://discord.com/api/v10"
API_HOSTS = frozenset({"discord.com"})
URL_HOSTS = frozenset({"discord.com", "www.discord.com"})
TOKEN_ENV = "DISCORD_BOT_TOKEN"
MAX_LIMIT = 100

_SNOWFLAKE = re.compile(r"^[1-9][0-9]{0,19}$")
_MESSAGE_URL = re.compile(r"^/channels/([1-9][0-9]{0,19})/([1-9][0-9]{0,19})/([1-9][0-9]{0,19})/?$")


class DiscordAdapter(Adapter):
    """Read messages in explicitly allowlisted Discord channels.

    Use ``DiscordAdapter(channel_ids={...})`` with channel IDs the local user
    has chosen to permit. Discord still enforces the bot's server and channel
    permissions. ``DISCORD_BOT_TOKEN`` is read only when an API call is made.
    """

    url_hosts = URL_HOSTS

    def __init__(
        self,
        channel_ids: Iterable[str | int],
        *,
        timeout: float = 12.0,
        retries: int = 1,
        token_env: str = TOKEN_ENV,
    ) -> None:
        if isinstance(channel_ids, (str, bytes, bytearray)):
            raise ValueError("channel_ids must be an iterable of IDs, not a string or bytes value")
        normalized: set[str] = set()
        try:
            iterator = iter(channel_ids)
        except TypeError as exc:
            raise ValueError("channel_ids must be an iterable of Discord snowflake IDs") from exc
        for channel_id in iterator:
            if isinstance(channel_id, bool) or not _SNOWFLAKE.fullmatch(str(channel_id)):
                raise ValueError("channel_ids must contain Discord snowflake IDs")
            normalized.add(str(channel_id))
        if not normalized:
            raise ValueError("DiscordAdapter requires an explicit non-empty channel_ids allowlist")
        if not isinstance(token_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", token_env):
            raise ValueError("token_env must be an environment variable name")
        super().__init__(adapter_capability("discord", ("read", "replies")))
        self.channel_ids = frozenset(normalized)
        self.timeout = timeout
        self.retries = retries
        self.token_env = token_env

    def _headers(self) -> dict[str, str]:
        token = os.environ.get(self.token_env, "").strip()
        if not token:
            raise ReachError(
                "authentication_required",
                f"Discord bot reads require a local bot token in {self.token_env}.",
                details={"environment_variable": self.token_env},
            )
        return {"Authorization": f"Bot {token}"}

    async def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        headers = self._headers()
        try:
            return await get_json(
                f"{API_ROOT}/{path.lstrip('/')}",
                allowed_hosts=API_HOSTS,
                params=params,
                headers=headers,
                timeout=self.timeout,
                retries=self.retries,
            )
        except ReachError as exc:
            status = exc.details.get("http_status")
            if status == 401:
                raise ReachError(
                    "authentication_required",
                    "Discord rejected the configured bot token.",
                    details={"http_status": status},
                ) from exc
            if status == 403:
                raise ReachError(
                    "permission_denied",
                    "The Discord bot cannot read this channel or message with its current permissions.",
                    details={"http_status": status},
                ) from exc
            raise

    @staticmethod
    def _message_url(url: str) -> tuple[str, str, str]:
        validate_https_url(url, URL_HOSTS)
        match = _MESSAGE_URL.fullmatch(urlsplit(url).path)
        if match is None:
            raise ReachError(
                "invalid_url",
                "Provide a Discord message URL like https://discord.com/channels/{guild_id}/{channel_id}/{message_id}.",
            )
        return match.group(1), match.group(2), match.group(3)

    def _allowed_channel(self, channel_id: str) -> None:
        if channel_id not in self.channel_ids:
            raise ReachError(
                "channel_not_allowed",
                "This Discord channel is not in the adapter's explicit channel allowlist.",
                details={"channel_id": channel_id},
            )

    @staticmethod
    def _limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ReachError("invalid_input", "The result limit must be a positive integer.")
        return min(limit, MAX_LIMIT)

    @staticmethod
    def _cursor(cursor: str | None, *, target_id: str) -> str | None:
        if cursor is None or cursor == "":
            return None
        if not isinstance(cursor, str) or not _SNOWFLAKE.fullmatch(cursor):
            raise ReachError("invalid_cursor", "The Discord pagination cursor must be a message ID.")
        if int(cursor) <= int(target_id):
            raise ReachError("invalid_cursor", "The Discord pagination cursor must be newer than the target message.")
        return cursor

    @staticmethod
    def _timestamp(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _string(value: Any) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _author(message: dict[str, Any]) -> str | None:
        author = message.get("author")
        if not isinstance(author, dict):
            return None
        for field in ("global_name", "username"):
            value = author.get(field)
            if isinstance(value, str):
                return value
        return None

    def _record(self, message: dict[str, Any], *, guild_id: str, channel_id: str) -> Record:
        message_id = message.get("id")
        response_channel_id = message.get("channel_id", channel_id)
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, (int, str))
            or not _SNOWFLAKE.fullmatch(str(message_id))
            or str(response_channel_id) != channel_id
        ):
            raise ReachError("parser_error", "Discord returned a message with invalid identity fields.")
        message_id = str(message_id)
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        author_id = author.get("id") if isinstance(author.get("id"), str) else None
        content = self._string(message.get("content"))
        media: list[MediaItem] = []
        attachments = message.get("attachments")
        if isinstance(attachments, list):
            for attachment in attachments:
                if not isinstance(attachment, dict) or not isinstance(attachment.get("url"), str):
                    continue
                media.append(
                    MediaItem(
                        kind="attachment",
                        url=attachment["url"],
                        mime_type=self._string(attachment.get("content_type")),
                        title=self._string(attachment.get("filename")),
                        metadata={
                            key: attachment[key]
                            for key in ("id", "size", "width", "height", "duration_secs", "spoiler")
                            if key in attachment and isinstance(attachment[key], (str, int, float, bool, type(None)))
                        },
                    )
                )
        reference = message.get("message_reference")
        extensions: dict[str, Any] = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "message_type": message.get("type"),
            "edited_at": self._timestamp(message.get("edited_timestamp")),
            "pinned": bool(message.get("pinned", False)),
        }
        if author_id:
            extensions["author_id"] = author_id
        if isinstance(author.get("bot"), bool):
            extensions["author_is_bot"] = author["bot"]
        if isinstance(reference, dict):
            extensions["message_reference"] = {
                key: reference[key]
                for key in ("type", "message_id", "channel_id", "guild_id")
                if key in reference and isinstance(reference[key], (str, int))
            }
        if content == "":
            extensions["content_may_be_suppressed"] = True
        return Record(
            platform=self.platform,
            type="message",
            id=message_id,
            url=f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}",
            author=self._author(message),
            text=content,
            created_at=self._timestamp(message.get("timestamp")),
            media=media,
            access=AccessInfo(mode="authenticated", complete=content != ""),
            extensions=extensions,
        )

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        if options not in (None, {}):
            raise ReachError("invalid_input", "Discord message reads do not accept options.")
        guild_id, channel_id, message_id = self._message_url(url)
        self._allowed_channel(channel_id)
        payload = await self._get(f"channels/{channel_id}/messages/{message_id}")
        if not isinstance(payload, dict) or str(payload.get("id")) != message_id:
            raise ReachError("parser_error", "Discord returned an invalid exact-message response.")
        record = self._record(payload, guild_id=guild_id, channel_id=channel_id)
        if record.extensions.get("content_may_be_suppressed"):
            record.extensions["content_note"] = (
                "Discord may return empty message content when the MESSAGE_CONTENT privileged intent "
                "is not configured or approved; empty content may also be genuine."
            )
        return record

    async def replies(
        self,
        url: str,
        limit: int = 50,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        if options not in (None, {}):
            raise ReachError("invalid_input", "Discord reply reads do not accept options.")
        guild_id, channel_id, target_id = self._message_url(url)
        self._allowed_channel(channel_id)
        page_size = self._limit(limit)
        before = self._cursor(cursor, target_id=target_id)
        params: dict[str, Any] = {"limit": page_size}
        if before:
            params["before"] = before
        else:
            params["after"] = target_id

        payload = await self._get(f"channels/{channel_id}/messages", params=params)
        if not isinstance(payload, list) or any(not isinstance(message, dict) for message in payload):
            raise ReachError("parser_error", "Discord returned an invalid channel-history response.")
        raw_ids: list[str] = []
        for message in payload:
            message_id = message.get("id")
            if isinstance(message_id, bool) or not isinstance(message_id, (str, int)) or not _SNOWFLAKE.fullmatch(str(message_id)):
                raise ReachError("parser_error", "Discord returned a channel message without a valid ID.")
            raw_ids.append(str(message_id))

        # Discord's message route returns newest-to-oldest. Continue toward the
        # target by using `before` with the oldest raw ID from each full page.
        page_reached_target = bool(raw_ids) and int(raw_ids[-1]) <= int(target_id)
        has_more = len(payload) == page_size and not page_reached_target
        next_cursor = raw_ids[-1] if has_more and raw_ids else None

        records: list[Record] = []
        for message, message_id in zip(payload, raw_ids):
            if int(message_id) <= int(target_id):
                continue
            reference = message.get("message_reference")
            if not isinstance(reference, dict):
                continue
            if str(reference.get("message_id", "")) != target_id:
                continue
            if reference.get("type") not in (None, 0):
                continue
            ref_channel_id = reference.get("channel_id")
            if ref_channel_id is not None and str(ref_channel_id) != channel_id:
                continue
            records.append(self._record(message, guild_id=guild_id, channel_id=channel_id))

        warnings: list[str] = []
        if not payload:
            warnings.append(
                "Discord returns no channel messages when READ_MESSAGE_HISTORY is missing; an empty page "
                "cannot distinguish that permission state from no newer messages."
            )
        elif has_more:
            warnings.append(
                "Discord returned a full page of channel history. More messages may remain; use the cursor "
                "to continue scanning for direct replies to this message."
            )
        if any(record.extensions.get("content_may_be_suppressed") for record in records):
            warnings.append(
                "Discord may return empty message content unless the MESSAGE_CONTENT privileged intent is configured or approved."
            )

        return ResultPage(
            platform=self.platform,
            operation="replies",
            items=records,
            pagination=Pagination(
                cursor=next_cursor,
                has_more=has_more,
                detail="Cursor is the oldest message ID in the returned page; pages scan channel history after the target message.",
            ),
            complete=not has_more and bool(payload),
            warnings=warnings,
            extensions={"guild_id": guild_id, "channel_id": channel_id, "target_message_id": target_id},
        )

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        del options
        return AdapterStatus(
            platform=self.platform,
            status="degraded",
            operations=list(self.capability.operations),
            message="Discord health check was not run; this adapter requires a local bot token and channel permissions.",
            access_mode="unknown",
        )
