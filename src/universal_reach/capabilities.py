"""Honest capability catalog for every requested platform."""

from __future__ import annotations

from universal_reach.models import PlatformCapability


_PLATFORMS = (
    PlatformCapability("reddit", "Reddit", "forum", "restricted", anonymous_access="no", auth_note="External Data API access is changing; do not use undocumented anonymous endpoints.", notes="No adapter until an authorized official access route is implemented.", documentation=("https://developers.reddit.com/app-registration", "https://developers.reddit.com/docs/guides/faq")),
    PlatformCapability("quora", "Quora", "forum", "planned", notes="Anonymous API capability has not been verified."),
    PlatformCapability("stackoverflow", "Stack Overflow", "forum", "planned", anonymous_access="yes", notes="Public Stack Exchange API; adapter implementation in progress.", documentation=("https://api.stackexchange.com/docs/advanced-search", "https://api.stackexchange.com/docs/throttle")),
    PlatformCapability("stackexchange", "Stack Exchange", "forum", "planned", anonymous_access="yes", notes="Shared API adapter with a caller-selected site; implementation in progress.", documentation=("https://api.stackexchange.com/docs",)),
    PlatformCapability("github", "GitHub", "forum", "planned", anonymous_access="yes", notes="Public REST reads and search; anonymous rate limits apply.", documentation=("https://docs.github.com/en/rest/repos/repos", "https://docs.github.com/en/rest/search/search", "https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api")),
    PlatformCapability("steam", "Steam Community", "forum", "planned", notes="A keyless public discussion API was not verified."),
    PlatformCapability("4chan", "4chan", "forum", "planned", anonymous_access="partial", notes="Public JSON supports board catalogs and threads; no global full-text search. Rate limits and attribution rules apply.", documentation=("https://github.com/4chan/4chan-API", "https://github.com/4chan/4chan-API/blob/master/pages/Endpoints_and_domains.md")),
    PlatformCapability("hackernews", "Hacker News", "forum", "planned", anonymous_access="partial", notes="Official Firebase API supports stories, items, comments, and users; it has no search endpoint.", documentation=("https://github.com/HackerNews/API",)),
    PlatformCapability("xda", "XDA Forums", "forum", "planned", notes="Public endpoint capability has not been verified."),
    PlatformCapability("v2ex", "V2EX", "forum", "restricted", anonymous_access="no", auth_note="The documented API 2.0 Beta uses a Bearer Personal Access Token.", documentation=("https://www.v2ex.com/help/api",)),
    PlatformCapability("zhihu", "Zhihu", "forum", "planned", notes="Anonymous public API capability has not been verified."),
    PlatformCapability("baidutieba", "Baidu Tieba", "forum", "planned", notes="Anonymous public API capability has not been verified."),
    PlatformCapability("resetera", "ResetEra", "forum", "planned", notes="Public endpoint capability has not been verified."),
    PlatformCapability("lemmy", "Lemmy", "forum", "planned", anonymous_access="partial", notes="Public API reads are available on instances that allow them; instance must be configured.", documentation=("https://join-lemmy.org/api/main", "https://join-lemmy.org/docs/contributors/04-api.html")),
    PlatformCapability("discourse", "Discourse forums", "forum", "planned", anonymous_access="partial", notes="Public JSON routes are instance-specific and subject to instance access settings.", documentation=("https://docs.discourse.org/",)),
    PlatformCapability("devto", "DEV Community", "forum", "planned", anonymous_access="partial", notes="Forem public keyword search and article reads are available; some endpoints require an API key.", documentation=("https://developers.forem.com/api/v1", "https://developers.forem.com/semantic-search")),
    PlatformCapability("indiehackers", "Indie Hackers", "forum", "planned", notes="Anonymous public API capability has not been verified."),
    PlatformCapability("bogleheads", "Bogleheads", "forum", "planned", notes="Anonymous public API capability has not been verified."),
    PlatformCapability("facebook", "Facebook", "social", "restricted", anonymous_access="no", auth_note="Public Page reads require an access token; broader public-content access requires Meta approval.", documentation=("https://developers.facebook.com/docs/graph-api/reference/page/feed/",)),
    PlatformCapability("instagram", "Instagram", "social", "restricted", anonymous_access="no", auth_note="Official API is intended for authorized professional accounts; no general anonymous public feed API verified."),
    PlatformCapability("youtube", "YouTube", "social", "restricted", anonymous_access="partial", auth_note="Public Data API reads require a Google API key; no keyless search adapter is planned.", documentation=("https://developers.google.com/youtube/data/guides/authentication", "https://developers.google.com/youtube/data/reference/rest/v3/search/list")),
    PlatformCapability("whatsapp", "WhatsApp", "social", "planned", notes="No documented public channel-search/read API verified."),
    PlatformCapability("tiktok", "TikTok", "social", "restricted", anonymous_access="no", auth_note="Research API requires approved research clients; Display API requires authorized users.", documentation=("https://developers.tiktok.com/docs/en/research-api-get-started", "https://developers.tiktok.com/docs/en/display-api-overview")),
    PlatformCapability("wechat", "WeChat", "social", "planned", notes="Anonymous public API capability has not been verified."),
    PlatformCapability("telegram", "Telegram", "social", "restricted", anonymous_access="no", auth_note="User MTProto requires login; bots can only read where added and authorized.", documentation=("https://core.telegram.org/api/auth", "https://core.telegram.org/bots/faq")),
    PlatformCapability("snapchat", "Snapchat", "social", "restricted", anonymous_access="no", auth_note="Public Profile API requires an allowlisted app and bearer token.", documentation=("https://www.developers.snap.com/marketing-api/Public-Profile-API/GetStarted",)),
    PlatformCapability("douyin", "Douyin", "social", "restricted", anonymous_access="no", auth_note="Official access uses OAuth authorization and app credentials.", documentation=("https://open.douyin.com/platform/resource/docs/develop/permission/overall-permission",)),
    PlatformCapability("kuaishou", "Kuaishou", "social", "restricted", anonymous_access="no", auth_note="Open Platform calls require app credentials and access tokens.", documentation=("https://open.kuaishou.com/docs/develop/server/getAccessToken.html",)),
    PlatformCapability("weibo", "Weibo", "social", "planned", notes="Official public endpoint details were not verifiable in this pass."),
    PlatformCapability("pinterest", "Pinterest", "social", "restricted", anonymous_access="no", auth_note="Pinterest API requires an authorized OAuth user.", documentation=("https://developers.pinterest.com/docs/getting-started/set-up-authentication-and-authorization/",)),
    PlatformCapability("x", "X", "social", "restricted", anonymous_access="no", auth_note="Recent search requires an approved developer app and bearer token.", documentation=("https://docs.x.com/x-api/posts/search/quickstart/recent-search",)),
    PlatformCapability("threads", "Threads", "social", "restricted", anonymous_access="no", auth_note="Official profile and post access requires Meta app authorization and scopes.", documentation=("https://developers.facebook.com/documentation/threads/threads-profiles",)),
    PlatformCapability("linkedin", "LinkedIn", "social", "restricted", anonymous_access="no", auth_note="LinkedIn API access requires OAuth authorization and program access.", documentation=("https://learn.microsoft.com/en-us/linkedin/shared/authentication/authentication",)),
    PlatformCapability("discord", "Discord", "social", "restricted", anonymous_access="no", auth_note="Bot credentials and server/channel permissions are required; self-bots are prohibited.", documentation=("https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots",)),
    PlatformCapability("bluesky", "Bluesky", "social", "planned", anonymous_access="yes", notes="Most public app.bsky GETs are available without authentication on the public AppView.", documentation=("https://docs.bsky.app/docs/api/app-bsky-feed-get-feed",)),
)

PLATFORM_CATALOG: dict[str, PlatformCapability] = {platform.id: platform for platform in _PLATFORMS}

if len(PLATFORM_CATALOG) != 35:
    raise RuntimeError(f"Expected 35 unique platform entries, found {len(PLATFORM_CATALOG)}")


def get_capability(platform: str) -> PlatformCapability:
    try:
        return PLATFORM_CATALOG[platform.casefold()]
    except KeyError as exc:
        from universal_reach.errors import ReachError

        raise ReachError("unknown_platform", f"Unknown platform: {platform}.", details={"platform": platform}) from exc


def list_capabilities() -> list[PlatformCapability]:
    return list(_PLATFORMS)
