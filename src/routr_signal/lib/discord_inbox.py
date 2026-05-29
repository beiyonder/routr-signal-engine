"""Discord REST client for reading reactions on bot/webhook messages.

We do NOT maintain a persistent gateway connection. The dispatch worker
calls these REST endpoints on a 15-minute cron from GitHub Actions.

Auth: bot token (`DISCORD_BOT_TOKEN`). The bot must be present in the
server (invited via the OAuth URL with permissions=68672 = View Channel +
Send Messages + Read Message History + Add Reactions).

Used by `tasks/dispatch_approved.py`.
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any

import httpx

from .env import env_required
from .logging import debug, warn


DISCORD_API_BASE = "https://discord.com/api/v10"
DEFAULT_TIMEOUT = 20.0


class DiscordError(RuntimeError):
    """Raised when Discord returns a non-2xx response."""


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bot {env_required('DISCORD_BOT_TOKEN')}",
        "User-Agent": "routr-signal-engine/0.1 (https://github.com/beiyonder/routr-signal-engine, 0.1)",
    }


def get_message(channel_id: str, message_id: str) -> dict[str, Any]:
    """Fetch a message by id.

    Returns the message JSON, which includes a `reactions` array if any
    reactions exist. Each entry: `{"count": N, "me": bool, "emoji": {...}}`.

    Handles Discord's 429 rate limit by sleeping for `retry_after` seconds
    and retrying up to 3 times.
    """

    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}"
    for attempt in range(3):
        try:
            resp = httpx.get(url, headers=_headers(), timeout=DEFAULT_TIMEOUT)
        except httpx.HTTPError as e:
            raise DiscordError(f"discord: network error: {e}") from e

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError as e:
                raise DiscordError("discord: non-JSON response on getMessage") from e
        if resp.status_code == 404:
            return {}
        if resp.status_code == 429:
            retry_after = _retry_after_seconds(resp)
            debug(
                f"discord: 429 on getMessage {message_id}; sleeping {retry_after:.2f}s "
                f"(attempt {attempt + 1}/3)"
            )
            time.sleep(retry_after)
            continue
        raise DiscordError(
            f"discord: HTTP {resp.status_code} on getMessage: {resp.text[:300]!r}"
        )

    raise DiscordError(
        f"discord: gave up on getMessage {message_id} after 3 rate-limit retries"
    )


def message_has_reaction(
    channel_id: str,
    message_id: str,
    emoji: str,
) -> bool:
    """Cheap existence check: does the message carry at least one of `emoji`?

    `emoji` is the unicode character (e.g. "✅"). The bot's own reactions
    count too, so the dispatch worker should add a confirmation reaction
    AFTER posting to prevent re-dispatch on the next poll cycle. See
    `pending_approvals_have_already_been_marked()` helper below.
    """

    msg = get_message(channel_id, message_id)
    if not msg:
        return False
    reactions = msg.get("reactions") or []
    for r in reactions:
        if not isinstance(r, dict):
            continue
        emoji_obj = r.get("emoji") or {}
        if not isinstance(emoji_obj, dict):
            continue
        if emoji_obj.get("name") == emoji:
            return True
    return False


def list_reaction_users(
    channel_id: str,
    message_id: str,
    emoji: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get the list of users who reacted to a message with a given emoji.

    Useful for permission gating later (e.g., only act on reactions from
    the owner's user id). For now the dispatch worker doesn't gate; any
    approval emoji in the channel triggers a post.

    Handles 429 the same way as `get_message`.
    """

    encoded = urllib.parse.quote(emoji, safe="")
    url = (
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}"
        f"/reactions/{encoded}?limit={max(1, min(100, limit))}"
    )
    for attempt in range(3):
        try:
            resp = httpx.get(url, headers=_headers(), timeout=DEFAULT_TIMEOUT)
        except httpx.HTTPError as e:
            raise DiscordError(f"discord: network error on listReactions: {e}") from e

        if resp.status_code == 200:
            try:
                users = resp.json()
            except ValueError as e:
                raise DiscordError("discord: non-JSON response on listReactions") from e
            return users if isinstance(users, list) else []
        if resp.status_code == 404:
            return []
        if resp.status_code == 429:
            retry_after = _retry_after_seconds(resp)
            debug(
                f"discord: 429 on listReactions {message_id}; sleeping {retry_after:.2f}s "
                f"(attempt {attempt + 1}/3)"
            )
            time.sleep(retry_after)
            continue
        raise DiscordError(
            f"discord: HTTP {resp.status_code} on listReactions: {resp.text[:300]!r}"
        )

    raise DiscordError(
        f"discord: gave up on listReactions {message_id} after 3 rate-limit retries"
    )


