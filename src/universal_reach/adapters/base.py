"""Adapter contract. CLI and MCP calls share these implementations."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable

from universal_reach.capabilities import get_capability
from universal_reach.errors import UnsupportedOperation
from universal_reach.models import AdapterStatus, PlatformCapability, Record, ResultPage


class Adapter:
    capability: PlatformCapability

    def __init__(self, capability: PlatformCapability) -> None:
        self.capability = capability

    @property
    def platform(self) -> str:
        return self.capability.id

    async def search(
        self, query: str, limit: int = 10, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        raise UnsupportedOperation(self.platform, "search")

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        raise UnsupportedOperation(self.platform, "read")

    async def replies(
        self, url: str, limit: int = 50, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        raise UnsupportedOperation(self.platform, "replies")

    async def profile(self, username: str, options: dict[str, Any] | None = None) -> Record:
        raise UnsupportedOperation(self.platform, "profile")

    async def profile_posts(
        self, username: str, limit: int = 20, cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ResultPage:
        raise UnsupportedOperation(self.platform, "profile_posts")

    async def media(self, url: str, options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        raise UnsupportedOperation(self.platform, "media")

    async def transcript(self, url: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        raise UnsupportedOperation(self.platform, "transcript")

    async def archive(self, query: str, options: dict[str, Any] | None = None) -> ResultPage:
        raise UnsupportedOperation(self.platform, "archive")

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        return AdapterStatus(
            platform=self.platform,
            status="degraded",
            operations=list(self.capability.operations),
            message="This adapter does not implement a live health check.",
        )


def adapter_capability(platform: str, operations: Iterable[str]) -> PlatformCapability:
    """Return the catalog entry updated to reflect a real adapter implementation."""
    base = get_capability(platform)
    return replace(base, status="partial", operations=tuple(operations))
