"""One-time interactive X / Twitter login.

Usage:
    .\\.venv\\Scripts\\python.exe tools/twitter_login.py

What it does:
    1. Launches a headed Playwright Chromium and navigates to x.com.
    2. Polls every 2 seconds for a logged-in state (home timeline visible or
       the URL has left the login flow). Up to 5 minutes total.
    3. Any login method works: password + TOTP, password + email code,
       password + phone push notification, password + backup code, or just
       a previously-valid session that resumes automatically.
    4. As soon as login is detected, captures all cookies, writes them in
       both formats (Playwright list + twikit flat dict), prints the base64
       blob for the GitHub Actions secret, and exits.

The browser stays open while you log in and closes itself when done. There
is no "press Enter" step. If you finish login but cookies do not appear,
re-run the script.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
from pathlib import Path

# Make the package importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

from routr_signal.lib.paths import cache_dir  # noqa: E402


COOKIES_PATH = cache_dir() / "twitter-cookies.json"
PLAYWRIGHT_COOKIES_PATH = cache_dir() / "twitter-cookies-playwright.json"

MAX_WAIT_SECONDS = 5 * 60
POLL_INTERVAL_SECONDS = 2

# A logged-in X session always has these. We don't validate their format;
# their mere presence after the login flow means we're in.
ESSENTIAL_COOKIE_NAMES = {"auth_token", "ct0"}


async def _wait_for_login(page) -> bool:
    """Poll until X home timeline is loaded (or timeout). Returns True on success."""

    elapsed = 0
    last_url: str | None = None
    while elapsed < MAX_WAIT_SECONDS:
        try:
            url = page.url
        except Exception:
            url = ""
        if url != last_url:
            print(f"  [{elapsed:>3}s] url: {url}")
            last_url = url

        # Definitive signal: home timeline / primary column present.
        try:
            primary = await page.query_selector('div[data-testid="primaryColumn"]')
        except Exception:
            primary = None
        if primary and "/i/flow" not in url and "/login" not in url:
            print(f"  [{elapsed:>3}s] home timeline detected -> login complete")
            return True

        # Secondary signal: we left the login flow and a tweet article exists.
        if "/i/flow" not in url and "/login" not in url:
            try:
                tweet = await page.query_selector('article[data-testid="tweet"]')
            except Exception:
                tweet = None
            if tweet:
                print(f"  [{elapsed:>3}s] tweet article visible -> login complete")
                return True

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS

    return False


async def capture() -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright is not installed. Run: pip install -e .", file=sys.stderr)
        return 1

    print("Opening Chromium. Log in to your X burner account in that window.")
    print("Use any verification method: password + TOTP / email code / phone push / backup code.")
    print(f"This script waits up to {MAX_WAIT_SECONDS // 60} minutes and captures cookies automatically.")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 900},
                locale="en-US",
            )
            page = await context.new_page()
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60_000)

            ok = await _wait_for_login(page)
            if not ok:
                print()
                print("Timed out waiting for the home timeline.")
                print("If you completed login but the timeline did not load, refresh and re-run.")
                cookies = await context.cookies()
            else:
                cookies = await context.cookies()
        finally:
            await browser.close()

    # Sanity check: do we have the essentials?
    cookie_names = {c.get("name") for c in cookies}
    missing = ESSENTIAL_COOKIE_NAMES - cookie_names
    if missing:
        print()
        print(f"WARNING: missing essential cookies: {sorted(missing)}")
        print("The session is probably not authenticated. Re-run after completing login.")
        return 2

    # Persist both formats.
    PLAYWRIGHT_COOKIES_PATH.write_text(
        json.dumps(cookies, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"playwright cookies -> {PLAYWRIGHT_COOKIES_PATH}  ({len(cookies)} entries)")

    flat = {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}
    COOKIES_PATH.write_text(json.dumps(flat, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"twikit cookies     -> {COOKIES_PATH}     ({len(flat)} entries)")

    # Print base64 for the GitHub secret.
    b64 = base64.b64encode(
        json.dumps(cookies, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")

    b64_path = cache_dir() / "twitter-cookies-b64.txt"
    b64_path.write_text(b64, encoding="utf-8")
    print(f"base64 for CI      -> {b64_path}")
    print()
    print(f"length: {len(b64):,} chars")
    print()
    print("Next step: load the base64 into the TWITTER_COOKIES_B64 GitHub secret.")
    print("From this directory:")
    print(f'  Get-Content -LiteralPath "{b64_path}" -Raw | gh secret set TWITTER_COOKIES_B64')

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(capture()))
