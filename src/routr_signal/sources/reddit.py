"""Reddit source â€” RSS first, Android-OAuth fallback when RSS gets IP-banned.

Three-stage fetch:
    1. Try anonymous RSS against old.reddit.com / www.reddit.com / reddit.com
       (the host fallback chain).
    2. If every host returns 403/429/timeout, switch to the Android-OAuth backend:
       hit https://www.reddit.com/api/v1/access_token with the public Android
       installed-client ID `ohXpoqrZYub1kg`, then call oauth.reddit.com/r/<sub>/new.json
       with the bearer token.
    3. If OAuth also fails, log once and return [].

The OAuth path bypasses Reddit's IP-throttling of unauthenticated browsers because
the OAuth endpoint is treated as a 1P mobile app surface, not as a scraper.

Token caching:
    The OAuth token is good for 24h. We persist it at data/cache/reddit-oauth.json
    so cross-run reuses survive. We refresh ~30min before expiry.

The community fallback is documented in the redlib project at github.com/redlib-org/redlib;
the client_id we use is the same one that project uses.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

import feedparser
import httpx

from ..lib import http
from ..lib.config import subreddits
from ..lib.dedupe import SeenStore
from ..lib.filters import prefilter
from ..lib.logging import debug, info, warn
from ..lib.paths import data_dir
from ..lib.types import RawItem


SOURCE = "reddit"

# Public Android installed-client id used by redlib and similar third-party clients.
# This is not a secret; it identifies the Reddit official Android app.
REDDIT_ANDROID_OAUTH_CLIENT_ID = "ohXpoqrZYub1kg"
OAUTH_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_API_BASE = "https://oauth.reddit.com"
# Realistic Android UA. Reddit's anti-bot is more lenient when it looks like the app.
ANDROID_USER_AGENT = "Reddit/Version 2024.04.0/Build 1645213/Android 13"

# Token refresh margin in seconds.
TOKEN_REFRESH_MARGIN = 30 * 60  # 30 minutes


def _configure_throttle() -> tuple[str, list[str]]:
    cfg = subreddits().get("fetch", {})
    ua = cfg.get(
        "user_agent",
        "linux:routr-signal-engine:v0.1 (by /u/routr-signals)",
    )
    min_gap = float(cfg.get("min_seconds_between_requests", 3.0))
    fallback_hosts: list[str] = list(
        cfg.get("host_fallback", ["old.reddit.com", "www.reddit.com", "reddit.com"])
    )
    # Reddit treats all subdomains under the same upstream rate budget.
    for host in fallback_hosts:
        http.set_budget(host, min_gap)
    return ua, fallback_hosts


def _swap_host(url: str, new_host: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(netloc=new_host))


def _fetch_feed(url: str, ua: str, fallback_hosts: list[str]) -> list[dict[str, Any]]:
    headers = {
        "User-Agent": ua,
        "Accept": "application/rss+xml, application/atom+xml, text/xml;q=0.9, */*;q=0.5",
    }
    last_status: str | None = None
    for host in fallback_hosts:
        candidate = _swap_host(url, host)
        try:
            resp = http.get(
                candidate,
                headers=headers,
                timeout=20.0,
                max_retries=0,  # we'll handle host fallback ourselves
                backoff_seconds=10,
            )
        except httpx.HTTPStatusError as e:
            last_status = f"{e.response.status_code} on {host}"
            debug(f"reddit: {candidate} â†’ {last_status}")
            continue
        except Exception as e:  # noqa: BLE001
            last_status = f"{type(e).__name__}: {e}"
            debug(f"reddit: {candidate} â†’ {last_status}")
            continue

        parsed = feedparser.parse(resp.text)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            debug(f"reddit: bozo feed at {candidate}: {getattr(parsed, 'bozo_exception', '')}")
            last_status = "bozo feed"
            continue
        return parsed.entries or []

    warn(
        f"reddit: all hosts failed for {url} (last: {last_status}). "
        "If this is persistent, see README troubleshooting â†’ 'Reddit 403'."
    )
    return []


# -----------------------------------------------------------------------------
# Android-OAuth fallback
# -----------------------------------------------------------------------------


def _oauth_cache_path():
    d = data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / "reddit-oauth.json"


def _load_cached_token() -> dict[str, Any] | None:
    path = _oauth_cache_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    expires_at = float(data.get("expires_at", 0))
    if expires_at - time.time() < TOKEN_REFRESH_MARGIN:
        return None  # too close to expiry; refresh
    return data


def _save_cached_token(token: str, expires_in: int, device_id: str) -> None:
    payload = {
        "access_token": token,
        "expires_at": time.time() + max(60, int(expires_in)),
        "device_id": device_id,
    }
    try:
        _oauth_cache_path().write_text(json.dumps(payload), encoding="utf-8")
    except OSError as e:
        warn(f"reddit: could not persist oauth token cache: {e}")


def _fetch_oauth_token() -> str | None:
    """Acquire an app-only OAuth token via the installed-client grant. Returns None on failure."""

    cached = _load_cached_token()
    if cached:
        return cached["access_token"]

    # Stable per-install device id (re-used across runs once generated).
    device_id_path = data_dir() / "cache" / "reddit-device-id.txt"
    if device_id_path.exists():
        device_id = device_id_path.read_text(encoding="utf-8").strip()
    else:
        # 24-char URL-safe random id; meets Reddit's 20-30 char requirement.
        device_id = secrets.token_urlsafe(18)[:24]
        device_id_path.write_text(device_id, encoding="utf-8")

    basic_auth = base64.b64encode(f"{REDDIT_ANDROID_OAUTH_CLIENT_ID}:".encode("ascii")).decode("ascii")
    headers = {
        "Authorization": f"Basic {basic_auth}",
        "User-Agent": ANDROID_USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    body = {
        "grant_type": "https://oauth.reddit.com/grants/installed_client",
        "device_id": device_id,
    }
    try:
        resp = httpx.post(OAUTH_TOKEN_URL, headers=headers, data=body, timeout=20.0)
    except httpx.HTTPError as e:
        warn(f"reddit: oauth token request failed: {e}")
        return None

    if resp.status_code != 200:
        warn(f"reddit: oauth token endpoint returned {resp.status_code}: {resp.text[:200]!r}")
        return None

    try:
        payload = resp.json()
    except ValueError:
        warn("reddit: oauth response was not JSON")
        return None

    token = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 86400))
    if not token:
        warn(f"reddit: oauth response missing access_token: {payload}")
        return None

    _save_cached_token(token, expires_in, device_id)
    info(f"reddit: acquired Android-OAuth token (expires in {expires_in}s)")
    return token


def _fetch_oauth_listing(token: str, subreddit: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Fetch r/<subreddit>/new via oauth.reddit.com. Returns raw `data.children` list."""

    url = f"{OAUTH_API_BASE}/r/{subreddit}/new"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": ANDROID_USER_AGENT,
    }
    params = {"limit": min(100, max(1, limit)), "raw_json": 1}
    try:
        resp = httpx.get(url, headers=headers, params=params, timeout=20.0)
    except httpx.HTTPError as e:
        warn(f"reddit: oauth listing for r/{subreddit} failed: {e}")
        return []

    if resp.status_code == 401:
        # Token likely expired; nuke the cache so the next run refreshes.
        try:
            _oauth_cache_path().unlink(missing_ok=True)
        except OSError:
            pass
        warn(f"reddit: oauth token rejected for r/{subreddit}; cleared cache.")
        return []

    if resp.status_code != 200:
        warn(f"reddit: oauth listing r/{subreddit} returned {resp.status_code}")
        return []

    try:
        payload = resp.json()
    except ValueError:
        return []

    children = (payload.get("data") or {}).get("children") or []
    return [c for c in children if isinstance(c, dict)]


