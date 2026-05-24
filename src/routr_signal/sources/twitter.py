"""X / Twitter source.

Strategy:
    Primary: Playwright headless Chromium loaded with cookies from a real
             browser session. Scrapes profile + search pages via the DOM.
    Disabled (for now): twikit (Python wrapper around X's internal GraphQL).

    twikit v2.3.3 has an unfixed regression -- `ClientTransaction` setup
    crashes before any auth happens (`KEY_BYTE indices` / `'key' attribute`
    errors). Tracking upstream; will re-enable when patched. Opt back in
    early with `ROUTR_SIGNAL_X_USE_TWIKIT=1`.

Cookies are persisted at `data/cache/twitter-cookies-playwright.json` (full
attribute set). On CI the cookies are bootstrapped from the env var
`TWITTER_COOKIES_B64` (base64 of the same file). Use
`tools/twitter_login.py --import path/to/cookies.json` to populate from a
Cookie-Editor export. NEVER use a personal account.

ToS posture: scraping public tweets is against X's TOS. We use a dedicated
burner. Stay polite (default 30s between reads), low volume (<100 items/day),
and the burner survives indefinitely in practice.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any

from ..lib import signal_store
from ..lib.config import twitter_watch
from ..lib.dedupe import SeenStore
from ..lib.env import env, env_flag
from ..lib.filters import prefilter
from ..lib.logging import debug, error, info, warn
from ..lib.paths import cache_dir
from ..lib.types import RawItem


SOURCE = "x"
COOKIES_PATH = cache_dir() / "twitter-cookies.json"
PLAYWRIGHT_COOKIES_PATH = cache_dir() / "twitter-cookies-playwright.json"

# X cookies that matter for authenticated reads. Everything else we keep
# as-is from the browser session but won't reconstruct ourselves.
ESSENTIAL_COOKIES = {"auth_token", "ct0", "twid", "guest_id"}


# ---------------------------------------------------------------------------
# Cookie bootstrap
# ---------------------------------------------------------------------------


def _hydrate_cookies_from_env() -> None:
    """On CI, decode TWITTER_COOKIES_B64 into the cookies file before any login."""

    if COOKIES_PATH.exists() and PLAYWRIGHT_COOKIES_PATH.exists():
        return
    b64 = env("TWITTER_COOKIES_B64")
    if not b64:
        return
    try:
        raw = base64.b64decode(b64).decode("utf-8")
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as e:
        warn(f"twitter: TWITTER_COOKIES_B64 could not be decoded: {e}")
        return

    # Accept either Playwright-format list or twikit-format dict.
    if isinstance(payload, list):
        PLAYWRIGHT_COOKIES_PATH.write_text(json.dumps(payload), encoding="utf-8")
        flat = {c["name"]: c["value"] for c in payload if "name" in c and "value" in c}
        COOKIES_PATH.write_text(json.dumps(flat), encoding="utf-8")
        info("twitter: hydrated cookies from TWITTER_COOKIES_B64 (Playwright format)")
    elif isinstance(payload, dict):
        COOKIES_PATH.write_text(json.dumps(payload), encoding="utf-8")
        info("twitter: hydrated twikit cookies from TWITTER_COOKIES_B64 (flat dict)")
    else:
        warn("twitter: TWITTER_COOKIES_B64 payload is not list or dict; ignoring")


# ---------------------------------------------------------------------------
# twikit primary path
# ---------------------------------------------------------------------------


async def _twikit_login_or_load() -> Any | None:
    """Return a logged-in twikit Client or None on failure."""

    try:
        from twikit import Client
    except ImportError:
        warn("twitter: twikit not installed; skipping primary path")
        return None

    client = Client(language="en-US")

    # Try loading saved cookies first.
    if COOKIES_PATH.exists():
        try:
            client.load_cookies(str(COOKIES_PATH))
            # Verify the session — a cheap call.
            user = await client.user()
            info(f"twitter: twikit session loaded as @{getattr(user, 'screen_name', '?')}")
            return client
        except Exception as e:  # noqa: BLE001
            warn(f"twitter: saved cookies rejected by twikit: {e}; trying fresh login")

    # Fresh login fallback. Requires TWITTER_USERNAME + TWITTER_EMAIL + TWITTER_PASSWORD.
    username = env("TWITTER_USERNAME")
    email = env("TWITTER_EMAIL")
    password = env("TWITTER_PASSWORD")
    totp_secret = env("TWITTER_TOTP_SECRET") or None
    if not (username and email and password):
        warn("twitter: TWITTER_USERNAME/EMAIL/PASSWORD not set and no usable cookies")
        return None

    try:
        kwargs: dict[str, Any] = {
            "auth_info_1": username,
            "auth_info_2": email,
            "password": password,
        }
        if totp_secret:
            kwargs["totp_secret"] = totp_secret
        await client.login(**kwargs)
        client.save_cookies(str(COOKIES_PATH))
        info("twitter: twikit fresh login OK; cookies saved")
        return client
    except Exception as e:  # noqa: BLE001
        warn(f"twitter: twikit login failed: {e}")
        return None


def _tweet_to_item(tweet: Any, *, attributed_to: str) -> RawItem | None:
    """Convert a twikit Tweet object to a RawItem. `attributed_to` is the source query / user."""

    try:
        tweet_id = str(tweet.id)
    except AttributeError:
        return None

    user = getattr(tweet, "user", None)
    screen_name = getattr(user, "screen_name", None) if user else None
    handle = f"@{screen_name}" if screen_name else None

    text = (getattr(tweet, "text", None) or getattr(tweet, "full_text", None) or "").strip()
    if not text:
        return None

    created_raw = getattr(tweet, "created_at", None)
    if isinstance(created_raw, datetime):
        created_at = created_raw if created_raw.tzinfo else created_raw.replace(tzinfo=timezone.utc)
    elif isinstance(created_raw, str):
        # twikit usually surfaces strings like "Wed Mar 12 14:47:28 +0000 2026"
        with contextlib.suppress(ValueError):
            created_at = datetime.strptime(created_raw, "%a %b %d %H:%M:%S %z %Y")
    else:
        created_at = datetime.now(timezone.utc)
    if not isinstance(created_at, datetime):  # safety net
        created_at = datetime.now(timezone.utc)

    url = (
        f"https://twitter.com/{screen_name}/status/{tweet_id}"
        if screen_name
        else f"https://twitter.com/i/status/{tweet_id}"
    )

    extra: dict[str, Any] = {
        "via": "twikit",
        "attributed_to": attributed_to,
        "favorite_count": getattr(tweet, "favorite_count", None),
        "retweet_count": getattr(tweet, "retweet_count", None),
        "reply_count": getattr(tweet, "reply_count", None),
        "view_count": getattr(tweet, "view_count", None),
        "lang": getattr(tweet, "lang", None),
    }

    return RawItem(
        id=f"{SOURCE}-{tweet_id}",
        source=SOURCE,
        title=text[:120].replace("\n", " "),
        body=text,
        url=url,
        author=handle,
        created_at=created_at,
        extra=extra,
    )


async def _fetch_via_twikit() -> list[RawItem]:
    cfg = twitter_watch()
    if not cfg:
        return []

    client = await _twikit_login_or_load()
    if client is None:
        return []

    fetch_cfg = cfg.get("fetch", {})
    max_search = int(fetch_cfg.get("max_search_results", 12))
    max_user = int(fetch_cfg.get("max_user_tweets", 8))
    delay = float(env("ROUTR_SIGNAL_TWITTER_DELAY_S") or fetch_cfg.get("request_delay_seconds", 30))
    total_cap = int(fetch_cfg.get("total_max_items", 80))
    search_product = str(fetch_cfg.get("search_product", "Latest"))

    items: list[RawItem] = []

    # Searches
    for query in cfg.get("searches", []):
        if len(items) >= total_cap:
            break
        try:
            tweets = await client.search_tweet(query, search_product, count=max_search)
        except Exception as e:  # noqa: BLE001
            warn(f"twitter: search {query!r} failed: {e}")
            continue
        added = 0
        for t in tweets[:max_search]:
            it = _tweet_to_item(t, attributed_to=f"search:{query}")
            if it is not None:
                items.append(it)
                added += 1
        debug(f"twitter: search {query!r} -> {added} items")
        await asyncio.sleep(delay)

    # User timelines
    for handle in cfg.get("users", []):
        if len(items) >= total_cap:
            break
        try:
            user = await client.get_user_by_screen_name(handle)
            tweets = await user.get_tweets("Tweets", count=max_user)
        except Exception as e:  # noqa: BLE001
            warn(f"twitter: user @{handle} failed: {e}")
            continue
        added = 0
        for t in tweets[:max_user]:
            it = _tweet_to_item(t, attributed_to=f"user:@{handle}")
            if it is not None:
                items.append(it)
                added += 1
        debug(f"twitter: user @{handle} -> {added} items")
        await asyncio.sleep(delay)

    return items


# ---------------------------------------------------------------------------
# Playwright fallback path
# ---------------------------------------------------------------------------


STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
"""


