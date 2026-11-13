"""Command-line interface for Universal Reach."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import platform as host_platform
import sys
from typing import Any

from universal_reach import __version__
from universal_reach.adapters.factory import build_registry
from universal_reach.errors import ReachError
from universal_reach.service import ReachService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="universal-reach", description="Read public content through local platform adapters.")
    parser.add_argument("--version", action="version", version=f"Universal Reach {__version__}")
    parser.add_argument("--format", choices=("json", "jsonl"), default="json", help="Output format (default: json).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("platforms", help="List platform capability declarations.")
    status = sub.add_parser("status", help="Show configured adapter status without contacting platforms.")
    status.add_argument("--platform")

    search = sub.add_parser("search", help="Search one platform.")
    search.add_argument("--platform", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--cursor")
    _options_argument(search)

    multi = sub.add_parser("search-multi", help="Search several implemented platforms.")
    multi.add_argument("--query", required=True)
    multi.add_argument("--platform", action="append", dest="platforms", help="Repeat to select multiple platforms; defaults to all searchable adapters.")
    multi.add_argument("--limit", type=int, default=10)
    _options_argument(multi)

    read = sub.add_parser("read", help="Read a public item by URL.")
    read.add_argument("--url", required=True)
    read.add_argument("--platform")
    _options_argument(read)

    replies = sub.add_parser("replies", help="Read public comments or replies for a URL.")
    replies.add_argument("--url", required=True)
    replies.add_argument("--platform")
    replies.add_argument("--limit", type=int, default=50)
    replies.add_argument("--cursor")
    _options_argument(replies)

    profile = sub.add_parser("profile", help="Read a public profile.")
    profile.add_argument("--platform", required=True)
    profile.add_argument("--username", required=True)
    _options_argument(profile)

    posts = sub.add_parser("profile-posts", help="Read public content from a profile.")
    posts.add_argument("--platform", required=True)
    posts.add_argument("--username", required=True)
    posts.add_argument("--limit", type=int, default=20)
    posts.add_argument("--cursor")
    _options_argument(posts)

    for name, help_text in (("media", "Read supported public media metadata."), ("transcript", "Read available public captions or transcripts.")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--url", required=True)
        command.add_argument("--platform")
        _options_argument(command)

    archive = sub.add_parser("archive", help="Use an archive where an adapter supports it.")
    archive.add_argument("--platform", required=True)
    archive.add_argument("--query", required=True)
    _options_argument(archive)

    health = sub.add_parser("health-check", help="Make bounded live checks for selected adapters.")
    health.add_argument("--platform", action="append", dest="platforms", help="Repeat to select platforms; defaults to implemented adapters.")
    sub.add_parser("doctor", help="Validate local configuration, MCP readiness, and live adapter health.")
    sub.add_parser("mcp", help="Run the local MCP server over stdio.")
    return parser


def _options_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--options", default="{}", help="Adapter options as a JSON object (default: {}).")


def _parse_options(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReachError("invalid_argument", "--options must contain valid JSON.") from exc
    if not isinstance(value, dict):
        raise ReachError("invalid_argument", "--options must be a JSON object.")
    return value


def _emit(value: Any, output_format: str) -> None:
    if output_format == "jsonl" and isinstance(value, dict):
        items = value.get("items")
        if isinstance(items, list):
            for item in items:
                print(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            return
        results = value.get("results")
        if isinstance(results, list):
            for result in results:
                print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def _dispatch(args: argparse.Namespace, service: ReachService) -> Any:
    command = args.command
    if command == "platforms":
        return {"platforms": service.platforms_list()}
    if command == "status":
        return await service.platform_status(args.platform)
    if command == "doctor":
        from universal_reach.config import config_path, load_config

        config = load_config()
        health = await service.health_check()
        mcp_ready = importlib.util.find_spec("mcp") is not None
        return {
            "ok": bool(health["healthy"]) and mcp_ready,
            "application": {"name": "Universal Reach", "version": __version__},
            "runtime": {
                "os": host_platform.system(),
                "architecture": host_platform.machine(),
                "python": host_platform.python_version(),
            },
            "configuration": {
                "valid": True,
                "path": str(config_path()),
                "configured_lemmy_instance": bool(config.get("lemmy_instance")),
                "configured_discourse_instances": sorted(config.get("discourse_instances", {})),
            },
            "mcp": {"ready": mcp_ready, "transport": "stdio"},
            "optional_dependencies": [],
            "adapter_health": health["results"],
            "healthy": health["healthy"],
        }
    if command == "health-check":
        return await service.health_check(args.platforms)

    options = _parse_options(getattr(args, "options", None))
    if command == "search":
        return await service.search(args.platform, args.query, args.limit, args.cursor, options)
    if command == "search-multi":
        return await service.search_multi(args.platforms, args.query, args.limit, options)
    if command == "read":
        return await service.read(args.url, args.platform, options)
    if command == "replies":
        return await service.replies(args.url, args.platform, args.limit, args.cursor, options)
    if command == "profile":
        return await service.profile(args.platform, args.username, options)
    if command == "profile-posts":
        return await service.profile_posts(args.platform, args.username, args.limit, args.cursor, options)
    if command == "media":
        return await service.media(args.url, args.platform, options)
    if command == "transcript":
        return await service.transcript(args.url, args.platform, options)
    if command == "archive":
        return await service.archive(args.platform, args.query, options)
    raise ReachError("invalid_command", f"Unknown command: {command}.")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "mcp":
        from universal_reach.mcp_server import run_server

        run_server()
        return 0
    try:
        value = asyncio.run(_dispatch(args, ReachService(build_registry())))
    except ReachError as exc:
        _emit({"ok": False, "error": exc.to_dict()}, args.format)
        return 2
    except KeyboardInterrupt:
        return 130
    _emit(value, args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