def _oauth_child_to_item(child: dict[str, Any], subreddit: str) -> RawItem | None:
    data = child.get("data") or {}
    if not isinstance(data, dict):
        return None
    fullname = data.get("name") or f"t3_{data.get('id', '')}"
    if not fullname:
        return None

    item_id = f"{SOURCE}-{fullname.split('_', 1)[-1] if '_' in fullname else fullname}"
    title = data.get("title") or ""
    body = data.get("selftext") or ""
    permalink = data.get("permalink") or ""
    url = data.get("url") or (f"https://www.reddit.com{permalink}" if permalink else "")
    author = data.get("author")
    if author == "[deleted]":
        author = None
    created_utc = float(data.get("created_utc") or 0)
    created_at = (
        datetime.fromtimestamp(created_utc, tz=timezone.utc) if created_utc else datetime.now(timezone.utc)
    )
    score = data.get("score")
    num_comments = data.get("num_comments")

    return RawItem(
        id=item_id,
        source=SOURCE,
        title=title,
        body=body,
        url=url,
        author=author,
        created_at=created_at,
        extra={
            "subreddit": subreddit,
            "score": score,
            "num_comments": num_comments,
            "via": "oauth",
        },
    )


# -----------------------------------------------------------------------------
# RSS path (preferred)
# -----------------------------------------------------------------------------


