"""Default adapter construction."""

import os

from universal_reach.adapters.bluesky import BlueskyAdapter
from universal_reach.adapters.bogleheads import BogleheadsAdapter
from universal_reach.adapters.discord import DiscordAdapter
from universal_reach.adapters.forem import ForemAdapter
from universal_reach.adapters.github import GitHubAdapter
from universal_reach.adapters.hackernews import HackerNewsAdapter
from universal_reach.adapters.meta_facebook import FacebookPageAdapter
from universal_reach.adapters.meta_instagram import InstagramProfessionalAdapter
from universal_reach.adapters.public_forums import DiscourseAdapter, FourChanAdapter, LemmyAdapter
from universal_reach.adapters.reddit import RedditAdapter
from universal_reach.adapters.telegram import TelegramAdapter
from universal_reach.adapters.tiktok import TikTokAdapter
from universal_reach.adapters.threads import ThreadsAdapter
from universal_reach.adapters.youtube import YouTubeAdapter
from universal_reach.adapters.x import XAdapter
from universal_reach.adapters.stackexchange import StackExchangeAdapter
from universal_reach.adapters.v2ex import V2EXAdapter
from universal_reach.registry import AdapterRegistry


def build_registry() -> AdapterRegistry:
    adapters = [
        GitHubAdapter(),
        StackExchangeAdapter(platform="stackoverflow"),
        StackExchangeAdapter(platform="stackexchange"),
        HackerNewsAdapter(),
        BogleheadsAdapter(),
        RedditAdapter(),
        TelegramAdapter(),
        YouTubeAdapter(),
        V2EXAdapter(),
        XAdapter(),
        FourChanAdapter(),
        LemmyAdapter(),
        DiscourseAdapter(),
        ForemAdapter(),
        BlueskyAdapter(),
        FacebookPageAdapter(),
        InstagramProfessionalAdapter(),
        ThreadsAdapter(),
        TikTokAdapter(),
    ]
    allowed_channels = [
        channel_id.strip()
        for channel_id in os.environ.get("DISCORD_ALLOWED_CHANNEL_IDS", "").split(",")
        if channel_id.strip()
    ]
    if allowed_channels:
        adapters.append(DiscordAdapter(channel_ids=allowed_channels))
    return AdapterRegistry(adapters)
