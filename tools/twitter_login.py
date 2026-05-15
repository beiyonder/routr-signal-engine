"""One-time interactive X / Twitter login.

Usage:
    .\\.venv\\Scripts\\python.exe tools/twitter_login.py

What it does:
    1. Reads TWITTER_USERNAME / EMAIL / PASSWORD / TOTP_SECRET from .env
    2. Attempts a twikit login (using TOTP if 2FA is enabled).
    3. Saves the twikit cookies to data/cache/twitter-cookies.json.
    4. Launches a headed Playwright Chromium, navigates to x.com, waits for
       the user to confirm the login is real (Home timeline visible), then
       dumps the full cookie set to data/cache/twitter-cookies-playwright.json.
    5. Prints a base64-encoded copy of the Playwright cookies so it can be
       added to GitHub Actions as the TWITTER_COOKIES_B64 secret.

Run this on your dev machine. The cookie files are gitignored. Use a burner
account; never use a personal account.
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

from routr_signal.lib.env import env, env_required  # noqa: E402
from routr_signal.lib.paths import cache_dir  # noqa: E402


COOKIES_PATH = cache_dir() / "twitter-cookies.json"
PLAYWRIGHT_COOKIES_PATH = cache_dir() / "twitter-cookies-playwright.json"


async def _twikit_login() -> bool:
    try:
        from twikit import Client
    except ImportError:
        print("twikit not installed. Run: pip install -e .", file=sys.stderr)
        return False

    username = env_required("TWITTER_USERNAME")
    email = env_required("TWITTER_EMAIL")
    password = env_required("TWITTER_PASSWORD")
    totp_secret = env("TWITTER_TOTP_SECRET") or None

    print(f"twikit: logging in as @{username} ...")
    client = Client(language="en-US")
    kwargs = {"auth_info_1": username, "auth_info_2": email, "password": password}
    if totp_secret:
        kwargs["totp_secret"] = totp_secret
    try:
        await client.login(**kwargs)
    except Exception as e:
        print(f"twikit login failed: {e}", file=sys.stderr)
        return False
    client.save_cookies(str(COOKIES_PATH))
    print(f"twikit: cookies saved -> {COOKIES_PATH}")
    return True


async def _playwright_capture() -> bool:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright not installed. Run: pip install -e .", file=sys.stderr)
        return False

    print("playwright: launching headed Chromium (HEADED so you can confirm login)")
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

            # Try to seed cookies from twikit first so the user doesn't have to log in twice.
            if COOKIES_PATH.exists():
                try:
                    flat = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
                    if isinstance(flat, dict):
                        seed = [
                            {
                                "name": k,
                                "value": v,
                                "domain": d,
                                "path": "/",
                                "secure": True,
                                "httpOnly": False,
                                "sameSite": "Lax",
                            }
                            for k, v in flat.items()
                            for d in (".x.com", ".twitter.com")
                        ]
                        await context.add_cookies(seed)
                        print("playwright: seeded cookies from twikit save")
                except (OSError, json.JSONDecodeError):
                    pass

            page = await context.new_page()
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=45_000)
            print("playwright: a Chromium window opened. If you see the Home timeline you")
            print("            are logged in. If you see the login page, log in manually.")
            print("            When done, press Enter here.")
            try:
                input()
            except EOFError:
                # In non-interactive contexts, give the user a chance via a long wait.
                await page.wait_for_timeout(60_000)

            cookies = await context.cookies()
        finally:
            await browser.close()

    PLAYWRIGHT_COOKIES_PATH.write_text(
        json.dumps(cookies, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"playwright: cookies saved -> {PLAYWRIGHT_COOKIES_PATH}")

    # Also write a flat-dict copy for twikit to consume. twikit currently can't
    # complete a programmatic login against X (Apr 2025+ regression), so this is
    # the only way it gets an authenticated session.
    flat = {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}
    COOKIES_PATH.write_text(json.dumps(flat, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"twikit:     flat-dict cookies saved -> {COOKIES_PATH}")

    b64 = base64.b64encode(
        json.dumps(cookies, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    print()
    print("Set this on your GitHub repo as the TWITTER_COOKIES_B64 secret:")
    print()
    print("  gh secret set TWITTER_COOKIES_B64 --body @-  # then paste the long string below + Ctrl-Z (Windows) / Ctrl-D (unix)")
    print()
    print(b64)
    return True


async def main() -> int:
    # twikit programmatic login is currently regressed (KEY_BYTE error on
    # most accounts). We try it anyway in case it's working again, but the
    # Playwright capture is the authoritative path now.
    twikit_ok = False
    try:
        twikit_ok = await _twikit_login()
    except Exception as e:
        print(f"twikit: skipped due to error ({e})")
    pw_ok = await _playwright_capture()
    return 0 if pw_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
