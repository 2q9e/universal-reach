"""Read-only adapter for documented V2EX API endpoints.

Topic reads and replies use API 2.0 and require ``V2EX_ACCESS_TOKEN``. Public
member profiles use the documented legacy JSON endpoint and need no token.
V2EX documents no keyword-search endpoint, so search remains unsupported.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_json
from universal_reach.models import AccessInfo, AdapterStatus, Pagination, Record, ResultPage
from universal_reach.security import validate_https_url


API_ROOT = "https://www.v2ex.com/api/v2"
LEGACY_API_ROOT = "https://www.v2ex.com/api"
API_HOSTS = frozenset({"www.v2ex.com"})
URL_HOSTS = frozenset({"v2ex.com", "www.v2ex.com", "edge.v2ex.com"})
TOKEN_ENV = "V2EX_ACCESS_TOKEN"
MAX_LIMIT = 100

_TOPIC_PATH = re.compile(r"^/t/([1-9][0-9]{0,15})(?:/.*)?$")
_USERNAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class V2EXAdapter(Adapter):
    """Retrieve public V2EX profiles and authenticated topics/replies.

    API 2.0 read routes accept a Bearer Personal Access Token. The token is
    read only from ``V2EX_ACCESS_TOKEN`` and is never copied into errors,
    records, or logs. The legacy profile route is publicly documented as
    unauthenticated.
    """

    url_hosts = URL_HOSTS

    def __init__(self, *, timeout: float = 12.0, retries: int = 1) -> None:
        super().__init__(adapter_capability("v2ex", ("read", "replies", "profile")))
        self.timeout = timeout
        self.retries = retries

    @staticmethod
    def _token() -> str:
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not token:
            raise ReachError(
                "authentication_required",
                f"This V2EX API operation requires a Personal Access Token in {TOKEN_ENV}.",
                details={"environment_variable": TOKEN_ENV},
            )
        return token

    async def _v2_get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        token = self._token()
        try:
            payload = await get_json(
                f"{API_ROOT}/{path.lstrip('/')}",
                allowed_hosts=API_HOSTS,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout,
                retries=self.retries,
            )
        except ReachError as exc:
            # The shared HTTP client deliberately uses a platform-neutral
            # authorization error. On this API, these GET routes require a PAT.
            if exc.details.get("http_status") in (401, 403):
                raise ReachError(
                    "authentication_required",
                    "V2EX rejected the Personal Access Token for this read.",
                    details={"http_status": exc.details["http_status"]},
                ) from exc
            raise

        if isinstance(payload, dict) and payload.get("success") is False:
            message = payload.get("message")
            is_auth_error = isinstance(message, str) and any(
                term in message.casefold() for term in ("token", "auth", "unauthorized")
            )
            raise ReachError(
                "authentication_required" if is_auth_error else "upstream_error",
                "V2EX rejected the Personal Access Token." if is_auth_error else "V2EX reported an API error.",
                details={"api_error": True},
            )
        return payload

    @staticmethod
    def _topic_id(url: str) -> str:
        validate_https_url(url, URL_HOSTS)
        match = _TOPIC_PATH.fullmatch(urlsplit(url).path)
        if match is None:
            raise ReachError(
                "invalid_url",
                "Provide a V2EX topic URL in the form https://www.v2ex.com/t/{topic_id}.",
            )
        return match.group(1)

    @staticmethod
    def _page(cursor: str | None) -> int:
        if cursor is None or cursor == "":
            return 1
        if isinstance(cursor, bool) or not isinstance(cursor, (str, int)):
            raise ReachError("invalid_cursor", "The V2EX pagination cursor must be a positive page number.")
        value = str(cursor)
        if not value.isascii() or not value.isdecimal() or len(value) > 6:
            raise ReachError("invalid_cursor", "The V2EX pagination cursor must be a page number from 1 to 100000.")
        page = int(value)
        if not 1 <= page <= 100_000:
            raise ReachError("invalid_cursor", "The V2EX pagination cursor must be a page number from 1 to 100000.")
        return page

    @staticmethod
    def _limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ReachError("invalid_input", "The result limit must be a positive integer.")
        return min(limit, MAX_LIMIT)

    @staticmethod
    def _payload_object(payload: Any, *, kind: str) -> dict[str, Any]:
        if isinstance(payload, dict):
            for key in (kind, "data", "result"):
                candidate = payload.get(key)
                if isinstance(candidate, dict):
                    payload = candidate
                    break
            if isinstance(payload, dict):
                return payload
        raise ReachError("parser_error", f"V2EX returned an invalid {kind} response.")

    @staticmethod
    def _payload_items(payload: Any, *, kind: str) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            items: Any = payload
        elif isinstance(payload, dict):
            items = None
            for key in ("items", kind, "data", "result"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    items = candidate
                    break
        else:
            items = None
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ReachError("parser_error", f"V2EX returned an invalid {kind} list.")
        return items

    @staticmethod
    def _string(value: Any) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _timestamp(value: Any) -> str | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            except (OverflowError, OSError, ValueError):
                return None
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except ValueError:
                return None
        return None

    @staticmethod
    def _count(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 15:
            return int(value)
        return None

    @staticmethod
    def _member_name(item: dict[str, Any]) -> str | None:
        member = item.get("member")
        if isinstance(member, dict):
            for key in ("username", "name", "display_name"):
                value = member.get(key)
                if isinstance(value, str):
                    return value
        for key in ("username", "author"):
            value = item.get(key)
            if isinstance(value, str):
                return value
        return None

    def _topic_record(self, item: dict[str, Any], topic_id: str, *, complete: bool) -> Record:
        returned_id = item.get("id", topic_id)
        if isinstance(returned_id, bool) or str(returned_id) != topic_id:
            raise ReachError("parser_error", "V2EX returned a topic with an unexpected ID.")
        metrics: dict[str, int | float | str | None] = {}
        reply_count = self._count(item.get("replies"))
        if reply_count is not None:
            metrics["replies"] = reply_count
        extensions: dict[str, Any] = {}
        rendered = self._string(item.get("content_rendered"))
        if rendered is not None:
            extensions["content_rendered"] = rendered
        node = item.get("node")
        if isinstance(node, dict):
            extensions["node"] = node
        last_modified = self._timestamp(item.get("last_modified"))
        if last_modified is not None:
            extensions["last_modified"] = last_modified
        api_url = self._string(item.get("url"))
        if api_url is not None:
            extensions["api_url"] = api_url
        return Record(
            platform=self.platform,
            type="topic",
            id=topic_id,
            url=f"https://www.v2ex.com/t/{topic_id}",
            author=self._member_name(item),
            title=self._string(item.get("title")),
            text=self._string(item.get("content")),
            created_at=self._timestamp(item.get("created")),
            metrics=metrics,
            access=AccessInfo(mode="authenticated", complete=complete),
            extensions=extensions,
        )

    def _reply_record(self, item: dict[str, Any], topic_id: str) -> Record:
        reply_id = item.get("id")
        if isinstance(reply_id, bool) or not isinstance(reply_id, (str, int)) or not str(reply_id).isdecimal():
            raise ReachError("parser_error", "V2EX returned a reply without a valid ID.")
        reply_id = str(reply_id)
        extensions: dict[str, Any] = {}
        rendered = self._string(item.get("content_rendered"))
        if rendered is not None:
            extensions["content_rendered"] = rendered
        thanks = self._count(item.get("thank"))
        metrics: dict[str, int | float | str | None] = {"thanks": thanks} if thanks is not None else {}
        return Record(
            platform=self.platform,
            type="reply",
            id=reply_id,
            url=f"https://www.v2ex.com/t/{topic_id}#reply{reply_id}",
            author=self._member_name(item),
            text=self._string(item.get("content")),
            created_at=self._timestamp(item.get("created")),
            metrics=metrics,
            access=AccessInfo(mode="authenticated", complete=True),
            extensions=extensions,
        )

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        if options not in (None, {}):
            raise ReachError("invalid_input", "V2EX topic reads do not accept options.")
        topic_id = self._topic_id(url)
        payload = await self._v2_get(f"topics/{topic_id}")
        item = self._payload_object(payload, kind="topic")
        return self._topic_record(item, topic_id, complete=True)

    async def replies(
        self,
        url: str,
        limit: int = 50,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        if options not in (None, {}):
            raise ReachError("invalid_input", "V2EX reply retrieval does not accept options.")
        topic_id = self._topic_id(url)
        page = self._page(cursor)
        result_limit = self._limit(limit)
        payload = await self._v2_get(f"topics/{topic_id}/replies", params={"p": page})
        items = self._payload_items(payload, kind="replies")
        records = [self._reply_record(item, topic_id) for item in items[:result_limit]]
        truncated = len(items) > result_limit
        has_more = bool(items)
        warnings: list[str] = []
        if has_more:
            warnings.append(
                "V2EX documents page-number pagination but no total count or has_more field; "
                "a non-empty page is treated as possibly followed by another page."
            )
        if truncated:
            warnings.append(
                "The response exceeded the local limit; remaining items on this API page were omitted. "
                "The documented page cursor advances to the next API page."
            )
        return ResultPage(
            platform=self.platform,
            operation="replies",
            items=records,
            pagination=Pagination(
                cursor=str(page + 1) if has_more else None,
                has_more=has_more,
                detail=(
                    "V2EX's documented route accepts a page number but does not return a total or has_more flag."
                ),
            ),
            complete=not has_more,
            warnings=warnings,
            extensions={"topic_id": topic_id, "page": page},
        )

    async def profile(self, username: str, options: dict[str, Any] | None = None) -> Record:
        if options not in (None, {}):
            raise ReachError("invalid_input", "V2EX profile reads do not accept options.")
        if not isinstance(username, str) or not _USERNAME.fullmatch(username.strip()):
            raise ReachError("invalid_input", "A valid V2EX username is required.")
        username = username.strip()
        payload = await get_json(
            f"{LEGACY_API_ROOT}/members/show.json",
            allowed_hosts=API_HOSTS,
            params={"username": username},
            timeout=self.timeout,
            retries=self.retries,
        )
        if isinstance(payload, list):
            if not payload:
                raise ReachError("not_found", "The V2EX member profile was not found.")
            item = payload[0]
        else:
            item = self._payload_object(payload, kind="profile")
        if not isinstance(item, dict):
            raise ReachError("parser_error", "V2EX returned an invalid member profile.")
        status = item.get("status")
        if isinstance(status, str) and status.casefold() in {"not_found", "error", "failed"}:
            raise ReachError("not_found", "The V2EX member profile was not found.")
        member_id = item.get("id")
        if isinstance(member_id, bool) or not isinstance(member_id, (str, int)) or not str(member_id):
            member_id = username
        if not isinstance(item.get("username"), str) and member_id == username:
            raise ReachError("parser_error", "V2EX returned a profile without a member identity.")
        extensions = {
            key: item[key]
            for key in ("status", "website", "twitter", "location", "tagline")
            if isinstance(item.get(key), str)
        }
        profile_name = self._string(item.get("username")) or username
        return Record(
            platform=self.platform,
            type="profile",
            id=str(member_id),
            url=f"https://www.v2ex.com/member/{quote(profile_name, safe='')}",
            author=profile_name,
            title=self._string(item.get("tagline")),
            text=self._string(item.get("bio")),
            access=AccessInfo(mode="anonymous", complete=True),
            extensions=extensions,
        )

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        del options
        try:
            await self.profile("V2EX")
        except ReachError as exc:
            return AdapterStatus(
                platform=self.platform,
                status="degraded" if exc.code == "rate_limited" else "unavailable",
                operations=list(self.capability.operations),
                message=f"V2EX public profile health check failed ({exc.code}).",
                access_mode="anonymous",
            )
        return AdapterStatus(
            platform=self.platform,
            status="available",
            operations=list(self.capability.operations),
            message="V2EX's documented anonymous profile endpoint responded.",
            access_mode="anonymous",
        )