def _load_playwright_cookies() -> list[dict[str, Any]] | None:
    if PLAYWRIGHT_COOKIES_PATH.exists():
        try:
            data = json.loads(PLAYWRIGHT_COOKIES_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError) as e:
            warn(f"twitter: playwright cookies unreadable: {e}")
    # Fall back to reconstructing minimal cookies from the twikit cookie file.
    if COOKIES_PATH.exists():
        try:
            flat = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
            if not isinstance(flat, dict):
                return None
        except (json.JSONDecodeError, OSError):
            return None
        synthetic: list[dict[str, Any]] = []
        for name, value in flat.items():
            if name not in ESSENTIAL_COOKIES:
                continue
            synthetic.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".twitter.com",
                    "path": "/",
                    "expires": int(time.time()) + 60 * 60 * 24 * 30,
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
        # Also add for x.com — X uses both domains.
        synthetic.extend(
            [
                {**c, "domain": ".x.com"} for c in synthetic
            ]
        )
        return synthetic or None
    return None


# NOTE: JS payload below uses zero regex. Regex literals inside Python
# triple-quoted strings are an escaping nightmare (//`\d` needs `\\\\d` etc.)
# and X's URL shape (`/handle/status/12345`) is easily parsed via split().
_SCRAPE_JS = """
() => {
  const out = [];
  document.querySelectorAll("article[data-testid='tweet']").forEach(a => {
    try {
      // Find the canonical tweet URL via the timestamp link.
      const timeEl = a.querySelector("time");
      const linkEl = timeEl ? timeEl.closest('a') : null;
      const href = linkEl ? linkEl.getAttribute('href') : null;
      if (!href) return;

      // /<handle>/status/<id>(/...)  ->  handle = segs[statusIdx-1], id = segs[statusIdx+1]
      const segs = href.split('/').filter(s => s.length > 0);
      const statusIdx = segs.indexOf('status');
      if (statusIdx < 0) return;
      const handle = (statusIdx > 0) ? segs[statusIdx - 1] : null;
      const idCandidate = segs[statusIdx + 1] || '';
      const tid = idCandidate.split('?')[0].split('#')[0];
      if (!tid) return;

      // Body text.
      const textEl = a.querySelector("div[data-testid='tweetText']");
      const text = textEl ? textEl.innerText.trim() : '';

      const dt = timeEl ? timeEl.getAttribute('datetime') : null;

      if (tid && text) out.push({ id: tid, text: text, handle: handle, datetime: dt });
    } catch (e) {}
  });
  return out;
}
"""


