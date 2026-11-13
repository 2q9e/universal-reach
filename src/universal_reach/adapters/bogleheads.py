"""Read individual public Bogleheads posts from the forum's Atom feed.

The forum documents anonymous Atom feeds for a whole forum or a single topic.
This adapter deliberately exposes only exact-post reads that appear in that
bounded topic feed; the feed is not a search API and does not guarantee a full
post body or arbitrary historical coverage.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from xml.etree import ElementTree

from universal_reach.adapters.base import Adapter, adapter_capability
from universal_reach.errors import ReachError
from universal_reach.http import get_text
from universal_reach.models import AccessInfo, AdapterStatus, Record
from universal_reach.security import validate_https_url


_FEED_URL = "https://www.bogleheads.org/forum/feed.php"
_ALLOWED_HOSTS = frozenset({"www.bogleheads.org", "bogleheads.org"})
_TOPIC_PATH = "/forum/viewtopic.php"
_POSITIVE_ID = re.compile(r"^[1-9][0-9]{0,15}$")
_POST_FRAGMENT = re.compile(r"^p([1-9][0-9]{0,15})$")
_BLOCK_TAGS = frozenset({"br", "div", "li", "p", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"})


class _PlainHTML(HTMLParser):
    """Convert feed HTML excerpts to readable text without rendering markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._hidden_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self._hidden_depth:
            self._hidden_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self.parts.append(data)


def _plain_text(value: str | None) -> str | None:
    if not value:
        return None
    parser = _PlainHTML()
    parser.feed(value)
    text = "".join(parser.parts)
    text = re.sub(r"[\t\r\f\v ]+", " ", text)
    text = re.sub(r" *\n+ *", "\n", text).strip()
    return text or None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":", 1)[-1]


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name:
            value = "".join(child.itertext()).strip()
            return value or None
    return None


def _entry_link(entry: ElementTree.Element) -> str | None:
    links = [node for node in entry if _local_name(node.tag) == "link"]
    for node in links:
        if node.attrib.get("rel", "alternate") == "alternate" and node.attrib.get("href"):
            return node.attrib["href"]
    for node in links:
        if node.attrib.get("href"):
            return node.attrib["href"]
    return None


def _atom_entries(document: str) -> list[dict[str, str | None]]:
    # External entity declarations are neither needed for Atom nor acceptable
    # for untrusted feed content. get_text also caps the response size.
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", document, re.IGNORECASE):
        raise ReachError("parser_error", "The Bogleheads feed contained a prohibited XML declaration.")
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise ReachError("parser_error", "The Bogleheads feed was not valid XML.") from exc
    if _local_name(root.tag) != "feed":
        raise ReachError("parser_error", "The Bogleheads endpoint did not return an Atom feed.")

    result: list[dict[str, str | None]] = []
    for entry in root:
        if _local_name(entry.tag) != "entry":
            continue
        author = None
        for child in entry:
            if _local_name(child.tag) == "author":
                author = _child_text(child, "name")
                if author:
                    break
            if _local_name(child.tag) == "creator":
                author = "".join(child.itertext()).strip() or None
        content = _child_text(entry, "content") or _child_text(entry, "summary")
        result.append({
            "id": _child_text(entry, "id"),
            "title": _plain_text(_child_text(entry, "title")),
            "link": _entry_link(entry),
            "author": _plain_text(author),
            "text": _plain_text(content),
            "published": _child_text(entry, "published") or _child_text(entry, "updated"),
        })
    return result


def _positive_parameter(query: dict[str, list[str]], name: str, *, required: bool = True) -> str | None:
    values = query.get(name, [])
    if not values and not required:
        return None
    if len(values) != 1 or not _POSITIVE_ID.fullmatch(values[0]):
        raise ReachError("invalid_url", f"The Bogleheads post URL needs one valid {name} parameter.")
    return values[0]