def _entry_to_item(entry: dict[str, Any], subreddit: str) -> RawItem | None:
    link = entry.get("link") or entry.get("id")
    if not link:
        return None

    # Reddit RSS entries carry an `id` like `t3_xxxxxx` (post) or `t1_xxxxxx` (comment).
    raw_id = entry.get("id") or link
    short_id = raw_id.split("/")[-1] if "/" in raw_id else raw_id
    item_id = f"{SOURCE}-{short_id}"

    title = entry.get("title") or ""
    # `summary` is HTML; for our keyword prefilter we feed it as-is. Claude can read HTML fine.
    body = entry.get("summary") or ""

    author = entry.get("author") or entry.get("author_detail", {}).get("name")
    if author and author.startswith("/u/"):
        author = author[3:]
    elif author and author.startswith("u/"):
        author = author[2:]

    # Parse `updated` first (Reddit uses Atom-style); fall back to `published`.
    ts_struct = entry.get("updated_parsed") or entry.get("published_parsed")
    if ts_struct:
        # feedparser returns time.struct_time in UTC.
        created_at = datetime(*ts_struct[:6], tzinfo=timezone.utc)
    else:
        created_at = datetime.now(timezone.utc)

    return RawItem(
        id=item_id,
        source=SOURCE,
        title=title,
        body=body,
        url=link,
        author=author,
        created_at=created_at,
        extra={"subreddit": subreddit},
    )


def fetch() -> list[RawItem]:
    cfg = subreddits()
    ua, fallback_hosts = _configure_throttle()
    seen = SeenStore(SOURCE)

    collected: list[RawItem] = []
    rss_failed_subs: list[str] = []

    # Per-subreddit /new feeds via RSS first
    for sub in cfg.get("subreddits", []):
        name = sub.get("name", "")
        url = sub.get("new_rss")
        if not url:
            continue
        max_items = int(sub.get("max_items_per_run", 25))
        entries = _fetch_feed(url, ua, fallback_hosts)
        if not entries:
            rss_failed_subs.append(name)
            continue
        added = 0
        for e in entries[:max_items]:
            item = _entry_to_item(e, subreddit=name)
            if item is None:
                continue
            if seen.has(item.id):
                continue
            seen.add_item(item)
            collected.append(item)
            added += 1
        debug(f"reddit: r/{name} -> {added} new items via RSS")

    # If RSS failed on at least one subreddit, try OAuth for those.
    if rss_failed_subs:
        info(f"reddit: RSS failed for {rss_failed_subs}, falling back to Android-OAuth")
        token = _fetch_oauth_token()
        if token:
            for name in rss_failed_subs:
                max_items = 25
                for sub_cfg in cfg.get("subreddits", []):
                    if sub_cfg.get("name") == name:
                        max_items = int(sub_cfg.get("max_items_per_run", 25))
                        break
                children = _fetch_oauth_listing(token, name, limit=max_items)
                added = 0
                for child in children[:max_items]:
                    item = _oauth_child_to_item(child, subreddit=name)
                    if item is None:
                        continue
                    if seen.has(item.id):
                        continue
                    seen.add_item(item)
                    collected.append(item)
                    added += 1
                debug(f"reddit: r/{name} -> {added} new items via OAuth")

    # Search feeds (RSS only â€” OAuth search needs scope we don't have)
    for search in cfg.get("searches", []):
        url = search.get("url")
        if not url:
            continue
        max_items = int(search.get("max_items_per_run", 15))
        entries = _fetch_feed(url, ua, fallback_hosts)
        if not entries:
            continue
        added = 0
        for e in entries[:max_items]:
            item = _entry_to_item(e, subreddit=search.get("query", "search"))
            if item is None:
                continue
            if seen.has(item.id):
                continue
            seen.add_item(item)
            collected.append(item)
            added += 1
        debug(f"reddit: search {search.get('query')!r} -> {added} new items")

    filtered = prefilter(collected)
    info(f"reddit: {len(collected)} new items, {len(filtered)} pass keyword prefilter")

    seen.save()
    return filtered


if __name__ == "__main__":
    import json

    items = fetch()
    print(json.dumps([it.to_dict() for it in items], indent=2))
