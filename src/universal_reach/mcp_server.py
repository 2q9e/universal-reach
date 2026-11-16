"""MCP tools for the local Universal Reach service."""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from universal_reach import __version__
from universal_reach.errors import ReachError
from universal_reach.service import ReachService

logger = logging.getLogger("universal_reach.mcp")


def _result(payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=payload,
        is_error=is_error,
    )


async def _execute(call: Callable[[], Awaitable[dict[str, Any]]]) -> CallToolResult:
    try:
        return _result(await call())
    except ReachError as exc:
        return _result({"ok": False, "error": exc.to_dict()}, is_error=True)
    except Exception:
        logger.exception("Unexpected Universal Reach MCP tool failure")
        error = ReachError("internal_error", "The local adapter failed unexpectedly.")
        return _result({"ok": False, "error": error.to_dict()}, is_error=True)


def create_mcp_server(service: ReachService | None = None) -> MCPServer:
    if service is None:
        from universal_reach.adapters.factory import build_registry

        service = ReachService(build_registry())

    mcp = MCPServer("Universal Reach", version=__version__)

    @mcp.tool()
    async def platforms_list() -> CallToolResult:
        """List the 35 catalogued platforms and their implemented capabilities."""
        return _result({"platforms": service.platforms_list()})

    @mcp.tool()
    async def platform_status(platform: str | None = None) -> CallToolResult:
        """Report configured adapter capabilities; this does not make live requests."""
        return await _execute(lambda: service.platform_status(platform))

    @mcp.tool()
    async def feed(
        platform: str,
        category: str,
        limit: int = 20,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Retrieve a public ranked or recent feed where the platform supports one."""
        return await _execute(lambda: service.feed(platform, category, limit, cursor, options))

    @mcp.tool()
    async def search(
        platform: str,
        query: str,
        limit: int = 10,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Search public content on one explicitly named platform."""
        return await _execute(lambda: service.search(platform, query, limit, cursor, options))

    @mcp.tool()
    async def search_multi(
        query: str,
        platforms: list[str] | None = None,
        limit: int = 10,
        options: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Search explicitly requested platforms, or all implemented searchable adapters."""
        return await _execute(lambda: service.search_multi(platforms, query, limit, options))

    @mcp.tool()
    async def read(
        url: str,
        platform: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Read publicly accessible content at an exact URL."""
        return await _execute(lambda: service.read(url, platform, options))

    @mcp.tool()
    async def replies(
        url: str,
        platform: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Retrieve available public comments or replies for a URL."""
        return await _execute(lambda: service.replies(url, platform, limit, cursor, options))

    @mcp.tool()
    async def profile(
        platform: str,
        username: str,
        options: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Read publicly available profile information."""
        return await _execute(lambda: service.profile(platform, username, options))

    @mcp.tool()
    async def profile_posts(
        platform: str,
        username: str,
        limit: int = 20,
        cursor: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Retrieve available public content associated with a profile."""
        return await _execute(lambda: service.profile_posts(platform, username, limit, cursor, options))

    @mcp.tool()
    async def media(
        url: str,
        platform: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Read supported public media metadata for a URL."""
        return await _execute(lambda: service.media(url, platform, options))

    @mcp.tool()
    async def transcript(
        url: str,
        platform: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Retrieve captions or transcripts only when the platform adapter supports them."""
        return await _execute(lambda: service.transcript(url, platform, options))

    @mcp.tool()
    async def archive(
        platform: str,
        query: str,
        options: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Access a platform archive only when a documented adapter supports it."""
        return await _execute(lambda: service.archive(platform, query, options))

    @mcp.tool()
    async def health_check(platforms: list[str] | None = None) -> CallToolResult:
        """Make bounded live checks against selected implemented adapters."""
        return await _execute(lambda: service.health_check(platforms))

    return mcp


def run_server(service: ReachService | None = None) -> None:
    """Run the MCP server over local stdio."""
    create_mcp_server(service).run()
