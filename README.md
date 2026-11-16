# Universal Reach

[![Tests](https://github.com/2q9e/universal-reach/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/2q9e/universal-reach/actions/workflows/tests.yml)

Universal Reach is a local, read-only CLI and Model Context Protocol (MCP) server for retrieving public content from supported social platforms and online communities. It makes direct requests to documented public APIs; there is no Universal Reach cloud service.

The project catalogs all 35 requested platforms and implements adapters for 20 platform IDs; Discord can be activated separately with an explicit channel allowlist. Some adapters require approved, owner-authorized tokens. The catalog distinguishes working adapters from restricted and not-yet-implemented platforms. It does not claim that a listed platform is fully supported.

Anonymous access means a request works without a user account, API key or token, or browser session cookie. A public API endpoint or feed is valid for this path when it is accessible without those credentials and its published terms permit the use. Separately marked, opt-in authenticated adapters add authorized functionality where a platform does not offer an anonymous route.

## Architecture

The CLI and stdio MCP server share one `ReachService` and a capability registry. Platform-specific adapters live under `src/universal_reach/adapters/`; they normalize responses into common records and use the shared bounded HTTP client. The JSON config stores public instance URLs and logging preferences only. Optional OAuth, PAT, and bot credentials are read from environment variables when an operation needs them. There is no browser automation backend or Universal Reach cloud service.

## Features

- One Python package, CLI, and stdio MCP server.
- Structured, normalized records with explicit pagination, access mode, and partial-result metadata.
- Read-only anonymous adapters, plus opt-in Reddit OAuth, V2EX Personal Access Token, X app-only reads, owner-authorized Facebook/Instagram/Threads/TikTok reads, and an allowlisted Discord bot.
- Host allowlists, HTTPS-only requests, redirect checks, bounded timeouts and retries, input validation, and safe error messages.
- Configurable public Lemmy and Discourse instances, with DNS checks that reject private or non-global addresses.
- No browser cookies or account passwords are used. Platform tokens are read from environment variables and never stored in the local JSON configuration. Reddit and X require explicit approval or paid-read opt-in; Meta adapters are restricted to authorized owner accounts.

## Installation

Requires CPython 3.11 or newer. Python 3.10 reached end of life in October 2026 ([Python version status](https://devguide.python.org/versions/)). Universal Reach code is pure Python. CI covers Linux and macOS on x64 and ARM64, and Windows x64; Windows ARM64 is not currently in the CI matrix.

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
universal-reach search --platform hackernews --query "mcp server" --options '{"sort":"recent","tags":"story"}'
universal-reach search-multi --query "public transit" --platform bluesky --platform github
universal-reach read --url https://github.com/modelcontextprotocol/python-sdk
universal-reach read --url 'https://youtu.be/M7lc1UVf-VE'
universal-reach replies --url https://news.ycombinator.com/item?id=1 --platform hackernews
universal-reach profile --platform github --username octocat
universal-reach health-check --platform bluesky
universal-reach doctor
```

The service validates input and returns a structured error with a nonzero exit status when an operation is unsupported or upstream access fails. `status` only reports configured capability; `health-check` makes bounded live requests. `doctor` validates local configuration, checks whether the MCP SDK is installed, and performs live checks against implemented adapters.

Some adapters need platform-specific options:

```sh
universal-reach search --platform 4chan --query "example" --options '{"board":"g"}'
universal-reach search --platform lemmy --query "example" --options '{"instance_url":"https://lemmy.world"}'
universal-reach search --platform discourse --query "example" --options '{"instance_url":"https://meta.discourse.org"}'
universal-reach search --platform steam --query "patch" --options '{"app_id":"489830"}'
```

Hacker News search uses the anonymous Algolia HN Search API (`sort` is `relevance` or `recent`; optional `tags` and `numeric_filters` follow the API's documented filters). Its cursor is a zero-based page number, and requests share the service's published limit of 10,000 per IP per hour. Exact reads, replies, and profile data continue to use the official Firebase API.

YouTube exact reads use its public oEmbed endpoint and return only limited video metadata (title, author, and a validated thumbnail URL); the channel Atom feed returns recent uploads. Neither route provides full channel history, comments, captions, or search.

Owner-authorized Meta reads use environment variables and accept only the configured account:

```sh
universal-reach profile-posts --platform facebook --username "$META_FACEBOOK_PAGE_ID"
universal-reach profile-posts --platform instagram --username "$META_INSTAGRAM_USER_ID"
universal-reach profile-posts --platform threads --username me
universal-reach profile --platform tiktok --username 'APP_SPECIFIC_OPEN_ID'
universal-reach profile-posts --platform tiktok --username 'APP_SPECIFIC_OPEN_ID'
universal-reach read --url 'https://www.threads.com/@<handle>/post/<shortcode>' --options '{"thread_id":"<numeric-api-id>"}'
```

Threads URLs contain a shortcode, while the official read and replies routes require the numeric API ID. Supply that ID from an authorized Threads API result in `options.thread_id`.

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
| Quora | Restricted | — | — | — | — | — | No verified anonymous API; partner data feed requires pricing and use-case approval |
| Stack Overflow | Partial | ✓ | ✓ | ✓ | ✓ | ✓ | Yes; Stack Exchange API quota and paging rules apply |
| Stack Exchange | Partial | ✓ | ✓ | ✓ | ✓ | ✓ | Yes; select `options.site` |
| GitHub | Partial | ✓ | ✓ | ✓ | ✓ | ✓ | Yes; unauthenticated rate limits apply |
| Steam Community | Partial | ✓ | — | — | — | — | Partial; searches current per-game announcement RSS entries only; discussions are unsupported |
| 4chan | Partial | Limited† | ✓ | ✓ | — | — | Yes; public JSON API |
| Hacker News | Partial | ✓ | ✓ | ✓ | ✓ | ✓ | Yes; Algolia HN Search plus official Firebase API |
| XDA Forums | Restricted | — | — | — | — | — | Valnet Terms cover forums/RSS and prohibit automated crawling or content extraction |
| V2EX | Partial | — | ✓ | ✓ | ✓ | — | Public profile route is anonymous; topic and replies routes require V2EX_ACCESS_TOKEN |
| Zhihu | Restricted | — | — | — | — | — | Official search requires Access Secret and timestamp; response contract/quota not verified |
| Baidu Tieba | Restricted | — | — | — | — | — | Official API is partner/application-based; no complete anonymous retrieval contract |
| ResetEra | Restricted | — | — | — | — | — | Terms restrict automated retrieval; announced RSS is limited to a specific staff feed |
| Lemmy | Partial | ✓ | ✓ | ✓ | — | — | Yes, on public configured instances |
| Discourse forums | Partial | ✓ | ✓ | ✓ | — | — | Yes, on public configured instances |
| DEV Community | Partial | ✓ | ✓ | — | ✓ | ✓ | Yes for implemented public Forem routes |
| Indie Hackers | Restricted | — | — | — | — | — | Terms prohibit automated crawling/scraping; no official feed found |
| Bogleheads | Partial | — | ✓ | — | — | — | Anonymous exact-post reads only when the post is in the bounded topic Atom feed |
| Facebook | Restricted | — | — | — | — | ✓ | Configured Page token, `pages_read_engagement`, `pages_read_user_content`, and Page `MODERATE` task; approximately 600 ranked published posts/year |
| Instagram | Restricted | — | — | — | ✓ | ✓ | Configured professional account only; Graph API scopes and app access apply; at most 10,000 recent media items |
| YouTube | Partial | — | ✓ | — | — | ✓ | Anonymous oEmbed metadata and recent channel Atom feed; broader Data API access requires a Google API key |
| WhatsApp | Restricted | — | — | — | — | — | Channel updates are public to visitors, but no automated public API/feed was verified; Business API requires a bearer token |
| TikTok | Partial | — | — | — | ✓ | ✓ | OAuth token for the owner account; Display API scopes and app approval required |
| WeChat | Restricted | — | — | — | — | — | Official account API requires onboarding, IP allowlisting, app credentials, and access tokens |
| Telegram | Partial | — | ✓ | — | ✓ | ✓ | Anonymous public channel previews; hidden posts and groups are unsupported |
| Snapchat | Restricted | — | — | — | — | — | Public Profile API requires an allowlisted app and bearer token; Developer Terms prohibit search-engine/directory use |
| Douyin | Restricted | — | — | — | — | — | Search is experimental/approval-gated, requires app credentials, and has restrictive result-use terms |
| Kuaishou | Restricted | — | — | — | — | — | Documented video routes require OAuth and are account-scoped; no anonymous public search verified |
| Weibo | Restricted | — | — | — | — | — | Official CLI requires developer verification, OAuth, and trial/paid access; automated collection needs prior permission |
| Pinterest | Restricted | — | — | — | — | — | App review and OAuth required; reads are account-scoped and public search is not documented |
| X / Twitter | Partial | ✓ | ✓ | — | — | — | App-only Bearer token and explicit paid-read opt-in; recent search covers seven days |
| Threads | Restricted | — | ✓ | ✓ | ✓ | ✓ | OAuth token owner's profile/posts; replies require `threads_read_replies`; exact reads require numeric API ID |
| LinkedIn | Restricted | — | — | — | — | — | OAuth and vetted product access required; general public post search is unavailable |
| Discord | Restricted* | — | ✓ | ✓ | — | — | Bot token, channel allowlist, and permissions required; content may be suppressed without privileged intent |
| Bluesky | Partial | ✓ | ✓ | ✓ | ✓ | ✓ | Yes through public AppView GET endpoints |

`—` means not implemented, rather than a claim that the platform itself lacks the capability. `*` Discord’s adapter is registered only when `DISCORD_ALLOWED_CHANNEL_IDS` contains one or more channel snowflakes. `†` 4chan search is limited to matching preview text in the active catalog of one explicitly selected board; it is not global full-text search. Hacker News search uses the separate public Algolia HN Search index; exact reads, replies, and profiles use the official Firebase API. Search completeness and availability depend on the index and its published limits. Lemmy and Discourse need an instance URL and only return content visible to anonymous visitors. Facebook, Instagram, Threads, and TikTok use owner-authorized APIs and do not provide anonymous access in this release.

### Other operations

No adapter in this release implements media extraction, transcripts, or archives. The CLI and MCP server expose those operations so unsupported requests receive the same explicit error shape across clients.

## Security and privacy

- Requests use read-only HTTP methods to documented APIs, official feeds, or public previews. TikTok's `video.list` API uses POST for a read operation; no adapter uses POST to publish or modify content. Adapters do not bypass authentication or access controls; owner-authorized Meta and TikTok reads remain limited to configured account identities.
- Static API hosts are allowlisted; URL inputs require HTTPS and reject credentials and nonstandard ports. Redirects are revalidated.
- Lemmy and Discourse origins must be public HTTPS hosts. Resolution rejects localhost, private, link-local, and other non-global addresses, then pins the connection to an address that passed validation. These two adapters connect directly and ignore proxy environment settings so a proxy cannot resolve the hostname to a different address.
- The local MCP server listens on stdio; this package does not open an inbound network server.
- No browser cookies or account passwords are imported. The local JSON config has no credential fields; optional PAT/OAuth values are environment-only.
- Logs avoid request query strings and response bodies. Upstream public content can still contain personal data; callers should apply their own retention and display policies.

Optional credentials are environment-only:

- V2EX topic reads/replies use `V2EX_ACCESS_TOKEN`.
- Reddit requires prior approval; only then set `REDDIT_API_APPROVED=true` and provide `REDDIT_ACCESS_TOKEN`, `REDDIT_CLIENT_ID`, and `REDDIT_USERNAME`. Requests remain disabled when a required setting is absent.
- X requires `X_API_BEARER_TOKEN` and the explicit `X_API_ALLOW_PAID_READS=true` opt-in. Recent search and exact reads use prepaid credits, so review current pricing first.
- Facebook requires `META_FACEBOOK_PAGE_ID` and `META_FACEBOOK_PAGE_ACCESS_TOKEN`, plus the Page permissions described in its capability entry. `META_FACEBOOK_PAGE_USERNAME` is optional and only matches that same Page.
- Instagram requires `META_INSTAGRAM_USER_ID` and `META_INSTAGRAM_ACCESS_TOKEN` for a professional account. `META_INSTAGRAM_USERNAME` is an optional account-name match.
- Threads requires `META_THREADS_ACCESS_TOKEN`; profile and post reads use the token owner, and replies also require `threads_read_replies`. Optional `META_THREADS_USER_ID` and `META_THREADS_USERNAME` bind calls to the expected owner. Exact reads and replies need `options.thread_id`, the numeric API ID returned by the Threads API; a public shortcode URL alone is insufficient.
- TikTok's Display API requires `TIKTOK_ACCESS_TOKEN` with the `user.info.basic` and `video.list` scopes. Supply the app-specific `open_id` returned by TikTok as the `username` argument; tokens are rejected from tool options to keep them out of MCP/CLI arguments. Search, public profile discovery, and anonymous reads are not implemented.
- Discord requires `DISCORD_BOT_TOKEN` and a comma-separated `DISCORD_ALLOWED_CHANNEL_IDS` allowlist; only those channels are addressable.

OS credential-store integration is not included.

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

Tests use deterministic mocked responses and should not require platform accounts or make live requests. GitHub Actions tests Python 3.11–3.15 on Linux x64; Python 3.11 and 3.15 on Linux ARM64, macOS Intel and Apple silicon, and Windows x64.

## Known limitations

- Twenty of the 35 catalog entries have adapters registered by default; Discord can be registered separately with its explicit channel allowlist. These are partial implementations, not full coverage of every platform feature. Reddit remains dormant until its approval and OAuth settings are present. Meta and TikTok reads require authorized account tokens; Steam search covers only documented per-game announcement feeds, while discussion threads remain unsupported.
- Public endpoints may change, return partial data, enforce rate limits, or deny particular content. Errors preserve those limits instead of retrying indefinitely.
- Anonymous GitHub and Stack Exchange quotas are limited. Stack Exchange page depth is bounded; GitHub search exposes a bounded result window.
- 4chan calls are rate-limited in-process and search is board-catalog-only. Discourse and Lemmy depend on the selected instance.
- Windows ARM64 is not covered in CI because the current dependency install falls back to a `cryptography` source build, which requires OpenSSL and Rust on Windows ([installation guide](https://cryptography.io/en/latest/installation/#building-cryptography-on-windows)). Browser automation, authenticated browser sessions, media downloads, transcript extraction, and archive integrations are not included.

## API references

Adapter routes and access declarations are based on the platforms' published documentation:

- [GitHub REST API](https://docs.github.com/en/rest) and [rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
- [Stack Exchange API v2.3](https://api.stackexchange.com/docs), [compression requirements](https://api.stackexchange.com/docs/compression), and [throttles](https://api.stackexchange.com/docs/throttle)
- [Hacker News Firebase API](https://github.com/HackerNews/API)
- [Hacker News Search API](https://hn.algolia.com/api) and [service description](https://hn.algolia.com/about)
- [4chan API](https://github.com/4chan/4chan-API) and [endpoint rules](https://github.com/4chan/4chan-API/blob/master/pages/Endpoints_and_domains.md)
- [Lemmy OpenAPI](https://join-lemmy.org/api/main)
- [Discourse API](https://docs.discourse.org/)
- [Forem API v1](https://developers.forem.com/api/v1)
- [Bluesky HTTP API](https://docs.bsky.app/docs/api/app-bsky-feed-get-feed)
- [Reddit Data API Wiki](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki) and [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy)
- [Telegram channel previews](https://telegram.org/tour/channels)
- [YouTube channel Atom feed](https://developers.google.com/youtube/data/guides/push_notifications) and [YouTube oEmbed provider endpoint](https://oembed.org/providers/youtube)
- [V2EX API 2.0](https://edge.v2ex.com/help/api) and [legacy profile API](https://www.v2ex.com/p/7v9tec53)
- [Bogleheads Atom feed announcement](https://www.bogleheads.org/forum/viewtopic.php?p=3123123)
- [Steamworks Events and Announcements RSS guidance](https://partner.steamgames.com/doc/marketing/event_tools), [Steam Web API](https://steamcommunity.com/dev), [ISteamNews interface](https://partner.steamgames.com/doc/webapi/ISteamNews), and [Steam Subscriber Agreement](https://store.steampowered.com/subscriber_agreement/)
- [Zhihu API Platform](https://developer.zhihu.com/) and [Zhihu Agreement](https://www.zhihu.com/plainterms)
- [Quora public-data information](https://help.quora.com/hc/en-us/articles/15205064557844-Can-I-opt-out-of-having-my-Quora-content-used-to-train-LLMs) and [partner data program](https://aws.amazon.com/marketplace/pp/prodview-qkz4nbakwh7cs)
- [Pinterest API access tiers](https://developers.pinterest.com/docs/key-concepts/access-tiers/) and [developer guidelines](https://policy.pinterest.com/en-gb/developer-guidelines/)
- [X recent Post search](https://docs.x.com/x-api/posts/search-recent-posts), [Get Post by ID](https://docs.x.com/x-api/posts/get-post-by-id), and [pricing](https://docs.x.com/x-api/getting-started/pricing)
- [Discord API reference](https://docs.discord.com/developers/reference), [message routes](https://docs.discord.com/developers/resources/message), and [permissions](https://docs.discord.com/developers/topics/permissions)
- [Meta Page Published Posts](https://developers.facebook.com/docs/graph-api/reference/page/published_posts/), [Page permissions](https://developers.facebook.com/docs/graph-api/reference/page/), and [Graph API versioning](https://developers.facebook.com/docs/graph-api/guides/versioning)
- [Instagram Platform overview](https://developers.facebook.com/documentation/instagram-platform/overview) and [IG User Media](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media)
- [Meta Threads API collection](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api), which describes the token-owner and reply endpoints used by the adapter
- [TikTok Display API overview](https://developers.tiktok.com/docs/en/display-api-overview), [Get user info](https://developers.tiktok.com/docs/en/tiktok-api-v2-get-user-info), [List videos](https://developers.tiktok.com/docs/en/tiktok-api-v2-video-list), and [error handling](https://developers.tiktok.com/docs/en/tiktok-api-v2-error-handling)
- [Baidu Tieba Open API](https://tieba.baidu.com/tb/zt/tiebaapi/index.html) and [client agreement](https://tieba.baidu.com/tb/eula.html)
- [Valnet's XDA ownership page](https://www.valnetinc.com/en/xda) and [Terms of Use](https://www.valnetinc.com/en/terms-of-use), which cover associated forums and RSS feeds
- [WhatsApp Channel visibility](https://faq.whatsapp.com/699347952293912/?cms_platform=web&helpref=uf_share), [Business Platform](https://developers.facebook.com/documentation/business-messaging/whatsapp/overview), and [Terms](https://www.whatsapp.com/legal/terms-of-service?mainArticle=true)
- [Tencent Cloud WeChat Open Platform documentation](https://cloud.tencent.com/document/product/1301/100187)
- [Weibo CLI quickstart](https://open.weibo.com/cli/quickstart) and [service agreement](https://www.weibo.com/signup/v5/protocol/)
- [Snapchat Public Profile API](https://developers.snap.com/marketing-api/Public-Profile-API/GetStarted), [Creator Discovery](https://developers.snap.com/marketing-api/Public-Profile-API/CreatorDiscovery), and [Developer Terms](https://www.snap.com/terms/developer)
- [Douyin search capability](https://developer.open-douyin.com/docs/resource/zh-CN/dop/ability/search-management/item-search), [search API](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/douyin-search-capability/aweme-dy-video-search/), and [platform agreement](https://developer.open-douyin.com/docs/resource/zh-CN/developer/operation-norm/platform-protocol/register-protocol)
- [Kuaishou website app authorization](https://open.kuaishou.com/platformDocs/develop/web-app), [video API](https://open.kuaishou.com/platformDocs/openAbility/contentManagement/searchForVideos.html), and [application management rules](https://open.kuaishou.com/platformDocs/newGuide/applicationManagementSpecifica.html)
- [LinkedIn OAuth](https://learn.microsoft.com/en-us/linkedin/shared/authentication/authentication), [Posts API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api?tabs=curl&view=li-lms-2026-04), [Community Management access](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/community-management-overview?view=li-lms-2026-08), and [API Terms](https://www.linkedin.com/legal/l/api-terms-of-use)

## Contributing

Keep adapters read-only and use documented public endpoints. Add deterministic fixtures for each implemented operation and update the capability catalog and matrix when behavior changes. Do not add credentials, session cookies, scraping that bypasses access controls, or an AI research/credibility layer.

## License and upstream attribution

This project is distributed under the MIT License. It was informed by research into public APIs and review of the MIT-licensed [Agent-Reach](https://github.com/Panniantong/Agent-Reach) project. The [upstream design review](docs/upstream-review.md) records the fork-versus-new-implementation decision; no Agent-Reach source code is copied into this repository.
