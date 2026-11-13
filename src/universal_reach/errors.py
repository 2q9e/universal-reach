"""Stable errors returned by the CLI and MCP layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ReachError(Exception):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class UnsupportedOperation(ReachError):
    def __init__(self, platform: str, operation: str) -> None:
        super().__init__(
            "unsupported_operation",
            f"The {platform} adapter does not support {operation}.",
            details={"platform": platform, "operation": operation},
        )


class PlatformNotImplemented(ReachError):
    def __init__(self, platform: str) -> None:
        super().__init__(
            "not_implemented",
            f"No retrieval adapter is implemented for {platform} yet.",
            details={"platform": platform},
        )
