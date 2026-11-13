"""Normalized data types shared by adapters, the CLI, and MCP tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

AccessMode = Literal["anonymous", "authenticated", "unknown"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class AccessInfo:
    mode: AccessMode = "anonymous"
    complete: bool = False


@dataclass(slots=True)
class Pagination:
    cursor: str | None = None
    has_more: bool = False
    detail: str | None = None


@dataclass(slots=True)
class MediaItem:
    kind: str
    url: str | None = None
    mime_type: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Record:
    platform: str
    type: str
    id: str
    url: str | None = None
    author: str | None = None
    title: str | None = None
    text: str | None = None
    created_at: str | None = None
    retrieved_at: str = field(default_factory=utc_now)
    metrics: dict[str, int | float | str | None] = field(default_factory=dict)
    media: list[MediaItem] = field(default_factory=list)
    replies: list[Record] = field(default_factory=list)
    pagination: Pagination | None = None
    access: AccessInfo = field(default_factory=AccessInfo)
    extensions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ResultPage:
    platform: str
    operation: str
    items: list[Record] = field(default_factory=list)
    pagination: Pagination = field(default_factory=Pagination)
    complete: bool = True
    warnings: list[str] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class PlatformCapability:
    id: str
    name: str
    group: str
    status: Literal["implemented", "partial", "restricted", "planned"]
    operations: tuple[str, ...] = ()
    anonymous_access: Literal["yes", "partial", "no", "unknown"] = "unknown"
    auth_note: str | None = None
    notes: str | None = None
    documentation: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AdapterStatus:
    platform: str
    status: Literal["available", "degraded", "unavailable", "not_implemented"]
    checked_at: str = field(default_factory=utc_now)
    message: str | None = None
    operations: list[str] = field(default_factory=list)
    access_mode: AccessMode = "anonymous"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
