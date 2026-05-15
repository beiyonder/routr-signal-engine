"""One-time X / Twitter session bootstrap.

Two modes. Pick the one that fits your situation.

Mode 1 (RECOMMENDED) -- import cookies from a real browser
==========================================================
You are already logged in to X in Chrome / Edge / Firefox. Export your
cookies for `x.com` to a JSON file and point this tool at it. X's anti-bot
never sees a login attempt, so this works regardless of what fingerprint
defenses they have shipped today.

    1. Install a cookie export extension in your real browser:
       Chrome / Edge -- Cookie-Editor (by Moustachauve), free
       Firefox       -- Cookie Quick Manager, free
    2. Open https://x.com in that browser (make sure you are logged in).
    3. Open the extension on the x.com tab, "Export" -> "Export as JSON",
       save the file somewhere (e.g. Downloads\\xcom-cookies.json).
    4. Run:
         python tools/twitter_login.py --import Downloads\\xcom-cookies.json

Mode 2 (FALLBACK) -- log in via a controlled Chromium
=====================================================
Slower, brittle, may be blocked by X's bot detection. Use only if Mode 1 is
not possible.

    python tools/twitter_login.py --browser

The script opens a headed Chromium with stealth patches, navigates to x.com,
and polls every 2 seconds for a logged-in state. As soon as the home
timeline loads, it captures cookies and exits.

Output (both modes)
===================
Writes three files into data/cache/:
    twitter-cookies.json              (twikit flat-dict format)
    twitter-cookies-playwright.json   (Playwright list format)
    twitter-cookies-b64.txt           (base64 of the Playwright file for CI)

Then run, to push the base64 to GitHub Actions:
    Get-Content -LiteralPath "data/cache/twitter-cookies-b64.txt" -Raw | gh secret set TWITTER_COOKIES_B64
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import sys
import time
from pathlib import Path
from typing import Any

# Make the package importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

from routr_signal.lib.paths import cache_dir  # noqa: E402


COOKIES_PATH = cache_dir() / "twitter-cookies.json"
PLAYWRIGHT_COOKIES_PATH = cache_dir() / "twitter-cookies-playwright.json"
B64_PATH = cache_dir() / "twitter-cookies-b64.txt"

MAX_WAIT_SECONDS = 5 * 60
POLL_INTERVAL_SECONDS = 2

# Essential cookies; without these the session is not authenticated.
ESSENTIAL_COOKIE_NAMES = {"auth_token", "ct0"}

# Stealth init script -- masks the automation tell-tales X looks for.
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {}, app: {}, csi: () => {}, loadTimes: () => {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(screen, 'availWidth',  { get: () => 1920 });
Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
"""


# ---------------------------------------------------------------------------
# Mode 1: cookie import
# ---------------------------------------------------------------------------


_SAMESITE_MAP = {
    "no_restriction": "None",
    "none": "None",
    "lax": "Lax",
    "strict": "Strict",
    "unspecified": "Lax",
    "": "Lax",
}


