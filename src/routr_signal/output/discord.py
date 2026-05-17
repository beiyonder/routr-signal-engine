"""Discord webhook output using Discord's NATIVE embed format.

Why not Slack-compat (`/slack` suffix):
    Discord's `/slack` endpoint accepts only the *legacy* Slack `attachments` format,
    NOT modern Block Kit (`blocks`). When we send `blocks`, Discord silently drops
    everything except the top-level `text` field — which is exactly the empty-looking
    message we were seeing.

What this module does instead:
    1. Strip any `/slack` suffix from the configured webhook URL so we hit the native
       Discord webhook endpoint.
    2. Build a multi-embed Discord payload (up to 10 embeds, 6000 chars body budget).
    3. Send in 1-2 messages depending on payload size, with a 0.5s pause between
       messages to stay under Discord's 5-req/2s rate limit.

Discord embed limits we respect:
    - 10 embeds per message
    - 6000 chars total across all embeds in one message
    - title: 256 chars, description: 4096, field name: 256, field value: 1024
    - 25 fields per embed
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..lib.env import env
from ..lib.logging import info, warn
from ..lib.types import ClassifiedItem, Digest, Lead, PostHook


# Discord embed limits
MAX_EMBEDS_PER_MESSAGE = 10
MAX_TOTAL_EMBED_CHARS = 5800  # leave 200 chars of headroom under the 6000 cap
MAX_TITLE = 250
MAX_DESCRIPTION = 4000
MAX_FIELD_NAME = 250
MAX_FIELD_VALUE = 1000
MAX_CONTENT = 1900

# Color palette (decimal, not hex)
COLOR_HEADER = 0x3B82F6   # blue-500
COLOR_SIGNAL = 0xEF4444   # red-500
COLOR_HOOKS = 0x10B981    # emerald-500
COLOR_LEADS = 0x8B5CF6    # violet-500

SOURCE_BADGES = {
    "hn": "HN",
    "reddit": "Reddit",
    "github": "GitHub",
    "devto": "Dev.to",
    "hf": "HF",
    "newsletter": "Newsletter",
    "x": "X",
    "other": "Other",
}

WEDGE_LABELS = {
    "cold_start": "cold-start",
    "markup": "0% markup",
    "self_host": "self-host",
    "mcp": "MCP",
    "reliability": "reliability",
    "other": "other",
}

HOOK_LABELS = {
    "x_thread": "X thread opener",
    "linkedin": "LinkedIn opener",
    "reddit": "Reddit post title",
    "hn_comment": "HN comment seed",
    "devto_title": "Dev.to title",
}


def publish(digest: Digest) -> list[str]:
    """POST the digest to $DISCORD_WEBHOOK_URL. Returns the list of Discord
    message IDs that landed (empty list = no message posted).

    The IDs are persisted on the runs row so the dispatch worker can poll
    them for reactions later.
    """

    raw_url = env("DISCORD_WEBHOOK_URL")
    if not raw_url:
        info("discord: DISCORD_WEBHOOK_URL not set, skipping.")
        return []

    url = _normalize_url(raw_url)
    messages = _build_messages(digest)

    if not messages:
        info("discord: nothing to send (no signals, hooks, or leads).")
        return []

    posted_ids: list[str] = []
    for i, payload in enumerate(messages):
        if i > 0:
            # Discord rate limit: 5 req per 2s per webhook. 0.5s pause is plenty.
            time.sleep(0.5)
        msg_id = _post_one(url, payload, message_index=i + 1, total=len(messages))
        if msg_id:
            posted_ids.append(msg_id)

    info(f"discord: {len(posted_ids)}/{len(messages)} message(s) posted ids={posted_ids}")
    return posted_ids


# -----------------------------------------------------------------------------
# URL normalization
# -----------------------------------------------------------------------------

def _normalize_url(url: str) -> str:
    """Strip a `/slack` suffix so we POST to the native Discord webhook endpoint."""

    cleaned = url.rstrip("/")
    if cleaned.endswith("/slack"):
        cleaned = cleaned[: -len("/slack")]
    if cleaned.endswith("/github"):
        cleaned = cleaned[: -len("/github")]
    return cleaned


# -----------------------------------------------------------------------------
# Payload construction
# -----------------------------------------------------------------------------

def _build_messages(digest: Digest) -> list[dict[str, Any]]:
    """Return one or two Discord webhook payloads carrying the full digest."""

    msg1_content = _header_content(digest)

    signal_embeds = [_signal_embed(i, c) for i, c in enumerate(digest.pain_signals, start=1)]
    hooks_embed = _hooks_embed(digest.hooks) if digest.hooks else None
    leads_embed = _leads_embed(digest.active_accounts) if digest.active_accounts else None
    notes_embed = _notes_embed(digest) if digest.notes else None

    # Try to fit everything in one message; otherwise split.
    first_msg_embeds: list[dict[str, Any]] = []
    if notes_embed is not None:
        first_msg_embeds.append(notes_embed)
    first_msg_embeds.extend(signal_embeds)

    second_msg_embeds: list[dict[str, Any]] = []
    if hooks_embed is not None:
        second_msg_embeds.append(hooks_embed)
    if leads_embed is not None:
        second_msg_embeds.append(leads_embed)

    # If first message has space and second is small, merge.
    merged_size = _embeds_size(first_msg_embeds + second_msg_embeds)
    merged_count = len(first_msg_embeds) + len(second_msg_embeds)
    if merged_count <= MAX_EMBEDS_PER_MESSAGE and merged_size <= MAX_TOTAL_EMBED_CHARS:
        return [_message(msg1_content, first_msg_embeds + second_msg_embeds)]

    messages: list[dict[str, Any]] = []
    if first_msg_embeds:
        messages.append(_message(msg1_content, first_msg_embeds))
    if second_msg_embeds:
        # Second message gets minimal content (we don't want duplicate header).
        messages.append(_message("", second_msg_embeds))

    # Edge case: nothing to send. Still post the header so the user knows the
    # workflow ran and produced an empty-but-real digest.
    if not messages and msg1_content:
        messages.append(_message(msg1_content, []))

    return messages


def _message(content: str, embeds: list[dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "username": "Routr Signal",
        "allowed_mentions": {"parse": []},
    }
    if content:
        payload["content"] = content[:MAX_CONTENT]
    if embeds:
        payload["embeds"] = embeds
    return payload


# -----------------------------------------------------------------------------
# Embed builders
# -----------------------------------------------------------------------------

def _header_content(digest: Digest) -> str:
    """The plain-text content above the embeds. Acts as the notification preview."""

    parts = [f"**📊 Routr daily signal — {digest.date}**"]
    if digest.source_counts:
        bits = " · ".join(f"{k} {v}" for k, v in digest.source_counts.items())
        parts.append(f"Source counts: {bits}")
    return "\n".join(parts)


def _notes_embed(digest: Digest) -> dict[str, Any] | None:
    if not digest.notes:
        return None
    text = "\n".join(f"• {n}" for n in digest.notes)
    return {
        "title": "Notes",
        "description": _truncate(text, MAX_DESCRIPTION),
        "color": COLOR_HEADER,
    }


def _signal_embed(idx: int, c: ClassifiedItem) -> dict[str, Any]:
    badge = SOURCE_BADGES.get(c.raw.source, c.raw.source)
    topics = c.raw.extra.get("topics") or []
    topic_str = ", ".join(topics[:3]) if topics else WEDGE_LABELS.get(c.wedge, c.wedge)
    title = (
        f"{idx}. [{badge}] {topic_str} · "
        f"llm {c.score:.2f} cos {c.cosine_score:.2f}"
    )
    description_lines: list[str] = []
    headline = c.raw.title.strip() or "(no title)"
    description_lines.append(f"**{_md_escape(headline)}**")
    if c.raw.author:
        description_lines.append(f"by `@{c.raw.author}`")
    description = "\n".join(description_lines)

    fields: list[dict[str, Any]] = []
    snapshot = c.raw.extra.get("person_snapshot")
    if snapshot and snapshot != "unknown":
        fields.append(
            {
                "name": "Who",
                "value": _truncate(snapshot, MAX_FIELD_VALUE),
                "inline": False,
            }
        )
    if c.pain_summary:
        fields.append(
            {
                "name": "Pain",
                "value": _truncate(c.pain_summary, MAX_FIELD_VALUE),
                "inline": False,
            }
        )
    if c.suggested_angle:
        fields.append(
            {
                "name": "Angle",
                "value": _truncate(c.suggested_angle, MAX_FIELD_VALUE),
                "inline": False,
            }
        )

    embed: dict[str, Any] = {
        "title": _truncate(title, MAX_TITLE),
        "url": c.raw.url,
        "description": _truncate(description, MAX_DESCRIPTION),
        "color": COLOR_SIGNAL,
        "fields": fields,
    }
    return embed


def _hooks_embed(hooks: list[PostHook]) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    for hook in hooks[:5]:
        label = HOOK_LABELS.get(hook.format, hook.format)
        anchor = f" · anchor `{hook.anchor_signal_id}`" if hook.anchor_signal_id else ""
        fields.append(
            {
                "name": _truncate(f"✍️ {label}{anchor}", MAX_FIELD_NAME),
                "value": _truncate(hook.text, MAX_FIELD_VALUE),
                "inline": False,
            }
        )
    return {
        "title": "Pre-drafted post hooks",
        "color": COLOR_HOOKS,
        "fields": fields,
    }


def _leads_embed(leads: list[Lead]) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    for lead in leads[:10]:
        link = f"[@{lead.handle}]({lead.profile_url})" if lead.profile_url else f"@{lead.handle}"
        body = (
            f"_{_truncate(lead.pain_in_their_words, 400)}_\n"
            f"**Angle:** {_truncate(lead.pitch_angle, 500)}"
        )
        fields.append(
            {
                "name": _truncate(f"📈 {lead.platform} · {link}", MAX_FIELD_NAME),
                "value": _truncate(body, MAX_FIELD_VALUE),
                "inline": False,
            }
        )
    return {
        "title": "Active accounts (engage early)",
        "color": COLOR_LEADS,
        "fields": fields,
    }


# -----------------------------------------------------------------------------
# Posting
# -----------------------------------------------------------------------------

def _post_one(url: str, payload: dict[str, Any], *, message_index: int, total: int) -> str | None:
    """Send one webhook message with retries for 429.

    Uses `?wait=true` so Discord returns the created message JSON; we extract
    `message.id` for the dispatch worker to later poll for reactions.

    Returns the message id on success, None on failure.
    """

    # `?wait=true` forces Discord to wait for the message to commit and return its
    # full body (including id). Without it, the response is 204 No Content and we
    # have no way to know which message we just sent.
    post_url = url if "?" in url else f"{url}?wait=true"

    for attempt in range(3):
        try:
            resp = httpx.post(post_url, json=payload, timeout=20.0)
        except httpx.HTTPError as e:
            warn(f"discord: POST failed ({message_index}/{total}): {e}")
            return None

        if resp.status_code in (200, 204):
            if resp.status_code == 204:
                # We asked for wait=true; 204 means the URL got cleaned of the
                # query string somewhere. Best-effort fallback: return None so
                # the run still records nothing rather than a fake id.
                warn(
                    f"discord: 204 returned for message {message_index}/{total} "
                    "despite wait=true; message id unknown"
                )
                return None
            try:
                body = resp.json()
            except Exception as e:  # noqa: BLE001
                warn(f"discord: response not JSON for message {message_index}/{total}: {e}")
                return None
            msg_id = body.get("id") if isinstance(body, dict) else None
            if not isinstance(msg_id, str):
                warn(
                    f"discord: response body missing string id for message "
                    f"{message_index}/{total}: {body!r}"
                )
                return None
            return msg_id

        if resp.status_code == 429:
            # Honor Retry-After.
            retry_after = float(resp.headers.get("Retry-After", "1"))
            warn(
                f"discord: rate-limited ({message_index}/{total}), "
                f"waiting {retry_after:.1f}s (attempt {attempt + 1}/3)"
            )
            time.sleep(retry_after + 0.1)
            continue

        warn(
            f"discord: webhook returned {resp.status_code} for message {message_index}/{total}: "
            f"{resp.text[:300]!r}"
        )
        return None

    warn(f"discord: gave up on message {message_index}/{total} after 3 rate-limit retries")
    return None


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _md_escape(s: str) -> str:
    # Light markdown escape for Discord. We don't want stray ** to break formatting.
    return s.replace("*", r"\*").replace("_", r"\_").replace("~", r"\~").replace("`", r"\`")


def _embeds_size(embeds: list[dict[str, Any]]) -> int:
    """Approximate total char count to predict Discord's 6000-char rejection."""

    total = 0
    for e in embeds:
        total += len(e.get("title", ""))
        total += len(e.get("description", ""))
        for f in e.get("fields", []) or []:
            total += len(f.get("name", ""))
            total += len(f.get("value", ""))
        footer = e.get("footer")
        if isinstance(footer, dict):
            total += len(footer.get("text", ""))
    return total
