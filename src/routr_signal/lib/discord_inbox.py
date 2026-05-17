"""Discord REST client for reading reactions on bot/webhook messages.

We do NOT maintain a persistent gateway connection. The dispatch worker
calls these REST endpoints on a 15-minute cron from GitHub Actions.

Auth: bot token (`DISCORD_BOT_TOKEN`). The bot must be present in the
server (invited via the OAuth URL with permissions=68672 = View Channel +
Send Messages + Read Message History + Add Reactions).

Used by `tasks/dispatch_approved.py`.
"""

from __future__ import annotations

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
    """

    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}"
    try:
        resp = httpx.get(url, headers=_headers(), timeout=DEFAULT_TIMEOUT)
    except httpx.HTTPError as e:
        raise DiscordError(f"discord: network error: {e}") from e

    if resp.status_code == 404:
        return {}
    if resp.status_code != 200:
        raise DiscordError(f"discord: HTTP {resp.status_code} on getMessage: {resp.text[:300]!r}")
    try:
        return resp.json()
    except ValueError as e:
        raise DiscordError(f"discord: non-JSON response on getMessage") from e


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
    ✅ in the channel triggers a post.
    """

    encoded = urllib.parse.quote(emoji, safe="")
    url = (
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}"
        f"/reactions/{encoded}?limit={max(1, min(100, limit))}"
    )
    try:
        resp = httpx.get(url, headers=_headers(), timeout=DEFAULT_TIMEOUT)
    except httpx.HTTPError as e:
        raise DiscordError(f"discord: network error on listReactions: {e}") from e

    if resp.status_code == 404:
        return []
    if resp.status_code != 200:
        raise DiscordError(
            f"discord: HTTP {resp.status_code} on listReactions: {resp.text[:300]!r}"
        )
    try:
        users = resp.json()
    except ValueError as e:
        raise DiscordError(f"discord: non-JSON response on listReactions") from e
    return users if isinstance(users, list) else []


def add_bot_reaction(channel_id: str, message_id: str, emoji: str) -> bool:
    """Add a reaction to a message as the bot.

    Used by the dispatch worker to mark a message as 'processed' so the next
    poll doesn't re-trigger. The bot adds a different emoji than the
    trigger one (e.g., bot adds 🚀 after posting; trigger was ✅).
    """

    encoded = urllib.parse.quote(emoji, safe="")
    url = (
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}"
        f"/reactions/{encoded}/@me"
    )
    try:
        resp = httpx.put(url, headers=_headers(), timeout=DEFAULT_TIMEOUT)
    except httpx.HTTPError as e:
        warn(f"discord: add_bot_reaction network error: {e}")
        return False

    if resp.status_code in (200, 204):
        return True
    warn(f"discord: add_bot_reaction HTTP {resp.status_code}: {resp.text[:200]!r}")
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
