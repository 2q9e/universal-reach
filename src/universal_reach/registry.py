"""Platform registry and URL routing."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from universal_reach.adapters.base import Adapter
from universal_reach.capabilities import PLATFORM_CATALOG, list_capabilities
from universal_reach.errors import PlatformNotImplemented, ReachError
from universal_reach.models import AdapterStatus, PlatformCapability


class AdapterRegistry:
    def __init__(self, adapters: list[Adapter] | None = None) -> None:
        self._adapters: dict[str, Adapter] = {}
        for adapter in adapters or []:
            if adapter.platform in self._adapters:
                raise ValueError(f"Duplicate adapter registered: {adapter.platform}")
            if adapter.platform not in PLATFORM_CATALOG:
                raise ValueError(f"Adapter has no capability catalog entry: {adapter.platform}")
            self._adapters[adapter.platform] = adapter

    def capabilities(self) -> list[dict[str, object]]:
        return [
            self._adapters.get(capability.id, None).capability.to_dict()
            if capability.id in self._adapters
            else capability.to_dict()
            for capability in list_capabilities()
        ]

    def searchable_platforms(self) -> list[str]:
        return [
            platform for platform, adapter in self._adapters.items()
            if "search" in adapter.capability.operations
        ]

    def implemented_platforms(self) -> list[str]:
        return list(self._adapters)

    def capability(self, platform: str) -> PlatformCapability:
        try:
            return PLATFORM_CATALOG[platform.casefold()]
        except KeyError as exc:
            raise ReachError("unknown_platform", f"Unknown platform: {platform}.", details={"platform": platform}) from exc

    def get(self, platform: str) -> Adapter:
        key = platform.casefold()
        self.capability(key)
        adapter = self._adapters.get(key)
        if adapter is None:
            raise PlatformNotImplemented(key)
        return adapter

    def for_url(self, url: str) -> Adapter:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        matches: list[Adapter] = []
        for adapter in self._adapters.values():
            hosts = getattr(adapter, "url_hosts", frozenset())
            if host in hosts or any(host.endswith("." + known) for known in hosts):
                matches.append(adapter)
        if not matches:
            raise ReachError("unsupported_url", "No implemented platform adapter recognizes this URL.", details={"host": host})
        if len(matches) > 1:
            matches.sort(key=lambda item: max(len(h) for h in getattr(item, "url_hosts", ())), reverse=True)
        return matches[0]

    async def status(self, platform: str | None = None) -> list[dict[str, object]]:
        selected = [platform.casefold()] if platform else [cap.id for cap in list_capabilities()]
        results: list[dict[str, object]] = []
        for key in selected:
            adapter = self._adapters.get(key)
            capability = adapter.capability if adapter else self.capability(key)
            if adapter is None:
                result = AdapterStatus(
                    platform=key,
                    status="not_implemented",
                    message=capability.notes or "No adapter is implemented.",
                    operations=list(capability.operations),
                    access_mode=(
                        "anonymous"
                        if capability.anonymous_access in {"yes", "partial"}
                        else "unknown"
                    ),
                )
            else:
                result = AdapterStatus(
                    platform=key,
                    status="degraded",
                    message="Adapter is configured; live upstream health has not been checked.",
                    operations=list(adapter.capability.operations),
                    access_mode=(
                        "anonymous"
                        if capability.anonymous_access == "yes"
                        else "unknown"
                    ),
                )
            results.append(result.to_dict())
        return results

    async def status_for_health(
        self,
        platforms: list[str] | None = None,
        options_by_platform: dict[str, dict[str, object]] | None = None,
    ) -> list[dict[str, object]]:
        selected = [platform.casefold() for platform in platforms] if platforms else list(self._adapters)
        if len(selected) > 35:
            raise ValueError("At most 35 platforms may be checked at once.")
        semaphore = asyncio.Semaphore(5)

        async def check_one(key: str) -> dict[str, object]:
            capability = self.capability(key)
            adapter = self._adapters.get(key)
            if adapter is None:
                status = AdapterStatus(
                    platform=key,
                    status="not_implemented",
                    message=capability.notes or "No adapter is implemented.",
                    operations=[],
                    access_mode=(
                        "anonymous"
                        if capability.anonymous_access in {"yes", "partial"}
                        else "unknown"
                    ),
                )
            else:
                try:
                    async with semaphore:
                        status = await adapter.health_check((options_by_platform or {}).get(key))
                except Exception:
                    status = AdapterStatus(
                        platform=key,
                        status="unavailable",
                        message="The adapter health check failed.",
                        operations=list(adapter.capability.operations),
                    )
            return status.to_dict()

        return list(await asyncio.gather(*(check_one(key) for key in selected)))
