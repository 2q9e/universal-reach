"""Deterministic tests for the public GitHub adapter."""

from __future__ import annotations

import asyncio

import pytest

from universal_reach.adapters import github
from universal_reach.errors import ReachError

REPO = {
    "id": 12,
    "node_id": "R_kgDOExample",
    "full_name": "octocat/hello-world",
    "html_url": "https://github.com/octocat/hello-world",
    "description": "Hello world repository",
    "created_at": "2020-01-01T00:00:00Z",
    "updated_at": "2024-01-01T00:00:00Z",
    "stargazers_count": 42,
    "forks_count": 3,
    "open_issues_count": 2,
    "owner": {"login": "octocat"},
    "default_branch": "main",
    "language": "Python",
    "license": {"spdx_id": "MIT"},
    "topics": ["example"],
}
ISSUE = {
    "id": 34,
    "number": 5,
    "html_url": "https://github.com/octocat/hello-world/issues/5",
    "title": "A public issue",
    "body": "Issue details",
    "created_at": "2024-02-01T00:00:00Z",
    "updated_at": "2024-02-02T00:00:00Z",
    "closed_at": None,
    "state": "open",
    "comments": 1,
    "reactions": {"total_count": 2},
    "user": {"login": "octocat"},
    "labels": [{"name": "bug"}],
    "repository_url": "https://api.github.com/repos/octocat/hello-world",
}
COMMENT = {
    "id": 35,
    "html_url": "https://github.com/octocat/hello-world/issues/5#issuecomment-35",
    "body": "A reply",
    "created_at": "2024-02-03T00:00:00Z",
    "updated_at": "2024-02-03T00:00:00Z",
    "user": {"login": "other-user"},
}


def run(awaitable):
    return asyncio.run(awaitable)


def test_repository_search_maps_results_and_paginates(monkeypatch):
    calls = []

    async def fake_get_json(url, **kwargs):
        calls.append((url, kwargs))
        return {"total_count": 3, "incomplete_results": False, "items": [REPO]}

    monkeypatch.setattr(github, "get_json", fake_get_json)
    page = run(github.GitHubAdapter().search("  hello world  ", limit=1))

    assert calls[0][0] == "https://api.github.com/search/repositories"
    assert calls[0][1]["allowed_hosts"] == frozenset({"api.github.com"})
    assert calls[0][1]["params"] == {"q": "hello world", "per_page": 1, "page": 1}
    assert calls[0][1]["headers"]["Accept"] == "application/vnd.github+json"
    assert page.platform == "github"
    assert page.operation == "search"
    assert page.items[0].type == "repository"
    assert page.items[0].id == "12"
    assert page.items[0].author == "octocat"
    assert page.items[0].metrics["stars"] == 42
    assert page.pagination.cursor == "2"
    assert page.pagination.has_more is True
    assert page.complete is False


def test_repository_search_uses_numeric_page_cursor(monkeypatch):
    calls = []

    async def fake_get_json(url, **kwargs):
        calls.append(kwargs["params"])
        return {"total_count": 2, "incomplete_results": False, "items": [REPO]}

    monkeypatch.setattr(github, "get_json", fake_get_json)
    page = run(github.GitHubAdapter().search("hello", limit=1, cursor="2"))

    assert calls == [{"q": "hello", "per_page": 1, "page": 2}]
    assert page.pagination.has_more is False
    assert page.complete is True


def test_search_reports_api_incomplete_and_search_window(monkeypatch):
    async def fake_get_json(url, **kwargs):
        return {
            "total_count": 1001,
            "incomplete_results": True,
            "items": [REPO],
        }

    monkeypatch.setattr(github, "get_json", fake_get_json)
    page = run(github.GitHubAdapter().search("hello", limit=100, cursor="10"))

    assert page.pagination.has_more is False
    assert "incomplete" in page.warnings[0].lower()
    assert any("first 1000" in warning for warning in page.warnings)
    assert page.complete is False


def test_read_repository_and_issue_urls(monkeypatch):
    calls = []

    async def fake_get_json(url, **kwargs):
        calls.append(url)
        return REPO if len(calls) == 1 else ISSUE

    monkeypatch.setattr(github, "get_json", fake_get_json)
    adapter = github.GitHubAdapter()
    repo = run(adapter.read("https://github.com/octocat/hello-world/"))
    issue = run(adapter.read("https://api.github.com/repos/octocat/hello-world/issues/5"))

    assert calls == [
        "https://api.github.com/repos/octocat/hello-world",
        "https://api.github.com/repos/octocat/hello-world/issues/5",
    ]
    assert repo.title == "octocat/hello-world"
    assert repo.access.mode == "anonymous"
    assert repo.access.complete is True
    assert issue.type == "issue"
    assert issue.id == "34"
    assert issue.text == "Issue details"
    assert issue.extensions["labels"] == ["bug"]


