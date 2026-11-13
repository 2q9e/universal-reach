"""Default adapter construction."""

from universal_reach.adapters.bluesky import BlueskyAdapter
from universal_reach.adapters.bogleheads import BogleheadsAdapter
from universal_reach.adapters.forem import ForemAdapter
from universal_reach.adapters.github import GitHubAdapter
from universal_reach.adapters.hackernews import HackerNewsAdapter
from universal_reach.adapters.public_forums import DiscourseAdapter, FourChanAdapter, LemmyAdapter
from universal_reach.adapters.reddit import RedditAdapter
from universal_reach.adapters.telegram import TelegramAdapter
from universal_reach.adapters.youtube import YouTubeAdapter
from universal_reach.adapters.stackexchange import StackExchangeAdapter
from universal_reach.adapters.v2ex import V2EXAdapter
from universal_reach.registry import AdapterRegistry


def build_registry() -> AdapterRegistry:
    return AdapterRegistry(
        [
            GitHubAdapter(),
            StackExchangeAdapter(platform="stackoverflow"),
            StackExchangeAdapter(platform="stackexchange"),
            HackerNewsAdapter(),
            BogleheadsAdapter(),
            RedditAdapter(),
            TelegramAdapter(),
            YouTubeAdapter(),
            V2EXAdapter(),
            FourChanAdapter(),
            LemmyAdapter(),
            DiscourseAdapter(),
            ForemAdapter(),
            BlueskyAdapter(),
        ]
    )
