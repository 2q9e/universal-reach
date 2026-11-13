"""Honest capability catalog for every requested platform."""

from __future__ import annotations

from universal_reach.models import PlatformCapability


_PLATFORMS = (
    PlatformCapability("reddit", "Reddit", "forum", "restricted", anonymous_access="no", auth_note="OAuth access is enabled only after Reddit explicitly approves the app and use case; configure the issued token and truthful app identity in environment variables.", notes="The adapter sends no request unless REDDIT_API_APPROVED=true and the approved OAuth configuration is present. No anonymous endpoint is used.", documentation=("https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki", "https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy", "https://www.reddit.com/dev/api/")),
    PlatformCapability("quora", "Quora", "forum", "planned", notes="Anonymous API capability has not been verified."),
    PlatformCapability("stackoverflow", "Stack Overflow", "forum", "planned", anonymous_access="yes", notes="Public Stack Exchange API; adapter implementation in progress.", documentation=("https://api.stackexchange.com/docs/advanced-search", "https://api.stackexchange.com/docs/throttle")),
    PlatformCapability("stackexchange", "Stack Exchange", "forum", "planned", anonymous_access="yes", notes="Shared API adapter with a caller-selected site; implementation in progress.", documentation=("https://api.stackexchange.com/docs",)),
    PlatformCapability("github", "GitHub", "forum", "planned", anonymous_access="yes", notes="Public REST reads and search; anonymous rate limits apply.", documentation=("https://docs.github.com/en/rest/repos/repos", "https://docs.github.com/en/rest/search/search", "https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api")),
    PlatformCapability("steam", "Steam Community", "forum", "restricted", anonymous_access="no", notes="No public discussions route is provided by the documented Steam Web API; the Subscriber Agreement restricts automated interaction with Steam services, so no HTML reader is included.", documentation=("https://steamcommunity.com/dev", "https://store.steampowered.com/subscriber_agreement/")),
    PlatformCapability("4chan", "4chan", "forum", "planned", anonymous_access="partial", notes="Public JSON supports board catalogs and threads; no global full-text search. Rate limits and attribution rules apply.", documentation=("https://github.com/4chan/4chan-API", "https://github.com/4chan/4chan-API/blob/master/pages/Endpoints_and_domains.md")),
    PlatformCapability("hackernews", "Hacker News", "forum", "planned", anonymous_access="partial", notes="Official Firebase API supports stories, items, comments, and users; it has no search endpoint.", documentation=("https://github.com/HackerNews/API",)),
    PlatformCapability("xda", "XDA Forums", "forum", "planned", notes="Public endpoint capability has not been verified."),
    PlatformCapability("v2ex", "V2EX", "forum", "restricted", anonymous_access="partial", auth_note="The API 2.0 topic and reply routes use a Bearer Personal Access Token; the documented legacy member-profile route is anonymous.", notes="Public profile reads need no token. Topic reads and replies use the optional V2EX_ACCESS_TOKEN environment variable. The API documents no keyword-search route.", documentation=("https://edge.v2ex.com/help/api", "https://www.v2ex.com/p/7v9tec53")),
    PlatformCapability("zhihu", "Zhihu", "forum", "planned", notes="Anonymous public API capability has not been verified."),
    PlatformCapability("baidutieba", "Baidu Tieba", "forum", "planned", notes="Anonymous public API capability has not been verified."),
    PlatformCapability("resetera", "ResetEra", "forum", "planned", notes="Public endpoint capability has not been verified."),
    PlatformCapability("lemmy", "Lemmy", "forum", "planned", anonymous_access="partial", notes="Public API reads are available on instances that allow them; instance must be configured.", documentation=("https://join-lemmy.org/api/main", "https://join-lemmy.org/docs/contributors/04-api.html")),
    PlatformCapability("discourse", "Discourse forums", "forum", "planned", anonymous_access="partial", notes="Public JSON routes are instance-specific and subject to instance access settings.", documentation=("https://docs.discourse.org/",)),
    PlatformCapability("devto", "DEV Community", "forum", "planned", anonymous_access="partial", notes="Forem public keyword search and article reads are available; some endpoints require an API key.", documentation=("https://developers.forem.com/api/v1", "https://developers.forem.com/semantic-search")),
    PlatformCapability("indiehackers", "Indie Hackers", "forum", "planned", notes="Anonymous public API capability has not been verified."),
    PlatformCapability("bogleheads", "Bogleheads", "forum", "planned", anonymous_access="yes", notes="The adapter reads an individual post only when it appears in the official bounded topic Atom feed; search, replies, and arbitrary history are unsupported.", documentation=("https://www.bogleheads.org/forum/feed.php", "https://www.bogleheads.org/forum/viewtopic.php?p=3123123")),
    PlatformCapability("facebook", "Facebook", "social", "restricted", anonymous_access="no", auth_note="Public Page reads require an authorized Page token and permissions. Tokenless oEmbed is limited to front-end embedding, not agent-facing retrieval.", notes="The Facebook oEmbed reference documents embed metadata and access limits, not a general post-reading API.", documentation=("https://developers.facebook.com/docs/graph-api/reference/page/feed/", "https://developers.facebook.com/docs/graph-api/reference/oembed-post", "https://developers.facebook.com/docs/graph-api/reference/oembed-video")),
    PlatformCapability("instagram", "Instagram", "social", "restricted", anonymous_access="no", auth_note="The public Display API is account- and permission-scoped; tokenless oEmbed is limited to front-end embedding and forbids extracting metadata for other purposes.", documentation=("https://developers.facebook.com/documentation/instagram-platform/oembed",)),
    PlatformCapability("youtube", "YouTube", "social", "restricted", anonymous_access="partial", auth_note="The adapter uses the documented public channel Atom feed for recent uploads; broader YouTube Data API reads, including search, require a Google API key.", notes="profile_posts needs a channel ID and returns only the bounded recent feed, not channel history.", documentation=("https://developers.google.com/youtube/data/guides/push_notifications", "https://developers.google.com/youtube/data/guides/authentication", "https://developers.google.com/youtube/data/reference/rest/v3/search/list")),
    PlatformCapability("whatsapp", "WhatsApp", "social", "planned", notes="No documented public channel-search/read API verified."),
    PlatformCapability("tiktok", "TikTok", "social", "restricted", anonymous_access="no", auth_note="Research API access requires approval; Display API reads an authorizing user’s content. TikTok Terms prohibit automated extraction without written approval.", notes="The documented oEmbed endpoint is for embedding content and does not provide a general agent-facing retrieval route.", documentation=("https://developers.tiktok.com/docs/en/research-api-get-started", "https://developers.tiktok.com/docs/en/display-api-overview", "https://developers.tiktok.com/docs/en/embed-videos", "https://t.tiktok.com/legal/page/us/terms-of-service/en")),
    PlatformCapability("wechat", "WeChat", "social", "planned", notes="Anonymous public API capability has not been verified."),
    PlatformCapability("telegram", "Telegram", "social", "restricted", anonymous_access="partial", auth_note="Anonymous reads cover only public channel content exposed in Telegram's t.me web preview; groups, private channels, and preview-hidden messages are unsupported.", notes="The before query used for preview pagination is not documented as a stable API contract.", documentation=("https://telegram.org/tour/channels", "https://telegram.org/blog/privacy-discussions-web-bots")),
    PlatformCapability("snapchat", "Snapchat", "social", "restricted", anonymous_access="no", auth_note="Public Profile API requires an allowlisted app and bearer token.", documentation=("https://www.developers.snap.com/marketing-api/Public-Profile-API/GetStarted",)),
    PlatformCapability("douyin", "Douyin", "social", "restricted", anonymous_access="no", auth_note="Official access uses OAuth authorization and app credentials.", documentation=("https://open.douyin.com/platform/resource/docs/develop/permission/overall-permission",)),
    PlatformCapability("kuaishou", "Kuaishou", "social", "restricted", anonymous_access="no", auth_note="Open Platform calls require app credentials and access tokens.", documentation=("https://open.kuaishou.com/docs/develop/server/getAccessToken.html",)),
    PlatformCapability("weibo", "Weibo", "social", "planned", notes="Official public endpoint details were not verifiable in this pass."),
    PlatformCapability("pinterest", "Pinterest", "social", "restricted", anonymous_access="no", auth_note="Pinterest API requires an authorized OAuth user.", documentation=("https://developers.pinterest.com/docs/getting-started/set-up-authentication-and-authorization/",)),
    PlatformCapability("x", "X", "social", "restricted", anonymous_access="no", auth_note="Recent search requires an approved developer app and bearer token.", documentation=("https://docs.x.com/x-api/posts/search/quickstart/recent-search",)),
    PlatformCapability("threads", "Threads", "social", "restricted", anonymous_access="no", auth_note="The authorized Threads API is scope-based; tokenless oEmbed is for front-end embedding and prohibits extracting or persisting post metadata for other uses.", documentation=("https://developers.facebook.com/documentation/threads/threads-profiles", "https://developers.facebook.com/docs/threads/tools-and-resources/embed-a-threads-post", "https://developers.facebook.com/docs/graph-api/reference/oembed-post")),
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
