# Universal Reach

Universal Reach is a local, read-only CLI and Model Context Protocol (MCP) server for retrieving public content from supported social platforms and online communities. It makes direct requests to documented public APIs; there is no Universal Reach cloud service.

The project catalogs all 35 requested platforms and currently implements partial adapters for 14 platform IDs. The catalog distinguishes working adapters from restricted and not-yet-implemented platforms. It does not claim that a listed platform is fully supported.

## Architecture

The CLI and stdio MCP server share one `ReachService` and a capability registry. Platform-specific adapters live under `src/universal_reach/adapters/`; they normalize responses into common records and use the shared bounded HTTP client. The JSON config stores public instance URLs and logging preferences only. Optional Reddit OAuth and V2EX PAT credentials are read from environment variables when an operation needs them. There is no browser automation backend or Universal Reach cloud service.

## Features

- One Python package, CLI, and stdio MCP server.
- Structured, normalized records with explicit pagination, access mode, and partial-result metadata.
- Read-only anonymous adapters, plus opt-in Reddit OAuth and V2EX Personal Access Token reads.
- Host allowlists, HTTPS-only requests, redirect checks, bounded timeouts and retries, input validation, and safe error messages.
- Configurable public Lemmy and Discourse instances, with DNS checks that reject private or non-global addresses.
- No browser cookies or account passwords are used. Reddit requires prior platform approval; Reddit and V2EX credentials are read from environment variables and never stored in the local JSON configuration.

## Installation

Requires CPython 3.10 or newer. The package is pure Python and supports Linux, macOS, and Windows where a compatible Python runtime is available. ARM64 has not been separately release-tested.

### Linux and macOS

```sh
git clone https://github.com/2q9e/universal-reach.git
cd universal-reach
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
```

### Windows PowerShell

```powershell
git clone https://github.com/2q9e/universal-reach.git
Set-Location universal-reach
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
```

The install includes the MCP Python SDK and the `universal-reach` console command. To install without test tools, use `python -m pip install .`.

## CLI

Commands return JSON by default. Use `--format jsonl` before the subcommand to emit result items as JSON Lines; multi-platform and health-check commands emit one platform result object per line.

```sh
universal-reach platforms
universal-reach status --platform bluesky
universal-reach search --platform bluesky --query "public transit"
universal-reach search --platform github --query "mcp server"
universal-reach search-multi --query "public transit" --platform bluesky --platform github
universal-reach read --url https://github.com/modelcontextprotocol/python-sdk
universal-reach replies --url https://news.ycombinator.com/item?id=1 --platform hackernews
universal-reach profile --platform github --username octocat
universal-reach health-check --platform bluesky
universal-reach doctor
```

The service validates input and returns a structured error with a nonzero exit status when an operation is unsupported or upstream access fails. `status` only reports configured capability; `health-check` makes bounded live requests. `doctor` validates local configuration, checks whether the MCP SDK is installed, and performs live checks against implemented adapters.

Some adapters need public instance or board options:

```sh
universal-reach search --platform 4chan --query "example" --options '{"board":"g"}'
universal-reach search --platform lemmy --query "example" --options '{"instance_url":"https://lemmy.world"}'
universal-reach search --platform discourse --query "example" --options '{"instance_url":"https://meta.discourse.org"}'
```

Lemmy defaults to `https://lemmy.world`. Persistent local configuration is JSON at:

- Linux and macOS: `${XDG_CONFIG_HOME:-~/.config}/universal-reach/config.json`
- Windows: `%APPDATA%\UniversalReach\config.json`

Example:

```json
{
  "lemmy_instance": "https://lemmy.world",
  "discourse_instances": {
    "meta": "https://meta.discourse.org"
  },
  "log_level": "WARNING"
}
```

Configuration contains public instance URLs and a log level; it has no credential fields.

## MCP integration

The server uses local stdio. Install Universal Reach into a virtual environment and configure the agent to run that environment's executable. Keep stdin and stdout reserved for MCP; diagnostics go to stderr/logging.

Codex CLI:

```sh
codex mcp add universal-reach -- /absolute/path/to/universal-reach/.venv/bin/universal-reach mcp
```

On Windows, use the executable path `C:\path\to\universal-reach\.venv\Scripts\universal-reach.exe` in the command appropriate for the installed Codex version.

Claude Code:

```sh
claude mcp add --transport stdio universal-reach -- /absolute/path/to/universal-reach/.venv/bin/universal-reach mcp
```

The MCP server exposes `platforms_list`, `platform_status`, `search`, `search_multi`, `read`, `replies`, `profile`, `profile_posts`, `media`, `transcript`, `archive`, and `health_check`. Unsupported operations return MCP tool errors rather than invented data. `platform_status` does not contact upstream services; `health_check` does.

## Platform capability matrix

