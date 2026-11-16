"""Application service shared by the CLI and MCP server."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from universal_reach.config import load_config
from universal_reach.errors import ReachError
from universal_reach.registry import AdapterRegistry

MAX_LIMIT = 100
MAX_QUERY_LENGTH = 1000


def validate_limit(limit: int, maximum: int = MAX_LIMIT) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise ReachError(
            "invalid_argument",
            f"limit must be between 1 and {maximum}.",
            details={"limit": limit, "maximum": maximum},
        )
    return limit


def validate_query(query: str) -> str:
    if not isinstance(query, str):
        raise ReachError("invalid_argument", "query must be a string.")
    value = query.strip()
    if not value:
        raise ReachError("invalid_argument", "query must not be empty.")
    if len(value) > MAX_QUERY_LENGTH:
        raise ReachError("invalid_argument", f"query must not exceed {MAX_QUERY_LENGTH} characters.")
    return value


def _options(options: dict[str, Any] | None) -> dict[str, Any]:
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise ReachError("invalid_argument", "options must be an object.")
    if len(options) > 40:
        raise ReachError("invalid_argument", "options contains too many fields.")
    return options


class ReachService:
    def __init__(self, registry: AdapterRegistry, config: dict[str, Any] | None = None) -> None:
        self.registry = registry
        self.config = config if config is not None else load_config()

    def _adapter_options(self, platform: str, options: dict[str, Any] | None) -> dict[str, Any]:
        resolved = dict(_options(options))
        if platform == "lemmy" and not resolved.get("instance_url"):
            resolved["instance_url"] = self.config.get("lemmy_instance", "https://lemmy.world")
        if platform == "discourse" and not resolved.get("instance_url"):
            instances = self.config.get("discourse_instances", {})
            alias = resolved.get("instance")
            if isinstance(alias, str) and alias in instances:
                resolved["instance_url"] = instances[alias]
            elif len(instances) == 1:
                resolved["instance_url"] = next(iter(instances.values()))
        return resolved

    def platforms_list(self) -> list[dict[str, object]]:
        return self.registry.capabilities()

    async def feed(
        self, platform: str, category: str, limit: int = 20, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        limit = validate_limit(limit)
        if not isinstance(platform, str) or not platform.strip():
            raise ReachError("invalid_argument", "platform must be a non-empty string.")
        if not isinstance(category, str) or not category.strip() or len(category) > 64:
            raise ReachError("invalid_argument", "category must contain 1 to 64 characters.")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 2048):
            raise ReachError("invalid_argument", "cursor must be a string of at most 2048 characters.")
        adapter = self.registry.get(platform)
        page = await adapter.feed(category.strip(), limit, cursor, self._adapter_options(adapter.platform, options))
        return page.to_dict()

    async def platform_status(self, platform: str | None = None) -> dict[str, Any]:
        if platform is not None and not isinstance(platform, str):
            raise ReachError("invalid_argument", "platform must be a string.")
        return {"platforms": await self.registry.status(platform)}

    async def search(
        self, platform: str, query: str, limit: int = 10, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        limit = validate_limit(limit)
        query = validate_query(query)
        if not isinstance(platform, str) or not platform.strip():
            raise ReachError("invalid_argument", "platform must be a non-empty string.")
        if cursor is not None and len(cursor) > 2048:
            raise ReachError("invalid_argument", "cursor must not exceed 2048 characters.")
        adapter = self.registry.get(platform)
        page = await adapter.search(query, limit, cursor, self._adapter_options(adapter.platform, options))
        return page.to_dict()

    async def search_multi(
        self, platforms: list[str] | None, query: str, limit: int = 10,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        limit = validate_limit(limit, 50)
        query = validate_query(query)
        selected = platforms if platforms else self.registry.searchable_platforms()
        if not selected:
            raise ReachError("no_search_adapters", "No implemented searchable adapters are enabled.")
        if len(selected) > 35:
            raise ReachError("invalid_argument", "At most 35 platforms may be requested.")
        if not all(isinstance(platform, str) and platform.strip() for platform in selected):
            raise ReachError("invalid_argument", "platforms must contain non-empty strings.")
        selected = [platform.casefold() for platform in selected]
        if len(set(selected)) != len(selected):
            raise ReachError("invalid_argument", "platforms must not contain duplicates.")
        opts = _options(options)
        semaphore = asyncio.Semaphore(5)

        async def one(platform: str) -> dict[str, Any]:
            async with semaphore:
                try:
                    adapter = self.registry.get(platform)
                    result = await adapter.search(query, limit, None, self._adapter_options(adapter.platform, opts))
                    return {"platform": platform, "ok": True, "result": result.to_dict()}
                except ReachError as exc:
                    return {"platform": platform, "ok": False, "error": exc.to_dict()}
                except Exception:
                    return {
                        "platform": platform,
                        "ok": False,
                        "error": {"code": "internal_error", "message": "Adapter failed unexpectedly.", "retryable": False, "details": {}},
                    }

        results = await asyncio.gather(*(one(platform) for platform in selected))
        failures = [result["platform"] for result in results if not result["ok"]]
        return {"query": query, "results": results, "partial": bool(failures), "failed_platforms": failures}

    def _for_url(self, url: str, platform: str | None, options: dict[str, Any] | None = None):
        if not isinstance(url, str) or not url.strip():
            raise ReachError("invalid_url", "A non-empty HTTPS URL is required.")
        if platform is not None and not isinstance(platform, str):
            raise ReachError("invalid_argument", "platform must be a string.")
        if platform:
            return self.registry.get(platform)
        try:
            host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        except ValueError as exc:
            raise ReachError("invalid_url", "The URL is malformed.") from exc
        configured_hosts: dict[str, str] = {}
        lemmy_url = self.config.get("lemmy_instance", "https://lemmy.world")
        lemmy_host = (urlsplit(lemmy_url).hostname or "").lower()
        if lemmy_host:
            configured_hosts[lemmy_host] = "lemmy"
        for instance_url in self.config.get("discourse_instances", {}).values():
            instance_host = (urlsplit(instance_url).hostname or "").lower()
            if instance_host:
                configured_hosts[instance_host] = "discourse"
        if host in configured_hosts:
            return self.registry.get(configured_hosts[host])
        try:
            return self.registry.for_url(url)
        except ReachError:
            # Configurable public instances can be selected by matching their
            # host after the caller explicitly names the adapter elsewhere.
            if host:
                raise ReachError(
                    "unsupported_url",
                    "This URL needs an explicit platform and configured public instance.",
                    details={"host": host},
                )
            raise

    async def read(self, url: str, platform: str | None = None, options: dict[str, Any] | None = None) -> dict[str, Any]:
        adapter = self._for_url(url, platform, options)
        record = await adapter.read(url, self._adapter_options(adapter.platform, options))
        return record.to_dict()

    async def replies(
        self, url: str, platform: str | None = None, limit: int = 50,
        cursor: str | None = None, options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        limit = validate_limit(limit)
        adapter = self._for_url(url, platform, options)
        return (await adapter.replies(url, limit, cursor, self._adapter_options(adapter.platform, options))).to_dict()

    async def profile(self, platform: str, username: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(platform, str) or not platform.strip():
            raise ReachError("invalid_argument", "platform must be a non-empty string.")
        if not isinstance(username, str):
            raise ReachError("invalid_argument", "username must be a string.")
        name = username.strip()
        if not name or len(name) > 256:
            raise ReachError("invalid_argument", "username must contain 1 to 256 characters.")
        adapter = self.registry.get(platform)
        return (await adapter.profile(name, self._adapter_options(adapter.platform, options))).to_dict()

    async def profile_posts(
        self, platform: str, username: str, limit: int = 20, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        limit = validate_limit(limit)
        if not isinstance(platform, str) or not platform.strip():
            raise ReachError("invalid_argument", "platform must be a non-empty string.")
        if not isinstance(username, str):
            raise ReachError("invalid_argument", "username must be a string.")
        name = username.strip()
        if not name or len(name) > 256:
            raise ReachError("invalid_argument", "username must contain 1 to 256 characters.")
        adapter = self.registry.get(platform)
        return (await adapter.profile_posts(name, limit, cursor, self._adapter_options(adapter.platform, options))).to_dict()

    async def media(self, url: str, platform: str | None = None, options: dict[str, Any] | None = None) -> dict[str, Any]:
        adapter = self._for_url(url, platform, options)
        items = await adapter.media(url, self._adapter_options(adapter.platform, options))
        return {"platform": adapter.platform, "url": url, "items": items}

    async def transcript(self, url: str, platform: str | None = None, options: dict[str, Any] | None = None) -> dict[str, Any]:
        adapter = self._for_url(url, platform, options)
        return await adapter.transcript(url, self._adapter_options(adapter.platform, options))

    async def archive(self, platform: str, query: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        query = validate_query(query)
        if not isinstance(platform, str) or not platform.strip():
            raise ReachError("invalid_argument", "platform must be a non-empty string.")
        adapter = self.registry.get(platform)
        return (await adapter.archive(query, self._adapter_options(adapter.platform, options))).to_dict()

    async def health_check(self, platforms: list[str] | None = None) -> dict[str, Any]:
        if platforms is not None:
            if not isinstance(platforms, list) or len(platforms) > 35:
                raise ReachError("invalid_argument", "platforms must be a list of at most 35 platform IDs.")
            if not all(isinstance(platform, str) and platform.strip() for platform in platforms):
                raise ReachError("invalid_argument", "platforms must contain non-empty strings.")
            if len({platform.casefold() for platform in platforms}) != len(platforms):
                raise ReachError("invalid_argument", "platforms must not contain duplicates.")
        selected = [platform.casefold() for platform in platforms] if platforms else self.registry.implemented_platforms()
        options_by_platform = {
            platform: self._adapter_options(platform, None)
            for platform in selected
            if platform in self.registry.implemented_platforms()
        }
        results = await self.registry.status_for_health(selected, options_by_platform)
        return {"results": results, "healthy": bool(results) and all(item["status"] == "available" for item in results)}
