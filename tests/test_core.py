from __future__ import annotations

import asyncio
import json
import os

import pytest

from universal_reach.adapters.factory import build_registry
from universal_reach.adapters import bluesky, bogleheads, discord, forem, github, hackernews, public_forums, reddit, stackexchange, telegram, v2ex, x, youtube
from universal_reach.config import load_config, save_config
from universal_reach.errors import PlatformNotImplemented, ReachError
from universal_reach.mcp_server import create_mcp_server
from universal_reach.security import validate_https_url, validate_public_instance_url
from universal_reach.service import ReachService, validate_limit, validate_query


def test_platform_catalog_and_loaded_adapters_are_truthful(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_ALLOWED_CHANNEL_IDS", raising=False)
    registry = build_registry()
    catalog = registry.capabilities()

    assert len(catalog) == 35
    assert len({item["id"] for item in catalog}) == 35
    implemented = [item for item in catalog if item["operations"]]
    assert len(implemented) == 15
    assert {item["id"] for item in implemented} == {
        "github", "stackoverflow", "stackexchange", "hackernews", "v2ex",
        "bogleheads", "reddit", "telegram", "youtube", "x", "4chan", "lemmy",
        "discourse", "devto", "bluesky",
    }
    assert all(item["status"] == "partial" for item in implemented)


def test_discord_adapter_requires_explicit_channel_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNEL_IDS", "222222222222222222,333333333333333333")
    registry = build_registry()

    discord_capability = next(item for item in registry.capabilities() if item["id"] == "discord")
    assert discord_capability["status"] == "partial"
    assert discord_capability["operations"] == ("read", "replies")
    assert "discord" in registry.implemented_platforms()
    assert "search" not in registry.get("hackernews").capability.operations


def test_unsupported_platform_operation_returns_explicit_error() -> None:
    service = ReachService(build_registry(), config={})
    with pytest.raises(PlatformNotImplemented) as error:
        asyncio.run(service.search("quora", "example"))
    assert error.value.code == "not_implemented"
    assert error.value.details["platform"] == "quora"


@pytest.mark.asyncio
async def test_multi_search_reports_unsupported_adapter_as_partial_failure() -> None:
    service = ReachService(build_registry(), config={})
    result = await service.search_multi(["hackernews"], "example")

    assert result["partial"] is True
    assert result["failed_platforms"] == ["hackernews"]
    assert result["results"][0]["error"]["code"] == "unsupported_operation"


@pytest.mark.asyncio
async def test_status_does_not_claim_live_health_and_lists_all_platforms() -> None:
    service = ReachService(build_registry(), config={})
    status = await service.platform_status()
    assert len(status["platforms"]) == 35
    bluesky = next(row for row in status["platforms"] if row["platform"] == "bluesky")
    assert bluesky["status"] == "degraded"
    assert "not been checked" in bluesky["message"]
    reddit = next(row for row in status["platforms"] if row["platform"] == "reddit")
    assert reddit["access_mode"] == "unknown"


@pytest.mark.asyncio
async def test_health_check_reaches_each_adapter_and_uses_configured_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_ALLOWED_CHANNEL_IDS", raising=False)
    calls: list[tuple[str, dict[str, object]]] = []

    async def github_json(url: str, **kwargs: object) -> object:
        return {"resources": {"core": {"remaining": 60}}}

    async def stack_json(url: str, **kwargs: object) -> object:
        return {"items": [{"api_site_parameter": "stackoverflow"}]}

    async def hn_json(url: str, **kwargs: object) -> object:
        return 100

    async def public_json(url: str, **kwargs: object) -> object:
        calls.append((url, kwargs))
        if url.endswith("/api/v4/search"):
            return {"posts": [], "comments": []}
        if url.endswith("/latest.json"):
            return {"topic_list": {"topics": []}}
        raise AssertionError(url)

    async def forem_json(url: str, **kwargs: object) -> object:
        return []

    async def bluesky_json(url: str, **kwargs: object) -> object:
        return {"posts": []}

    async def fourchan_json(self, path: str, **kwargs: object) -> object:
        return {"boards": [{"board": "g"}]}

    monkeypatch.setattr(github, "get_json", github_json)
    monkeypatch.setattr(stackexchange, "get_json", stack_json)
    monkeypatch.setattr(hackernews, "get_json", hn_json)
    monkeypatch.setattr(public_forums, "get_json", public_json)
    monkeypatch.setattr(public_forums, "validate_public_instance_url", lambda url: (url.rstrip("/"), frozenset({url.split("/")[2]})))
    monkeypatch.setattr(public_forums.FourChanAdapter, "_api_json", fourchan_json)
    monkeypatch.setattr(forem, "get_json", forem_json)
    monkeypatch.setattr(bluesky, "get_json", bluesky_json)

    async def available_status(self, options=None):
        from universal_reach.models import AdapterStatus
        return AdapterStatus(platform=self.platform, status="available", operations=list(self.capability.operations))

    async def dormant_reddit_status(self, options=None):
        from universal_reach.models import AdapterStatus
        return AdapterStatus(platform=self.platform, status="degraded", operations=list(self.capability.operations), message="Approval and OAuth configuration are required.")

    for adapter_class in (bogleheads.BogleheadsAdapter, telegram.TelegramAdapter, v2ex.V2EXAdapter, youtube.YouTubeAdapter):
        monkeypatch.setattr(adapter_class, "health_check", available_status)
    monkeypatch.setattr(reddit.RedditAdapter, "health_check", dormant_reddit_status)

    service = ReachService(
        build_registry(),
        config={"lemmy_instance": "https://lemmy.example", "discourse_instances": {"main": "https://discourse.example"}},
    )
    result = await service.health_check()

    assert result["healthy"] is False
    assert len(result["results"]) == 15
    assert sum(row["status"] == "available" for row in result["results"]) == 13
    assert {row["platform"] for row in result["results"] if row["status"] == "degraded"} == {"reddit", "x"}
    assert any(url == "https://lemmy.example/api/v4/search" and kwargs["public_instance"] is True for url, kwargs in calls)
    assert any(url == "https://discourse.example/latest.json" and kwargs["public_instance"] is True for url, kwargs in calls)


@pytest.mark.asyncio
async def test_real_mcp_client_protocol_round_trip() -> None:
    from mcp import Client

    server = create_mcp_server(ReachService(build_registry(), config={}))
    async with Client(server, mode="legacy") as client:
        tools = await client.list_tools()
        result = await client.call_tool("platform_status", {"platform": "youtube"})

    assert "platform_status" in {tool.name for tool in tools.tools}
    assert result.is_error is False
    assert result.structured_content["platforms"][0]["platform"] == "youtube"
    assert result.structured_content["platforms"][0]["status"] == "degraded"


@pytest.mark.asyncio
async def test_mcp_registers_expected_tools_and_marks_errors() -> None:
    server = create_mcp_server(ReachService(build_registry(), config={}))
    tools = await server.list_tools()
    names = {tool.name for tool in tools}
    assert names == {
        "platforms_list", "platform_status", "search", "search_multi", "read",
        "replies", "profile", "profile_posts", "media", "transcript", "archive", "health_check",
    }

    result = await server.call_tool("search", {"platform": "quora", "query": "example"})
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "not_implemented"


def test_limit_and_query_validation_are_explicit() -> None:
    assert validate_limit(10) == 10
    assert validate_query("  hello  ") == "hello"
    for invalid in (0, -1, True, "1"):
        with pytest.raises(ReachError):
            validate_limit(invalid)
    for invalid in ("", "  ", "x" * 1001, None):
        with pytest.raises(ReachError):
            validate_query(invalid)


def test_security_rejects_credentials_and_unsafe_instance_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ReachError):
        validate_https_url("https://user:secret@api.example.test/items", {"api.example.test"})
    with pytest.raises(ReachError):
        validate_https_url("http://api.example.test/items", {"api.example.test"})
    with pytest.raises(ReachError):
        validate_public_instance_url("https://127.0.0.1")
    with pytest.raises(ReachError):
        validate_public_instance_url("https://public.example.test/path")

    monkeypatch.setattr(
        "universal_reach.security.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("10.1.2.3", 443))],
    )
    with pytest.raises(ReachError):
        validate_public_instance_url("https://public.example.test")