The table describes operations present in this source release. A check means an adapter operation exists; upstream visibility, instance settings, rate limits, and content availability still apply. “Planned” means no adapter is implemented here. “Restricted” means an access requirement or policy prevents an anonymous adapter in this release.

| Platform | Status | Search | Read | Replies | Profile | Profile posts | Anonymous access |
|---|---|---:|---:|---:|---:|---:|---|
| Reddit | Partial | ✓ | ✓ | ✓ | ✓ | ✓ | No anonymous access; OAuth adapter sends no request without explicit Reddit approval and app configuration |
| Quora | Planned | — | — | — | — | — | Not verified |
| Stack Overflow | Partial | ✓ | ✓ | ✓ | ✓ | ✓ | Yes; Stack Exchange API quota and paging rules apply |
| Stack Exchange | Partial | ✓ | ✓ | ✓ | ✓ | ✓ | Yes; select `options.site` |
| GitHub | Partial | ✓ | ✓ | ✓ | ✓ | ✓ | Yes; unauthenticated rate limits apply |
| Steam Community | Restricted | — | — | — | — | — | No documented public discussions route; automated HTML reading is excluded |
| 4chan | Partial | Limited* | ✓ | ✓ | — | — | Yes; public JSON API |
| Hacker News | Partial | — | ✓ | ✓ | ✓ | ✓ | Yes; official Firebase API |
| XDA Forums | Planned | — | — | — | — | — | Not verified |
| V2EX | Partial | — | ✓ | ✓ | ✓ | — | Public profile route is anonymous; topic and replies routes require V2EX_ACCESS_TOKEN |
| Zhihu | Planned | — | — | — | — | — | Not verified |
| Baidu Tieba | Planned | — | — | — | — | — | Not verified |
| ResetEra | Planned | — | — | — | — | — | Not verified |
| Lemmy | Partial | ✓ | ✓ | ✓ | — | — | Yes, on public configured instances |
| Discourse forums | Partial | ✓ | ✓ | ✓ | — | — | Yes, on public configured instances |
| DEV Community | Partial | ✓ | ✓ | — | ✓ | ✓ | Yes for implemented public Forem routes |
| Indie Hackers | Planned | — | — | — | — | — | Not verified |
| Bogleheads | Partial | — | ✓ | — | — | — | Anonymous exact-post reads only when the post is in the bounded topic Atom feed |
| Facebook | Restricted | — | — | — | — | — | Official content access requires authorized API access |
| Instagram | Restricted | — | — | — | — | — | Official API access is account- and permission-scoped |
| YouTube | Partial | — | — | — | — | ✓ | Anonymous recent channel Atom feed; broader Data API access requires a Google API key |
| WhatsApp | Planned | — | — | — | — | — | No public read API verified |
| TikTok | Restricted | — | — | — | — | — | Official public-content APIs require authorization/approval |
| WeChat | Planned | — | — | — | — | — | Not verified |
| Telegram | Partial | — | ✓ | — | ✓ | ✓ | Anonymous public channel previews; hidden posts and groups are unsupported |
| Snapchat | Restricted | — | — | — | — | — | Public Profile API is allowlisted and token-based |
| Douyin | Restricted | — | — | — | — | — | Official access requires OAuth and app credentials |
| Kuaishou | Restricted | — | — | — | — | — | Official access requires app credentials and tokens |
| Weibo | Planned | — | — | — | — | — | Not verified |
| Pinterest | Restricted | — | — | — | — | — | Official API requires OAuth authorization |
| X / Twitter | Restricted | — | — | — | — | — | Developer app and bearer token required |
| Threads | Restricted | — | — | — | — | — | Meta app authorization and scopes required |
| LinkedIn | Restricted | — | — | — | — | — | OAuth and product access required |
| Discord | Restricted | — | — | — | — | — | Bot authorization and channel permissions required |
| Bluesky | Partial | ✓ | ✓ | ✓ | ✓ | ✓ | Yes through public AppView GET endpoints |

`—` means not implemented, rather than a claim that the platform itself lacks the capability. `*` 4chan search is limited to matching preview text in the active catalog of one explicitly selected board; it is not global full-text search. Hacker News has no search operation in its official Firebase API. Lemmy and Discourse need an instance URL and only return content visible to anonymous visitors.

### Other operations

No adapter in this release implements media extraction, transcripts, or archives. The CLI and MCP server expose those operations so unsupported requests receive the same explicit error shape across clients.

## Security and privacy