async def _scrape_url(page: Any, url: str, *, max_items: int) -> list[dict[str, Any]]:
    """Navigate to url and scroll until we have max_items tweets in the DOM."""

    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(3_500)  # let React hydrate

    seen_ids: set[str] = set()
    scraped: list[dict[str, Any]] = []
    stagnant_iters = 0
    for _ in range(20):
        if len(scraped) >= max_items:
            break
        try:
            tweets = await page.evaluate(_SCRAPE_JS)
        except Exception as e:  # noqa: BLE001
            debug(f"twitter[pw]: page.evaluate raised: {e}")
            break
        new_this_round = 0
        for t in tweets:
            tid = t.get("id")
            if not tid or tid in seen_ids:
                continue
            seen_ids.add(tid)
            scraped.append(t)
            new_this_round += 1
        if new_this_round == 0:
            stagnant_iters += 1
        else:
            stagnant_iters = 0
        if stagnant_iters >= 3:
            break
        await page.evaluate("window.scrollBy(0, window.innerHeight * 2);")
        await page.wait_for_timeout(2_000)

    return scraped[:max_items]


async def _fetch_via_playwright_config(cfg: dict[str, Any]) -> list[RawItem]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        warn("twitter: playwright not installed; skipping fallback path")
        return []

    cookies = _load_playwright_cookies()
    if not cookies:
        warn("twitter: no usable cookies for Playwright fallback; skipping")
        return []

    fetch_cfg = cfg.get("fetch", {})
    max_search = int(fetch_cfg.get("max_search_results", 12))
    max_user = int(fetch_cfg.get("max_user_tweets", 8))
    delay_ms = int(float(env("ROUTR_SIGNAL_TWITTER_DELAY_S") or fetch_cfg.get("request_delay_seconds", 30)) * 1000)
    total_cap = int(fetch_cfg.get("total_max_items", 80))

    items: list[RawItem] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 900},
                locale="en-US",
            )
            await context.add_init_script(STEALTH_INIT_SCRIPT)
            await context.add_cookies(cookies)
            page = await context.new_page()

            for query in cfg.get("searches", []):
                if len(items) >= total_cap:
                    break
                url = f"https://x.com/search?q={query.replace(' ', '%20')}&src=typed_query&f=live"
                try:
                    rows = await _scrape_url(page, url, max_items=max_search)
                except Exception as e:  # noqa: BLE001
                    warn(f"twitter[pw]: search {query!r} failed: {e}")
                    continue
                added = 0
                for r in rows:
                    it = _row_to_item(r, attributed_to=f"search:{query}")
                    if it is not None:
                        items.append(it)
                        added += 1
                debug(f"twitter[pw]: search {query!r} -> {added} items")
                await page.wait_for_timeout(delay_ms)

            for handle in cfg.get("users", []):
                if len(items) >= total_cap:
                    break
                url = f"https://x.com/{handle}"
                try:
                    rows = await _scrape_url(page, url, max_items=max_user)
                except Exception as e:  # noqa: BLE001
                    warn(f"twitter[pw]: user @{handle} failed: {e}")
                    continue
                added = 0
                for r in rows:
                    it = _row_to_item(r, attributed_to=f"user:@{handle}")
                    if it is not None:
                        items.append(it)
                        added += 1
                debug(f"twitter[pw]: user @{handle} -> {added} items")
                await page.wait_for_timeout(delay_ms)
        finally:
            await browser.close()

    return items