def test_config_rejects_credentials_and_writes_private_file(tmp_path) -> None:
    path = tmp_path / "config.json"
    with pytest.raises(ReachError):
        save_config({"github_token": "should-not-be-accepted"}, path)

    save_config({"lemmy_instance": "https://lemmy.world", "log_level": "INFO"}, path)
    assert load_config(path) == {
        "lemmy_instance": "https://lemmy.world",
        "discourse_instances": {},
        "log_level": "INFO",
    }
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["log_level"] == "INFO"


def test_cli_json_and_error_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    from universal_reach.cli import main

    assert main(["platforms"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert len(output["platforms"]) == 35

    assert main(["search", "--platform", "hackernews", "--query", "example"]) == 2
    failure = json.loads(capsys.readouterr().out)
    assert failure["ok"] is False
    assert failure["error"]["code"] == "unsupported_operation"

    assert main(["--format", "jsonl", "search-multi", "--query", "example", "--platform", "hackernews"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(rows) == 1
    assert rows[0]["platform"] == "hackernews"
    assert rows[0]["ok"] is False


@pytest.mark.asyncio
async def test_doctor_performs_live_adapter_checks_and_reports_mcp_readiness(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from argparse import Namespace
    from universal_reach import cli

    async def failed_health_check():
        return {
            "results": [{"platform": "github", "status": "unavailable", "message": "network_error"}],
            "healthy": False,
        }

    class FakeService:
        health_check = staticmethod(failed_health_check)

    monkeypatch.setattr("universal_reach.config.load_config", lambda: {
        "lemmy_instance": "https://lemmy.example",
        "discourse_instances": {"main": "https://forum.example"},
        "log_level": "INFO",
    })
    monkeypatch.setattr("universal_reach.config.config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None)

    result = await cli._dispatch(Namespace(command="doctor"), FakeService())

    assert result["ok"] is False
    assert result["healthy"] is False
    assert result["mcp"] == {"ready": False, "transport": "stdio"}
    assert result["adapter_health"][0]["status"] == "unavailable"
    assert result["configuration"]["configured_discourse_instances"] == ["main"]


def test_malformed_url_returns_reach_error_instead_of_unhandled_value_error() -> None:
    service = ReachService(build_registry(), config={})
    with pytest.raises(ReachError) as error:
        service._for_url("https://[broken", None)
    assert error.value.code == "invalid_url"


@pytest.mark.asyncio
async def test_health_check_validates_platform_arguments() -> None:
    service = ReachService(build_registry(), config={})
    for value in ("bluesky", [1], ["bluesky", "BLUESKY"], ["a"] * 36):
        with pytest.raises(ReachError):
            await service.health_check(value)