- Requests are read-only HTTP GETs to documented public APIs, official feeds, or public previews. Adapters do not bypass authentication or access controls.
- Static API hosts are allowlisted; URL inputs require HTTPS and reject credentials and nonstandard ports. Redirects are revalidated.
- Lemmy and Discourse origins must be public HTTPS hosts. Resolution rejects localhost, private, link-local, and other non-global addresses, then pins the connection to an address that passed validation. These two adapters connect directly and ignore proxy environment settings so a proxy cannot resolve the hostname to a different address.
- The local MCP server listens on stdio; this package does not open an inbound network server.
- No browser cookies or account passwords are imported. The local JSON config has no credential fields; optional PAT/OAuth values are environment-only.
- Logs avoid request query strings and response bodies. Upstream public content can still contain personal data; callers should apply their own retention and display policies.

Optional credentials are environment-only: set V2EX_ACCESS_TOKEN for V2EX topic reads/replies. For Reddit, set REDDIT_API_APPROVED=true only after Reddit has approved the app and use case, then provide REDDIT_ACCESS_TOKEN, REDDIT_CLIENT_ID, and REDDIT_USERNAME. Reddit requests remain disabled when any required setting is absent. OS credential-store integration is not included.

## Troubleshooting

- Run `universal-reach doctor` to inspect local configuration, MCP SDK availability, and live health checks for implemented adapters.
- Run `universal-reach status --platform <id>` for configured capability without contacting the platform; use `health-check` to test upstream reachability.
- Reddit stays inactive unless the app and use case have explicit platform approval and all required environment variables are set. V2EX profile reads are anonymous; topic reads and replies need `V2EX_ACCESS_TOKEN`.
- `rate_limited`, `private_or_inaccessible`, and `unsupported_operation` are expected structured outcomes. Retry only after the platform's limit window, use content visible to anonymous visitors, or choose an operation the adapter advertises.

## Performance configuration

The shared HTTP layer bounds response size, timeout, retries, redirects, and destination hosts. Health checks run at most five upstream requests at a time. Adapter page sizes and feed windows remain platform-specific; the capability response and result warnings report when a source cannot confirm complete pagination. There is no user-configurable global timeout or cache in this release.

## Development and tests

```sh
python -m pip install -e '.[test]'
python -m pytest
```

Tests use deterministic mocked responses and should not require platform accounts or make live requests. CI runs the test suite on Linux, macOS, and Windows for supported Python versions.

## Known limitations

- Fourteen of the 35 catalog entries have adapters; these are partial implementations, not full coverage of every platform feature. Reddit remains dormant until its approval and OAuth settings are present. Steam discussions remain restricted.
- Public endpoints may change, return partial data, enforce rate limits, or deny particular content. Errors preserve those limits instead of retrying indefinitely.
- Anonymous GitHub and Stack Exchange quotas are limited. Stack Exchange page depth is bounded; GitHub search exposes a bounded result window.
- 4chan calls are rate-limited in-process and search is board-catalog-only. Discourse and Lemmy depend on the selected instance.
- No cross-platform ARM matrix has been run. Browser automation, authenticated sessions, media downloads, transcript extraction, and archive integrations are not included.

## API references

Adapter routes and access declarations are based on the platforms' published documentation:

- [GitHub REST API](https://docs.github.com/en/rest) and [rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
- [Stack Exchange API v2.3](https://api.stackexchange.com/docs), [compression requirements](https://api.stackexchange.com/docs/compression), and [throttles](https://api.stackexchange.com/docs/throttle)
- [Hacker News Firebase API](https://github.com/HackerNews/API)
- [4chan API](https://github.com/4chan/4chan-API) and [endpoint rules](https://github.com/4chan/4chan-API/blob/master/pages/Endpoints_and_domains.md)
- [Lemmy OpenAPI](https://join-lemmy.org/api/main)
- [Discourse API](https://docs.discourse.org/)
- [Forem API v1](https://developers.forem.com/api/v1)
- [Bluesky HTTP API](https://docs.bsky.app/docs/api/app-bsky-feed-get-feed)
- [Reddit Data API Wiki](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki) and [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy)
- [Telegram channel previews](https://telegram.org/tour/channels)
- [YouTube channel Atom feed](https://developers.google.com/youtube/data/guides/push_notifications)
- [V2EX API 2.0](https://edge.v2ex.com/help/api) and [legacy profile API](https://www.v2ex.com/p/7v9tec53)
- [Bogleheads Atom feed announcement](https://www.bogleheads.org/forum/viewtopic.php?p=3123123)
- [Steam Web API](https://steamcommunity.com/dev) and [Steam Subscriber Agreement](https://store.steampowered.com/subscriber_agreement/)

## Contributing

Keep adapters read-only and use documented public endpoints. Add deterministic fixtures for each implemented operation and update the capability catalog and matrix when behavior changes. Do not add credentials, session cookies, scraping that bypasses access controls, or an AI research/credibility layer.

## License and upstream attribution

This project is distributed under the MIT License. It was informed by research into public APIs and by review of the MIT-licensed [Agent-Reach](https://github.com/Panniantong/Agent-Reach) project; no Agent-Reach source code is copied into this repository.