def _post_id_from_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() != "https" or (parts.hostname or "").lower().rstrip(".") not in _ALLOWED_HOSTS:
            return None
        if parts.path != _TOPIC_PATH:
            return None
        query = parse_qs(parts.query, keep_blank_values=True)
        candidate = query.get("p", [None])
        if len(candidate) == 1 and candidate[0] and _POSITIVE_ID.fullmatch(candidate[0]):
            return candidate[0]
        match = _POST_FRAGMENT.fullmatch(parts.fragment)
        return match.group(1) if match else None
    except ValueError:
        return None


def _created_at(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class BogleheadsAdapter(Adapter):
    """Read an exact post only when the public topic Atom feed contains it."""

    url_hosts = _ALLOWED_HOSTS

    def __init__(self) -> None:
        super().__init__(adapter_capability("bogleheads", ("read",)))

    @staticmethod
    def _parse_post_url(url: str) -> tuple[str, str, str]:
        validate_https_url(url, _ALLOWED_HOSTS)
        parts = urlsplit(url)
        if parts.path != _TOPIC_PATH:
            raise ReachError("invalid_url", "Expected a Bogleheads forum post URL.")
        query = parse_qs(parts.query, keep_blank_values=True)
        forum_id = _positive_parameter(query, "f")
        topic_id = _positive_parameter(query, "t")
        post_id = _positive_parameter(query, "p", required=False)
        if post_id is None:
            fragment_match = _POST_FRAGMENT.fullmatch(parts.fragment)
            post_id = fragment_match.group(1) if fragment_match else None
        if post_id is None:
            raise ReachError(
                "invalid_url",
                "Read requires an individual Bogleheads post URL with a p=ID parameter or #pID fragment.",
            )
        return forum_id or "", topic_id or "", post_id

    async def health_check(self, options: dict[str, Any] | None = None) -> AdapterStatus:
        del options
        try:
            document = await get_text(
                f"{_FEED_URL}?f=1",
                allowed_hosts=_ALLOWED_HOSTS,
                retries=0,
                max_bytes=500_000,
            )
            _atom_entries(document)
        except ReachError as exc:
            return AdapterStatus(
                platform=self.platform,
                status="degraded" if exc.code == "rate_limited" else "unavailable",
                operations=list(self.capability.operations),
                message=f"Bogleheads public Atom feed health check failed ({exc.code}).",
                access_mode="anonymous",
            )
        return AdapterStatus(
            platform=self.platform,
            status="available",
            operations=list(self.capability.operations),
            message="Bogleheads' public Atom feed responded.",
            access_mode="anonymous",
        )

    async def read(self, url: str, options: dict[str, Any] | None = None) -> Record:
        del options  # The official feed has no documented per-read options.
        forum_id, topic_id, post_id = self._parse_post_url(url)
        feed_url = f"{_FEED_URL}?{urlencode({'f': forum_id, 't': topic_id})}"
        document = await get_text(
            feed_url,
            allowed_hosts=_ALLOWED_HOSTS,
            retries=0,
            max_bytes=2_000_000,
        )
        entries = _atom_entries(document)
        matched = next(
            (
                entry for entry in entries
                if _post_id_from_url(entry.get("link")) == post_id
                or _post_id_from_url(entry.get("id")) == post_id
            ),
            None,
        )
        if matched is None:
            raise ReachError(
                "not_found",
                "The requested post is not present in the current public Bogleheads topic feed.",
                details={"scope": "current topic Atom feed", "post_id": post_id},
            )

        return Record(
            platform=self.platform,
            type="post",
            id=post_id,
            url=url,
            author=matched.get("author"),
            title=matched.get("title"),
            text=matched.get("text"),
            created_at=_created_at(matched.get("published")),
            access=AccessInfo(mode="anonymous", complete=False),
            extensions={
                "source_url": url,
                "feed_url": feed_url,
                "feed_entry_url": matched.get("link"),
                "feed_limited": True,
            },
        )