def test_replies_maps_comments_and_paginates(monkeypatch):
    calls = []

    async def fake_get_json(url, **kwargs):
        calls.append(kwargs["params"])
        return [COMMENT] if kwargs["params"]["page"] == 1 else []

    monkeypatch.setattr(github, "get_json", fake_get_json)
    adapter = github.GitHubAdapter()
    first = run(adapter.replies("https://github.com/octocat/hello-world/issues/5", limit=1))
    second = run(adapter.replies(
        "https://github.com/octocat/hello-world/issues/5", limit=1, cursor=first.pagination.cursor
    ))

    assert calls == [{"per_page": 1, "page": 1}, {"per_page": 1, "page": 2}]
    assert first.items[0].type == "comment"
    assert first.items[0].author == "other-user"
    assert first.items[0].text == "A reply"
    assert first.pagination.cursor == "2"
    assert first.pagination.has_more is True
    assert second.items == []
    assert second.pagination.has_more is False


def test_profile_and_public_repository_posts(monkeypatch):
    async def fake_get_json(url, **kwargs):
        if url.endswith("/users/octocat"):
            return {
                "id": 1,
                "login": "octocat",
                "name": "The Octocat",
                "bio": "A cat",
                "html_url": "https://github.com/octocat",
                "created_at": "2011-01-25T00:00:00Z",
                "followers": 100,
                "following": 10,
                "public_repos": 8,
                "public_gists": 1,
            }
        assert url.endswith("/users/octocat/repos")
        assert kwargs["params"] == {
            "per_page": 1,
            "page": 2,
            "sort": "updated",
            "type": "owner",
        }
        return [REPO]

    monkeypatch.setattr(github, "get_json", fake_get_json)
    adapter = github.GitHubAdapter()
    profile = run(adapter.profile("octocat"))
    repos = run(adapter.profile_posts("octocat", limit=1, cursor="2"))

    assert profile.type == "profile"
    assert profile.author == "octocat"
    assert profile.title == "The Octocat"
    assert profile.metrics["followers"] == 100
    assert repos.items[0].type == "repository"
    assert repos.items[0].id == "12"
    assert repos.pagination.has_more is True


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/octocat/hello-world",
        "https://github.com.evil.example/octocat/hello-world",
        "https://api.github.com.evil.example/repos/octocat/hello-world",
        "https://user:secret@github.com/octocat/hello-world",
        "https://github.com/octocat/hello-world/actions",
        "https://github.com/octocat/hello-world/pull/1",
        "https://github.com/octocat%2Fother/hello-world",
    ],
)
def test_read_rejects_unsupported_or_unsafe_urls(url):
    with pytest.raises(ReachError) as caught:
        run(github.GitHubAdapter().read(url))
    assert caught.value.code == "bad_input"


def test_replies_require_issue_url():
    with pytest.raises(ReachError) as caught:
        run(github.GitHubAdapter().replies("https://github.com/octocat/hello-world"))
    assert caught.value.code == "bad_input"


@pytest.mark.parametrize(
    ("query", "limit", "cursor"),
    [
        ("", 10, None),
        ("hello", 0, None),
        ("hello", 101, None),
        ("hello", 1, "0"),
        ("hello", 1, "next"),
        ("hello", 100, "11"),
    ],
)
def test_search_rejects_bad_inputs(query, limit, cursor):
    with pytest.raises(ReachError) as caught:
        run(github.GitHubAdapter().search(query, limit=limit, cursor=cursor))
    assert caught.value.code == "bad_input"


def test_404_remains_not_found(monkeypatch):
    async def fake_get_json(url, **kwargs):
        raise ReachError("not_found", "The requested public content was not found.", details={"http_status": 404})

    monkeypatch.setattr(github, "get_json", fake_get_json)
    with pytest.raises(ReachError) as caught:
        run(github.GitHubAdapter().read("https://github.com/octocat/missing"))
    assert caught.value.code == "not_found"
    assert caught.value.details["http_status"] == 404


@pytest.mark.parametrize("status", [403, 429])
def test_github_rate_limit_errors_are_mapped(monkeypatch, status):
    async def fake_get_json(url, **kwargs):
        code = "private_or_inaccessible" if status == 403 else "rate_limited"
        raise ReachError(code, "HTTP error", retryable=status == 429, details={"http_status": status})

    monkeypatch.setattr(github, "get_json", fake_get_json)
    with pytest.raises(ReachError) as caught:
        run(github.GitHubAdapter().search("hello"))
    assert caught.value.code == "rate_limited"
    assert caught.value.retryable is True
