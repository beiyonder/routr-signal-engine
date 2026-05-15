"""Slack webhook output — Block Kit payload."""

from __future__ import annotations

from typing import Any

import httpx

from ..lib.env import env
from ..lib.logging import info, warn
from ..lib.types import Digest


MAX_SIGNALS_IN_SLACK = 5
MAX_LEADS_IN_SLACK = 5
SLACK_TEXT_LIMIT = 2900  # under the 3000-char block text cap


def publish(digest: Digest) -> bool:
    """POST the digest to $SLACK_WEBHOOK_URL. Returns True on success."""

    url = env("SLACK_WEBHOOK_URL")
    if not url:
        info("slack: SLACK_WEBHOOK_URL not set, skipping.")
        return False

    payload = _build_payload(digest)
    try:
        resp = httpx.post(url, json=payload, timeout=15.0)
    except httpx.HTTPError as e:
        warn(f"slack: POST failed: {e}")
        return False

    if resp.status_code >= 300 or resp.text.strip() != "ok":
        warn(f"slack: webhook returned {resp.status_code} {resp.text!r}")
        return False
    info("slack: digest posted")
    return True


def _build_payload(digest: Digest) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []

    blocks.append(
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Routr daily signal — {digest.date}"},
        }
    )

    if digest.source_counts:
        counts_line = " · ".join(f"*{k}* {v}" for k, v in digest.source_counts.items())
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": counts_line}]})

    if digest.notes:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "Notes: " + "; ".join(digest.notes)}],
            }
        )

    # Pain signals
    blocks.append({"type": "divider"})
    blocks.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": "*🔴 Top pain signals*"}}
    )
    if not digest.pain_signals:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "_No relevant signals today._"}}
        )
    else:
        for i, c in enumerate(digest.pain_signals[:MAX_SIGNALS_IN_SLACK], start=1):
            lines = [
                f"*{i}. [{c.raw.source.upper()}]* <{c.raw.url}|{_truncate(c.raw.title, 120)}>",
                f"  · _{c.wedge}_  · score `{c.score:.2f}`",
            ]
            if c.pain_summary:
                lines.append(f"  · _Pain:_ {_truncate(c.pain_summary, 200)}")
            if c.suggested_angle:
                lines.append(f"  · _Angle:_ {_truncate(c.suggested_angle, 240)}")
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": _truncate("\n".join(lines), SLACK_TEXT_LIMIT)},
                }
            )

    # Leads
    blocks.append({"type": "divider"})
    blocks.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": "*📈 Active accounts*"}}
    )
    if not digest.active_accounts:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "_No new leads today._"}}
        )
    else:
        for lead in digest.active_accounts[:MAX_LEADS_IN_SLACK]:
            link = f"<{lead.profile_url}|@{lead.handle}>" if lead.profile_url else f"@{lead.handle}"
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": _truncate(
                            f"*{lead.platform}* — {link}\n"
                            f"  · _Pain:_ {_truncate(lead.pain_in_their_words, 200)}\n"
                            f"  · _Angle:_ {_truncate(lead.pitch_angle, 240)}",
                            SLACK_TEXT_LIMIT,
                        ),
                    },
                }
            )

    # Hooks
    blocks.append({"type": "divider"})
    blocks.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": "*✍️ Post hooks*"}}
    )
    if not digest.hooks:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "_No drafts produced._"}}
        )
    else:
        for hook in digest.hooks:
            label = _hook_label(hook.format)
            anchor = f" _(anchor: `{hook.anchor_signal_id}`)_" if hook.anchor_signal_id else ""
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": _truncate(
                            f"*{label}*{anchor}\n>{_blockquote(hook.text)}",
                            SLACK_TEXT_LIMIT,
                        ),
                    },
                }
            )

    return {
        "text": f"Routr daily signal — {digest.date}",  # fallback for notifications
        "blocks": blocks,
    }


def _hook_label(fmt: str) -> str:
    return {
        "x_thread": "X thread opener",
        "linkedin": "LinkedIn opener",
        "reddit": "Reddit post title",
        "hn_comment": "HN comment seed",
        "devto_title": "Dev.to title",
    }.get(fmt, fmt)


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _blockquote(text: str) -> str:
    """Format multi-line text as Slack-style blockquote."""

    lines = text.strip().splitlines() or [""]
    return "\n>".join(line.strip() for line in lines)
