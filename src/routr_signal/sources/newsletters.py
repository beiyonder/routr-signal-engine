"""Curated newsletter RSS source.

Reads a list of feed URLs from `config/newsletters.yaml` and pulls fresh items
via `feedparser`. Each item is identified by `newsletter-<sha1(url)[:12]>` so
re-runs over the same feed item are idempotent.

Why this source:
    Newsletter writers and technical bloggers curate signal for us before we
    even see it. High SNR.
    The cosine prefilter then ranks how on-topic each issue is against our
    LLMOps anchors.

Notes:
    - Feedparser is already a dep (used by sources/reddit.py).
    - We respect a single global throttle for all feed hosts. They're
      distinct hosts so the global throttle is a soft floor.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from ..lib import http
from ..lib.config import newsletters as newsletters_config
from ..lib.dedupe import SeenStore
from ..lib.filters import prefilter
from ..lib.logging import debug, info, warn
from ..lib.types import RawItem


SOURCE = "newsletter"
DEFAULT_UA = "routr-signal-engine/0.1 (+https://github.com/beiyonder/routr-signal-engine)"
HTML_TAG_RE = re.compile(r"<[^>]+>")


def fetch() -> list[RawItem]:
    cfg = newsletters_config()
    if not cfg:
        debug("newsletters: no config (config/newsletters.yaml absent), skipping")
        return []

    feeds = cfg.get("feeds") or []
    if not isinstance(feeds, list) or not feeds:
        debug("newsletters: no feeds configured")
        return []

    ua = cfg.get("user_agent") or DEFAULT_UA
    throttle = float(cfg.get("min_seconds_between_requests", 2.0))
    headers = {
        "User-Agent": ua,
        "Accept": "application/rss+xml, application/atom+xml, text/xml;q=0.9, */*;q=0.5",
    }

    seen = SeenStore(SOURCE)
    collected: list[RawItem] = []

    for feed_cfg in feeds:
        if not isinstance(feed_cfg, dict):
            continue
        url = feed_cfg.get("url")
        name = feed_cfg.get("name") or url
        if not url:
            continue
        max_items = int(feed_cfg.get("max_items_per_run", 10))
        # Set per-host throttle as a floor.
        try:
            host = httpx.URL(url).host
            if host:
                http.set_budget(host, throttle)
        except Exception:  # noqa: BLE001
            pass

        entries = _fetch_feed(url, headers)
        if not entries:
            debug(f"newsletters: {name} returned no entries")
            continue

        added = 0
        for entry in entries[:max_items]:
            item = _entry_to_item(entry, feed_name=name)
            if item is None:
                continue
            if seen.has(item.id):
                continue
            seen.add_item(item)
            collected.append(item)
            added += 1
        debug(f"newsletters: {name} -> {added} new items")

    filtered = prefilter(collected)
    info(f"newsletters: {len(collected)} new items, {len(filtered)} pass keyword prefilter")
    seen.save()
    return filtered


def _fetch_feed(url: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    try:
        resp = http.get(url, headers=headers, timeout=20.0, max_retries=2)
    except Exception as e:  # noqa: BLE001
        warn(f"newsletters: GET {url} failed: {e}")
        return []
    parsed = feedparser.parse(resp.text)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        warn(f"newsletters: bozo feed at {url}: {getattr(parsed, 'bozo_exception', '')}")
        return []
    return parsed.entries or []


def _entry_to_item(entry: dict[str, Any], *, feed_name: str) -> RawItem | None:
    link = entry.get("link") or entry.get("id")
    if not isinstance(link, str) or not link:
        return None

    # Normalize id: sha1 of the URL keeps it stable even if a feed changes its
    # internal entry-id scheme between renders.
    digest = hashlib.sha1(link.encode("utf-8")).hexdigest()[:12]
    item_id = f"{SOURCE}-{digest}"

    title = (entry.get("title") or "").strip()
    # `summary` or `description` is usually HTML; strip tags so the keyword
    # prefilter sees plain text. The classifier sees the cleaned version too;
    # that's intentional (HTML noise distracts Haiku).
    body_html = entry.get("summary") or entry.get("description") or ""
    if isinstance(body_html, list):
        body_html = body_html[0] if body_html else ""
    body = _strip_html(body_html)
    # feedparser parses `content` differently per format (Atom vs RSS). If
    # body is empty try `content[0].value`.
    if not body:
        content = entry.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            body = _strip_html(content[0].get("value", ""))

    author = entry.get("author") or entry.get("author_detail", {}).get("name")
    if isinstance(author, str):
        author = author.strip() or None

    ts_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if ts_struct:
        created_at = datetime(*ts_struct[:6], tzinfo=timezone.utc)
    else:
        created_at = datetime.now(timezone.utc)

    return RawItem(
        id=item_id,
        source=SOURCE,
        title=title,
        body=body[:4000],  # newsletters can be long; truncate before storage
        url=link,
        author=author,
        created_at=created_at,
        extra={"feed": feed_name},
    )


def _strip_html(html: str) -> str:
    if not isinstance(html, str):
        return ""
    # Very intentionally NOT a full HTML parser. Newsletter feeds have
    # well-formed content from established publishers; tag stripping is enough.
    text = HTML_TAG_RE.sub(" ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


if __name__ == "__main__":
    import json as _json

    out = fetch()
    print(_json.dumps([it.to_dict() for it in out], indent=2))