async def _fetch_via_playwright() -> list[RawItem]:
    return await _fetch_via_playwright_config(twitter_watch())


def fetch_from_config(
    cfg: dict[str, Any],
    *,
    apply_keyword_prefilter: bool = True,
    id_prefix: str = SOURCE,
    persist_seen: bool = True,
) -> list[RawItem]:
    """Fetch X items from an explicit config dict.

    The daily source uses `twitter_watch.yaml` and source ids like `x-<tweet_id>`.
    Fast-reply monitoring passes a different config and `id_prefix="xwatch"` so
    broad AI monitoring does not consume the daily digest's X dedupe namespace.
    """

    if not cfg or (not cfg.get("searches") and not cfg.get("users")):
        return []

    _ensure_windows_proactor_loop()
    _hydrate_cookies_from_env()

    try:
        items = asyncio.run(_fetch_via_playwright_config(cfg))
        info(f"twitter: explicit config returned {len(items)} items")
    except Exception as e:  # noqa: BLE001
        error(f"twitter: explicit Playwright config crashed: {e}")
        items = []

    fresh: list[RawItem] = []
    for it in items:
        if id_prefix != SOURCE and it.id.startswith(f"{SOURCE}-"):
            it.id = f"{id_prefix}-{it.id.removeprefix(f'{SOURCE}-')}"
            it.extra["canonical_x_id"] = it.id.removeprefix(f"{id_prefix}-")
        if persist_seen and signal_store.is_seen(it.id):
            continue
        if persist_seen:
            signal_store.upsert_fetched(it, run_id=None)
        fresh.append(it)

    if apply_keyword_prefilter:
        filtered = prefilter(fresh)
        info(f"twitter: {len(fresh)} new, {len(filtered)} pass keyword prefilter")
        return filtered

    info(f"twitter: {len(fresh)} new, keyword prefilter skipped")
    return fresh


