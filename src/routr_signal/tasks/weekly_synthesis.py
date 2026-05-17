"""Weekly synthesis task.

Aggregates the last 7 days of relevant signals, asks the drafter to write a
400-500 word synthesis essay, posts the result to Discord for review, and
records a `pending` post row for Beehiiv newsletter publishing once the
user reacts to approve.

Trigger: `.github/workflows/weekly-synthesis.yml` (Sunday 14:00 UTC by default)
or manual `python -m routr_signal.tasks.weekly_synthesis`.

Output flow:
  1. signal_store.open_run(kind='synthesis')
  2. classify.synthesize.synthesize() -> SynthesisResult
  3. Post structured payload to Discord webhook (2 messages: the draft, then
     the metadata block).
  4. signal_store.record_run_discord_messages(run_id, [msg_ids])
  5. signal_store.insert_post(kind='synthesis', platform='beehiiv',
     status='pending', discord_message_id=...)
  6. signal_store.close_run(...)

The dispatch worker (tasks/dispatch_approved.py) handles the reaction
polling and Beehiiv API call separately.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from typing import Any

import httpx

from ..classify import synthesize as synthesize_mod
from ..lib import signal_store
from ..lib.env import env, env_required
from ..lib.logging import error, info, warn


# Approval emoji that the dispatch worker watches for on the synthesis message.
SYNTHESIS_APPROVAL_EMOJI_FOR_NEWSLETTER = "📰"
SYNTHESIS_APPROVAL_DESCRIPTION = (
    "React 📰 on this message to publish to Beehiiv as a newsletter draft. "
    "LinkedIn and X stay manual."
)


def run() -> int:
    info("=== weekly synthesis ===")

    result = synthesize_mod.synthesize()
    if result is None:
        info("synthesize: no synthesis produced (insufficient signal); exit 0")
        return 0

    run_id = f"synthesis-{result.period.replace('..', '_to_')}-{uuid.uuid4().hex[:6]}"
    signal_store.open_run(run_id, kind="synthesis")

    # Post to Discord (2 messages: draft + metadata).
    msg_ids = _post_synthesis(result)
    if not msg_ids:
        warn("weekly_synthesis: no Discord messages posted; closing run as failed")
        signal_store.close_run(
            run_id,
            status="failed",
            source_counts={},
            cosine_kept={},
            classifier_relevant={},
            notes=["discord post failed"],
            digest_md=result.draft_post,
            hooks=None,
        )
        return 1

    signal_store.record_run_discord_messages(run_id, msg_ids)

    # Pre-create a 'pending' Beehiiv post; the dispatch worker promotes on 📰 reaction.
    post_id = f"post-{uuid.uuid4().hex[:12]}"
    signal_store.insert_post(
        post_id=post_id,
        kind="synthesis",
        platform="beehiiv",
        text=result.draft_post,
        status="pending",
        run_id=run_id,
        discord_message_id=msg_ids[0],   # synthesis lives in the FIRST message
        metadata={
            "period": result.period,
            "dominant_theme": result.dominant_theme,
            "approval_emoji": SYNTHESIS_APPROVAL_EMOJI_FOR_NEWSLETTER,
            "source_message_ids": msg_ids,
        },
    )
    info(f"weekly_synthesis: pending Beehiiv post {post_id} for run {run_id}")

    signal_store.close_run(
        run_id,
        status="success",
        source_counts={},
        cosine_kept={},
        classifier_relevant={},
        notes=[
            f"weekly synthesis for {result.period}",
            f"dominant theme: {result.dominant_theme}",
            SYNTHESIS_APPROVAL_DESCRIPTION,
        ],
        digest_md=_render_synthesis_md(result),
        hooks=[{"format": "synthesis", "anchor_signal_id": None, "text": result.draft_post}],
    )

    info("=== weekly synthesis complete ===")
    return 0


def _render_synthesis_md(result: synthesize_mod.SynthesisResult) -> str:
    """Render the synthesis as Markdown for archival on the runs row."""

    lines: list[str] = []
    lines.append(f"# Weekly synthesis — {result.period}\n")
    lines.append(f"**Dominant theme:** {result.dominant_theme}\n")
    lines.append("\n## Contrarian read\n")
    lines.append(result.contrarian_read + "\n")
    lines.append("\n## Where this goes\n")
    lines.append(result.where_this_goes + "\n")
    lines.append("\n## Draft post\n")
    lines.append(result.draft_post + "\n")
    if result.routr_bridge:
        lines.append("\n## Optional Routr bridge\n")
        lines.append(result.routr_bridge + "\n")
    if result.evidence:
        lines.append("\n## Evidence (top signals)\n")
        for e in result.evidence:
            lines.append(f"- `{e['signal_id']}` — {e['what_it_shows']}\n")
    if result.topic_distribution:
        lines.append("\n## Topic distribution\n")
        for t, n in result.topic_distribution.items():
            lines.append(f"- `{t}`: {n}\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# Discord posting (minimal, embed-based)
# ---------------------------------------------------------------------------


def _post_synthesis(result: synthesize_mod.SynthesisResult) -> list[str]:
    """Post the synthesis to Discord. Returns the list of message IDs that landed."""

    raw_url = env("DISCORD_WEBHOOK_URL")
    if not raw_url:
        warn("weekly_synthesis: DISCORD_WEBHOOK_URL not set; skipping post")
        return []

    url = raw_url.rstrip("/")
    if url.endswith("/slack"):
        url = url[: -len("/slack")]
    if url.endswith("/github"):
        url = url[: -len("/github")]

    # Message 1: header + the draft post (the reactable content).
    msg1_content = f"📝 **Weekly synthesis — {result.period}**\n_{SYNTHESIS_APPROVAL_DESCRIPTION}_"
    msg1_embeds: list[dict[str, Any]] = [
        {
            "title": "Dominant theme",
            "description": result.dominant_theme[:4000],
            "color": 0x10B981,
        },
        {
            "title": "Draft post (400-500 words)",
            "description": result.draft_post[:4000],
            "color": 0x3B82F6,
        },
    ]
    msg1 = {
        "username": "Routr Synthesis",
        "allowed_mentions": {"parse": []},
        "content": msg1_content[:1900],
        "embeds": msg1_embeds,
    }

    # Message 2: structured reasoning (lower priority; user references when editing).
    fields: list[dict[str, Any]] = [
        {"name": "Contrarian read", "value": result.contrarian_read[:1000], "inline": False},
        {"name": "Where this goes (60-90d)", "value": result.where_this_goes[:1000], "inline": False},
    ]
    if result.routr_bridge:
        fields.append({"name": "Optional Routr bridge", "value": result.routr_bridge[:1000], "inline": False})
    if result.evidence:
        ev_lines = [f"• `{e['signal_id']}` — {e['what_it_shows']}" for e in result.evidence[:8]]
        fields.append({"name": "Evidence", "value": "\n".join(ev_lines)[:1000], "inline": False})
    if result.topic_distribution:
        topic_lines = [f"`{k}`: {v}" for k, v in list(result.topic_distribution.items())[:10]]
        fields.append({"name": "Topic distribution", "value": " · ".join(topic_lines)[:1000], "inline": False})

    msg2 = {
        "username": "Routr Synthesis",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": "Reasoning & supporting data",
                "color": 0x8B5CF6,
                "fields": fields,
            }
        ],
    }

    posted: list[str] = []
    for idx, payload in enumerate((msg1, msg2)):
        if idx > 0:
            time.sleep(0.5)
        mid = _post_one(url, payload)
        if mid:
            posted.append(mid)
    info(f"weekly_synthesis: {len(posted)}/2 message(s) posted ids={posted}")
    return posted


def _post_one(url: str, payload: dict[str, Any]) -> str | None:
    post_url = f"{url}?wait=true" if "?" not in url else url
    for attempt in range(3):
        try:
            resp = httpx.post(post_url, json=payload, timeout=20.0)
        except httpx.HTTPError as e:
            warn(f"weekly_synthesis: POST failed: {e}")
            return None
        if resp.status_code == 200:
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                return None
            mid = body.get("id") if isinstance(body, dict) else None
            return mid if isinstance(mid, str) else None
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "1"))
            warn(f"weekly_synthesis: rate-limited; sleeping {retry_after:.1f}s (attempt {attempt + 1}/3)")
            time.sleep(retry_after + 0.1)
            continue
        warn(f"weekly_synthesis: webhook returned {resp.status_code}: {resp.text[:300]!r}")
        return None
    return None


def cli() -> None:
    """Entry point for `routr-synthesize` console script."""

    try:
        sys.exit(run())
    except KeyboardInterrupt:
        error("interrupted")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        error(f"unhandled error: {e}")
        raise


if __name__ == "__main__":
    cli()