def add_bot_reaction(channel_id: str, message_id: str, emoji: str) -> bool:
    """Add a reaction to a message as the bot.

    Used by the dispatch worker to mark a message as 'processed' so the next
    poll doesn't re-trigger. The bot adds a different emoji than the
    trigger one (e.g., bot adds 🚀 after posting; trigger was ✅).

    Returns True on success, False on persistent failure. Handles 429.
    """

    encoded = urllib.parse.quote(emoji, safe="")
    url = (
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}"
        f"/reactions/{encoded}/@me"
    )
    for attempt in range(3):
        try:
            resp = httpx.put(url, headers=_headers(), timeout=DEFAULT_TIMEOUT)
        except httpx.HTTPError as e:
            warn(f"discord: add_bot_reaction network error: {e}")
            return False

        if resp.status_code in (200, 204):
            return True
        if resp.status_code == 429:
            retry_after = _retry_after_seconds(resp)
            debug(
                f"discord: 429 on add_bot_reaction {message_id}; sleeping {retry_after:.2f}s "
                f"(attempt {attempt + 1}/3)"
            )
            time.sleep(retry_after)
            continue
        warn(f"discord: add_bot_reaction HTTP {resp.status_code}: {resp.text[:200]!r}")
        return False

    warn(f"discord: gave up on add_bot_reaction {message_id} after 3 rate-limit retries")
    return False


def has_bot_marker_reaction(channel_id: str, message_id: str, marker_emoji: str) -> bool:
    """Has the bot already added its 'processed' marker to this message?

    True means: the dispatch worker already handled this message; skip.
    """

    msg = get_message(channel_id, message_id)
    if not msg:
        return False
    reactions = msg.get("reactions") or []
    for r in reactions:
        if not isinstance(r, dict):
            continue
        if not r.get("me"):
            continue
        emoji_obj = r.get("emoji") or {}
        if isinstance(emoji_obj, dict) and emoji_obj.get("name") == marker_emoji:
            return True
    return False


def is_configured() -> bool:
    try:
        env_required("DISCORD_BOT_TOKEN")
        env_required("DISCORD_CHANNEL_ID")
        return True
    except Exception:
        return False


def is_dm_configured() -> bool:
    try:
        env_required("DISCORD_BOT_TOKEN")
        return True
    except Exception:
        return False



# ---------------------------------------------------------------------------
# Direct Messages (DMs to a specific user)
# ---------------------------------------------------------------------------
#
# The X-burst task uses DMs to deliver long-form posts (> Buffer's 280-char
# ceiling) that the operator copy-pastes into X by hand. Bot DMs work
# under these conditions:
#
#   1. The bot must share at least one guild with the recipient.
#   2. The recipient must have "Allow direct messages from server members"
#      enabled for that guild (Discord default = on).
#
# Discord's per-message content cap is 2,000 chars. Long X posts can be
# up to 25,000 chars, so we chunk by paragraph boundaries with a small
# header marker (`(part X / Y)`) on each chunk so the operator can copy
# them in order. The first chunk also carries an "envelope" describing
# the total length and the anchor signal URL.
#
# Auth is the same bot token as the rest of this module; the bot needs
# Send Messages permission in any guild it shares with the recipient
# (which it already has from the digest channel invite).


MAX_DM_BODY_CHARS = 1900   # leave 100 chars of headroom under Discord's 2000 cap
DM_OPEN_ENDPOINT = f"{DISCORD_API_BASE}/users/@me/channels"


def _open_dm_channel(user_id: str) -> str | None:
    """Open (or fetch) the DM channel id for `user_id`. Returns None on failure."""

    for attempt in range(3):
        try:
            resp = httpx.post(
                DM_OPEN_ENDPOINT,
                headers={**_headers(), "Content-Type": "application/json"},
                json={"recipient_id": user_id},
                timeout=DEFAULT_TIMEOUT,
            )
        except httpx.HTTPError as e:
            warn(f"discord: open_dm network error: {e}")
            return None

        if resp.status_code in (200, 201):
            try:
                ch = resp.json()
            except ValueError:
                warn("discord: open_dm returned non-JSON")
                return None
            ch_id = ch.get("id") if isinstance(ch, dict) else None
            return ch_id if isinstance(ch_id, str) else None
        if resp.status_code == 429:
            retry_after = _retry_after_seconds(resp)
            debug(f"discord: 429 on open_dm; sleeping {retry_after:.2f}s (attempt {attempt + 1}/3)")
            time.sleep(retry_after)
            continue
        warn(f"discord: open_dm HTTP {resp.status_code}: {resp.text[:200]!r}")
        return None

    warn("discord: gave up on open_dm after 3 rate-limit retries")
    return None


