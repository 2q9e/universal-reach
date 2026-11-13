"""Default adapter construction."""

from universal_reach.adapters.bluesky import BlueskyAdapter
from universal_reach.adapters.forem import ForemAdapter
from universal_reach.adapters.github import GitHubAdapter
from universal_reach.adapters.hackernews import HackerNewsAdapter
from universal_reach.adapters.public_forums import DiscourseAdapter, FourChanAdapter, LemmyAdapter
from universal_reach.adapters.stackexchange import StackExchangeAdapter
from universal_reach.registry import AdapterRegistry


def build_registry() -> AdapterRegistry:
    return AdapterRegistry(
        [
            GitHubAdapter(),
            StackExchangeAdapter(platform="stackoverflow"),
            StackExchangeAdapter(platform="stackexchange"),
            HackerNewsAdapter(),
            FourChanAdapter(),
            LemmyAdapter(),
            DiscourseAdapter(),
            ForemAdapter(),
            BlueskyAdapter(),
        ]
    )
