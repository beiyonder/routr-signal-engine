"""Hacker News source — Algolia search_by_date.

API reference: https://hn.algolia.com/api

We issue one request per configured tag-query and one per keyword-query. Each request returns
up to 50 hits sorted by date, bounded by created_at_i > now - lookback_seconds. All hits are
unioned, deduped by objectID, filtered through `prefilter`, and returned as RawItems.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..lib import http
from ..lib.config import hn_config
from ..lib.dedupe import SeenStore
from ..lib.filters import prefilter
from ..lib.logging import debug, info, warn
from ..lib.types import RawItem


ALGOLIA_BASE = "https://hn.algolia.com/api/v1/search_by_date"
SOURCE = "hn"


def _hit_to_item(hit: dict[str, Any]) -> RawItem | None:
    """Convert one Algolia hit to a RawItem. Returns None if unparseable."""

    object_id = hit.get("objectID")
    if not object_id:
        return None

    tags: list[str] = hit.get("_tags") or []
    is_comment = "comment" in tags

    if is_comment:
        title = (hit.get("story_title") or "")[:200]
        body = hit.get("comment_text") or ""
        url = f"https://news.ycombinator.com/item?id={object_id}"
    else:
        title = hit.get("title") or hit.get("story_title") or ""
        body = hit.get("story_text") or ""
        # Prefer external URL when present; otherwise link to HN item.
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}"

    ts_int = hit.get("created_at_i")
    if not ts_int:
        return None
    created_at = datetime.fromtimestamp(int(ts_int), tz=timezone.utc)

    author = hit.get("author")

    return RawItem(
        id=f"{SOURCE}-{object_id}",
        source=SOURCE,
        title=title,
        body=body,
        url=url,
        author=author,
        created_at=created_at,
        extra={
            "tags": tags,
            "points": hit.get("points"),
            "num_comments": hit.get("num_comments"),
            "is_comment": is_comment,
        },
    )


def _search(
    query: str,
    tags: str,
    hits_per_page: int,
    numeric_filter: str,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "hitsPerPage": hits_per_page,
        "numericFilters": numeric_filter,
    }
    if query:
        params["query"] = query
    if tags:
        params["tags"] = tags

    try:
        resp = http.get(ALGOLIA_BASE, params=params, timeout=20.0, max_retries=2)
        payload = resp.json()
        return payload.get("hits", []) or []
    except Exception as e:  # noqa: BLE001 — keep pipeline robust
        warn(f"hn: Algolia request failed for tags={tags!r} query={query!r}: {e}")
        return []


def fetch() -> list[RawItem]:
    """Fetch fresh HN items across configured tag and keyword queries."""

    cfg = hn_config()
    lookback_hours = int(cfg.get("lookback_hours", 30))
    since_ts = int(time.time()) - lookback_hours * 3600
    numeric_filter = f"created_at_i>{since_ts}"

    seen = SeenStore(SOURCE)
    raw_hits: dict[str, dict[str, Any]] = {}

    # Tag-based queries (e.g., show_hn + keyword OR).
    for tq in cfg.get("tag_queries", []):
        tag = tq.get("tag", "")
        keywords = tq.get("keywords", []) or [""]
        max_items = int(tq.get("max_items", 30))

        # We issue one Algolia call per keyword to maximize recall — Algolia OR'd parens
        # in the `query` parameter sometimes don't behave; per-keyword is simpler.
        for kw in keywords:
            hits = _search(query=kw, tags=tag, hits_per_page=max_items, numeric_filter=numeric_filter)
            for h in hits:
                oid = h.get("objectID")
                if oid:
                    raw_hits[oid] = h

    # Free-text keyword queries (across stories AND comments).
    for kq in cfg.get("keyword_queries", []):
        query = kq.get("query", "")
        max_items = int(kq.get("max_items", 20))
        if not query:
            continue
        hits = _search(query=query, tags="", hits_per_page=max_items, numeric_filter=numeric_filter)
        for h in hits:
            oid = h.get("objectID")
            if oid:
                raw_hits[oid] = h

    info(f"hn: fetched {len(raw_hits)} unique hits across all queries")

    items: list[RawItem] = []
    for hit in raw_hits.values():
        item = _hit_to_item(hit)
        if item is None:
            continue
        if seen.has(item.id):
            continue
        items.append(item)
        seen.add_item(item)

    filtered = prefilter(items)
    debug(f"hn: {len(items)} new items, {len(filtered)} pass keyword prefilter")

    seen.save()
    return filtered


if __name__ == "__main__":
    # Manual run: `python -m routr_signal.sources.hn`
    import json

    out = fetch()
    print(json.dumps([it.to_dict() for it in out], indent=2))