def _post_dm_message(dm_channel_id: str, content: str) -> str | None:
    """Send one DM message. Returns the message id on success, else None."""

    url = f"{DISCORD_API_BASE}/channels/{dm_channel_id}/messages"
    for attempt in range(3):
        try:
            resp = httpx.post(
                url,
                headers={**_headers(), "Content-Type": "application/json"},
                json={"content": content},
                timeout=DEFAULT_TIMEOUT,
            )
        except httpx.HTTPError as e:
            warn(f"discord: send_dm network error: {e}")
            return None

        if resp.status_code in (200, 201):
            try:
                msg = resp.json()
            except ValueError:
                warn("discord: send_dm returned non-JSON")
                return None
            mid = msg.get("id") if isinstance(msg, dict) else None
            return mid if isinstance(mid, str) else None
        if resp.status_code == 429:
            retry_after = _retry_after_seconds(resp)
            debug(f"discord: 429 on send_dm; sleeping {retry_after:.2f}s (attempt {attempt + 1}/3)")
            time.sleep(retry_after)
            continue
        warn(f"discord: send_dm HTTP {resp.status_code}: {resp.text[:200]!r}")
        return None

    warn("discord: gave up on send_dm after 3 rate-limit retries")
    return None


def _chunk_long_text(text: str, *, max_chars: int = MAX_DM_BODY_CHARS) -> list[str]:
    """Split `text` into chunks of at most `max_chars`, preferring paragraph
    boundaries (\\n\\n), then line boundaries (\\n), then word boundaries.

    Pure-text input; no markdown wrapping (the caller adds the code fence).
    """

    if len(text) <= max_chars:
        return [text]

    out: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            out.append(remaining)
            break

        window = remaining[:max_chars]
        # Prefer the last paragraph break inside the window.
        idx = window.rfind("\n\n")
        if idx < max_chars // 2:  # too early; try a single newline
            idx = window.rfind("\n")
        if idx < max_chars // 2:  # too early; try a sentence-end period+space
            idx = window.rfind(". ")
            if idx >= 0:
                idx += 1   # keep the period with the chunk above
        if idx < max_chars // 2:  # too early; just break on the last space
            idx = window.rfind(" ")
        if idx <= 0:
            idx = max_chars   # hard cut

        out.append(remaining[:idx].rstrip())
        remaining = remaining[idx:].lstrip()
    return out


def send_dm(
    user_id: str,
    body: str,
    *,
    header: str | None = None,
    code_block: bool = True,
) -> list[str]:
    """DM `body` to `user_id`, chunking automatically over Discord's 2000-char
    message cap.

    `header` is sent as a leading non-code message describing the payload
    (e.g., "Manual X post (847 chars) anchored to https://...").

    `code_block=True` wraps each chunk in a triple-backtick fence so the
    operator can copy the raw post text with Discord's built-in "copy"
    affordance (the X-burst use case). Set False for plain text DMs.

    Returns the list of message ids sent (header + chunks). Empty list on
    total failure.
    """

    result = send_dm_with_channel(user_id, body, header=header, code_block=code_block)
    return result[1] if result else []


def send_dm_with_channel(
    user_id: str,
    body: str,
    *,
    header: str | None = None,
    code_block: bool = True,
) -> tuple[str, list[str]] | None:
    """DM `body` and return the DM channel id plus sent message ids."""

    dm_channel = _open_dm_channel(user_id)
    if not dm_channel:
        return None

    msg_ids: list[str] = []

    if header:
        # Header is plain text, no chunking expected (we keep these short).
        head = header if len(header) <= MAX_DM_BODY_CHARS else header[:MAX_DM_BODY_CHARS - 3] + "..."
        mid = _post_dm_message(dm_channel, head)
        if mid:
            msg_ids.append(mid)

    # When code_block is enabled, each chunk gets wrapped in ``` fences. The
    # raw wrapper takes 8 chars (```\n + \n```), so reduce the per-chunk
    # body cap accordingly to stay under Discord's 2000 limit.
    chunk_cap = MAX_DM_BODY_CHARS - 16 if code_block else MAX_DM_BODY_CHARS

    chunks = _chunk_long_text(body, max_chars=chunk_cap)
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        prefix = f"(part {i} / {total})\n" if total > 1 else ""
        payload = f"{prefix}```\n{chunk}\n```" if code_block else f"{prefix}{chunk}"
        mid = _post_dm_message(dm_channel, payload)
        if mid:
            msg_ids.append(mid)
        else:
            warn(f"discord: send_dm chunk {i}/{total} failed; aborting remaining chunks")
            break
    return (dm_channel, msg_ids) if msg_ids else None



def _retry_after_seconds(resp: httpx.Response) -> float:
    """Parse Discord's Retry-After (header or JSON body). Defaults to 1.0s
    when missing/unparseable."""

    hdr = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    if hdr:
        try:
            return max(0.1, float(hdr))
        except ValueError:
            pass
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return 1.0
    ra = body.get("retry_after") if isinstance(body, dict) else None
    if isinstance(ra, (int, float)):
        return max(0.1, float(ra))
    return 1.0