def _normalize_cookie_editor(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Cookie-Editor JSON rows to Playwright `add_cookies` format."""

    out: list[dict[str, Any]] = []
    for r in rows:
        name = r.get("name")
        value = r.get("value")
        domain = r.get("domain") or ".x.com"
        path = r.get("path") or "/"
        if not name or value is None:
            continue
        cookie: dict[str, Any] = {
            "name": name,
            "value": str(value),
            "domain": domain,
            "path": path,
            "secure": bool(r.get("secure", True)),
            "httpOnly": bool(r.get("httpOnly", False)),
            "sameSite": _SAMESITE_MAP.get(str(r.get("sameSite", "Lax")).lower(), "Lax"),
        }
        # Cookie-Editor uses `expirationDate` (float), Playwright wants `expires`.
        for k in ("expirationDate", "expires", "expiry"):
            if k in r and r[k] is not None:
                with _suppress(ValueError, TypeError):
                    cookie["expires"] = int(float(r[k]))
                    break
        else:
            cookie["expires"] = int(time.time()) + 60 * 60 * 24 * 365
        out.append(cookie)
    return out


class _suppress:
    """Tiny context manager so the line above stays a one-liner."""

    def __init__(self, *exc: type[BaseException]) -> None:
        self.exc = exc

    def __enter__(self) -> "_suppress":
        return self

    def __exit__(self, exc_type, *_: object) -> bool:
        return exc_type is not None and issubclass(exc_type, self.exc)


def _parse_netscape(text: str) -> list[dict[str, Any]]:
    """Parse a Netscape cookies.txt-format file into Cookie-Editor-like rows."""

    rows: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expires, name, value = parts[:7]
        rows.append(
            {
                "domain": domain,
                "path": path,
                "secure": secure.upper() == "TRUE",
                "expirationDate": float(expires) if expires.isdigit() else None,
                "name": name,
                "value": value,
            }
        )
    return rows


def import_cookies(path: Path) -> int:
    if not path.exists():
        print(f"ERROR: {path} does not exist", file=sys.stderr)
        return 1
    text = path.read_text(encoding="utf-8")

    rows: list[dict[str, Any]]
    text_stripped = text.lstrip()
    if text_stripped.startswith("[") or text_stripped.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            print(f"ERROR: file is not valid JSON: {e}", file=sys.stderr)
            return 1
        rows = payload if isinstance(payload, list) else payload.get("cookies", [])
    else:
        rows = _parse_netscape(text)

    if not rows:
        print(
            "ERROR: no cookies parsed. Expected Cookie-Editor JSON or Netscape cookies.txt",
            file=sys.stderr,
        )
        return 1

    # Only keep cookies for x.com / twitter.com domains.
    rows = [
        r
        for r in rows
        if isinstance(r, dict)
        and any(d in (r.get("domain") or "") for d in ("x.com", "twitter.com"))
    ]
    print(f"parsed {len(rows)} cookies for x.com / twitter.com")

    cookies = _normalize_cookie_editor(rows)

    # Add a mirror set for the sister domain so requests against either resolve.
    cookies = _mirror_domains(cookies)
    return _finalize(cookies)


def _mirror_domains(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """X serves both x.com and twitter.com; mirror the auth cookies across both."""

    out = list(cookies)
    have_pairs = {(c["name"], c["domain"]) for c in cookies}
    for c in cookies:
        name = c["name"]
        domain = c["domain"]
        sister = None
        if "x.com" in domain:
            sister = domain.replace("x.com", "twitter.com")
        elif "twitter.com" in domain:
            sister = domain.replace("twitter.com", "x.com")
        if sister and (name, sister) not in have_pairs:
            out.append({**c, "domain": sister})
            have_pairs.add((name, sister))
    return out


# ---------------------------------------------------------------------------
# Mode 2: browser login
# ---------------------------------------------------------------------------


async def _wait_for_login(page) -> bool:
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

        try:
            primary = await page.query_selector('div[data-testid="primaryColumn"]')
        except Exception:
            primary = None
        if primary and "/i/flow" not in url and "/login" not in url:
            print(f"  [{elapsed:>3}s] home timeline detected -> login complete")
            return True

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS

    return False


async def browser_capture() -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright is not installed. Run: pip install -e .", file=sys.stderr)
        return 1

    print("Opening a stealth Chromium. Log in to your X burner account.")
    print(f"Waits up to {MAX_WAIT_SECONDS // 60} minutes, captures cookies automatically.")
    print()
    print("IMPORTANT: if X freezes the login flow (e.g. won't show the password")
    print("screen after you type the username), close the window and use Mode 1")
    print("instead (cookie import). See `python tools/twitter_login.py --help`.")
    print()

    profile_dir = cache_dir() / "playwright-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # Persistent context shares storage with the previous run, so X sees a
        # stable client over time -- much less suspicious than a fresh fingerprint
        # on every launch.
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        try:
            await context.add_init_script(STEALTH_INIT_SCRIPT)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60_000)

            ok = await _wait_for_login(page)
            cookies = await context.cookies()
            if not ok:
                print()
                print("Timed out waiting for the home timeline.")
                print("Use Mode 1 (cookie import) -- see --help.")
        finally:
            await context.close()

    return _finalize(cookies)


# ---------------------------------------------------------------------------
# Shared finalize step
# ---------------------------------------------------------------------------


def _finalize(cookies: list[dict[str, Any]]) -> int:
    cookie_names = {c.get("name") for c in cookies}
    missing = ESSENTIAL_COOKIE_NAMES - cookie_names
    if missing:
        print()
        print(f"WARNING: missing essential cookies: {sorted(missing)}")
        print("Either you are not logged in, or the export skipped these cookies.")
        print("Re-export with the 'Include httpOnly cookies' option enabled.")
        return 2

    PLAYWRIGHT_COOKIES_PATH.write_text(
        json.dumps(cookies, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"playwright cookies -> {PLAYWRIGHT_COOKIES_PATH}  ({len(cookies)} entries)")

    flat = {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}
    COOKIES_PATH.write_text(json.dumps(flat, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"twikit cookies     -> {COOKIES_PATH}     ({len(flat)} entries)")

    b64 = base64.b64encode(
        json.dumps(cookies, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    B64_PATH.write_text(b64, encoding="utf-8")
    print(f"base64 for CI      -> {B64_PATH}  ({len(b64):,} chars)")
    print()
    print("Next, push to CI:")
    print(f'  Get-Content -LiteralPath "{B64_PATH}" -Raw | gh secret set TWITTER_COOKIES_B64')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="X / Twitter session bootstrap")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--import",
        dest="import_path",
        metavar="FILE",
        help="Import cookies from a Cookie-Editor JSON or Netscape cookies.txt file "
        "exported from your real browser (RECOMMENDED).",
    )
    mode.add_argument(
        "--browser",
        action="store_true",
        help="Open a controlled Chromium window so you can log in interactively. "
        "May be blocked by X's anti-bot.",
    )
    args = parser.parse_args()

    if args.import_path:
        return import_cookies(Path(args.import_path))
    if args.browser:
        return asyncio.run(browser_capture())

    parser.print_help()
    print()
    print("No mode selected. Pick one:")
    print("  python tools/twitter_login.py --import path\\to\\cookies.json   # RECOMMENDED")
    print("  python tools/twitter_login.py --browser                          # fallback")
    return 1


if __name__ == "__main__":
    sys.exit(main())