def _row_to_item(row: dict[str, Any], *, attributed_to: str) -> RawItem | None:
    tid = row.get("id")
    text = row.get("text") or ""
    if not tid or not text:
        return None
    handle = row.get("handle")
    author = f"@{handle}" if handle else None
    dt_str = row.get("datetime")
    if dt_str:
        try:
            created_at = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except ValueError:
            created_at = datetime.now(timezone.utc)
    else:
        created_at = datetime.now(timezone.utc)

    return RawItem(
        id=f"{SOURCE}-{tid}",
        source=SOURCE,
        title=text[:120].replace("\n", " "),
        body=text,
        url=f"https://twitter.com/{handle}/status/{tid}" if handle else f"https://twitter.com/i/status/{tid}",
        author=author,
        created_at=created_at,
        extra={"via": "playwright", "attributed_to": attributed_to},
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _ensure_windows_proactor_loop() -> None:
    """twikit's import side-effect can flip the policy to selector, which then
    breaks Playwright's subprocess spawn on Windows. Force proactor up front so
    both paths work in either order."""

    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def fetch() -> list[RawItem]:
    """Fetch X items via Playwright (primary). Optionally try twikit if enabled."""

    cfg = twitter_watch()
    if not cfg or (not cfg.get("searches") and not cfg.get("users")):
        info("twitter: config/twitter_watch.yaml is empty; source disabled")
        return []

    _ensure_windows_proactor_loop()
    _hydrate_cookies_from_env()

    seen = SeenStore(SOURCE)
    items: list[RawItem] = []

    # twikit is opt-in until upstream fixes the ClientTransaction regression.
    if env_flag("ROUTR_SIGNAL_X_USE_TWIKIT", default=False):
        try:
            items = asyncio.run(_fetch_via_twikit())
            info(f"twitter: twikit returned {len(items)} items")
        except Exception as e:  # noqa: BLE001
            warn(f"twitter: twikit path crashed: {e}")
            items = []

    if not items:
        try:
            items = asyncio.run(_fetch_via_playwright())
            info(f"twitter: Playwright returned {len(items)} items")
        except Exception as e:  # noqa: BLE001
            error(f"twitter: Playwright crashed: {e}")
            items = []

    # Dedupe
    fresh: list[RawItem] = []
    for it in items:
        if seen.has(it.id):
            continue
        seen.add_item(it)
        fresh.append(it)

    filtered = prefilter(fresh)
    info(f"twitter: {len(fresh)} new, {len(filtered)} pass keyword prefilter")

    seen.save()
    return filtered


if __name__ == "__main__":
    out = fetch()
    print(json.dumps([it.to_dict() for it in out], indent=2))
