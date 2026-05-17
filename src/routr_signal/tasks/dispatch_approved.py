"""Dispatch worker: poll Discord for approval reactions and post.

Runs on a 15-minute GitHub Actions cron. Stateless except for the SQLite
intel.db that the daily pipeline shares.

Flow per poll:
  1. Read recent runs that have discord_message_ids recorded.
  2. For each message on each run, check whether the user reacted with the
     approval emoji for that run's kind:
       - kind='daily':     ✅  -> post pending x_thread hook via Buffer
       - kind='synthesis': 📰  -> push synthesis draft to Beehiiv as draft
  3. After a successful dispatch, the bot adds 🚀 (processed marker) to the
     message so subsequent polls skip it.
  4. Failed dispatches mark the post row with status='failed' and the bot
     adds ❌ as a visible failure marker.

Idempotency: the 'processed' marker on the message means even if we crash
between Buffer/Beehiiv success and DB update, the next poll won't re-post.
We accept a small window of "Buffer posted but DB shows pending" if a crash
hits between the API call and the DB write.

Run locally: `python -m routr_signal.tasks.dispatch_approved`
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from ..lib import beehiiv_client, buffer_client, discord_inbox, signal_store
from ..lib.env import env, env_required
from ..lib.logging import error, info, warn


# Emoji conventions
# Either of these on a daily digest message approves the auto-postable hook
# (currently x_thread only) for posting to X via Buffer. ✅ is the explicit
# convention; 👍 is allowed because it's the universal "approve" gesture and
# nothing else competes for that meaning in this channel.
HOOK_APPROVAL_EMOJIS: tuple[str, ...] = ("✅", "👍")
HOOK_APPROVAL_EMOJI = HOOK_APPROVAL_EMOJIS[0]   # back-compat alias for tests / docs

SYNTHESIS_APPROVAL_EMOJI = "📰"      # user reacts: send synthesis to Beehiiv as draft
BOT_PROCESSED_MARKER = "🚀"          # bot reacts after successful dispatch
BOT_FAILED_MARKER = "❌"             # bot reacts after a failed dispatch

# How far back to look. Daily digests usually get reacted within 24h; we keep
# a 3-day window to cover weekends.
LOOKBACK_HOURS = 72


def run() -> int:
    info("=== dispatch_approved poll ===")

    channel_id = env("DISCORD_CHANNEL_ID")
    if not channel_id:
        warn("dispatch_approved: DISCORD_CHANNEL_ID not set; nothing to poll")
        return 0
    if not discord_inbox.is_configured():
        warn("dispatch_approved: Discord bot not configured; exiting")
        return 0

    since_iso = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).isoformat()
    runs = signal_store.runs_with_pending_messages(since_iso=since_iso, limit=50)
    info(f"dispatch_approved: checking {len(runs)} recent run(s)")

    processed = 0
    skipped = 0
    failed = 0
    for r in runs:
        kind = r.get("kind") or "daily"
        msg_ids = _parse_message_ids(r.get("discord_message_ids"))
        if not msg_ids:
            continue

        if kind == "daily":
            n_p, n_f, n_s = _handle_daily_run(channel_id, r, msg_ids)
        elif kind == "synthesis":
            n_p, n_f, n_s = _handle_synthesis_run(channel_id, r, msg_ids)
        else:
            warn(f"dispatch_approved: unknown run kind {kind!r}; skipping")
            continue
        processed += n_p
        failed += n_f
        skipped += n_s

    info(
        f"dispatch_approved: processed={processed} failed={failed} skipped={skipped}"
    )
    return 0 if failed == 0 else 2


# ---------------------------------------------------------------------------
# Daily run handler (x_thread -> Buffer -> X)
# ---------------------------------------------------------------------------


def _handle_daily_run(
    channel_id: str,
    run_row: dict[str, Any],
    msg_ids: list[str],
) -> tuple[int, int, int]:
    """Returns (processed, failed, skipped)."""

    run_id = run_row["id"]
    pending = signal_store.pending_posts_for_run(run_id)
    if not pending:
        return (0, 0, 0)

    if not buffer_client.is_configured():
        warn(f"dispatch_approved[daily/{run_id}]: Buffer not configured; cannot dispatch")
        return (0, 0, len(pending))

    # Skip if we've already processed this run (bot's marker present on any message).
    if any(
        discord_inbox.has_bot_marker_reaction(channel_id, mid, BOT_PROCESSED_MARKER)
        for mid in msg_ids
    ):
        return (0, 0, len(pending))

    # Approval = any of HOOK_APPROVAL_EMOJIS on any of the run's messages.
    if not any(
        discord_inbox.message_has_reaction(channel_id, mid, emoji)
        for emoji in HOOK_APPROVAL_EMOJIS
        for mid in msg_ids
    ):
        return (0, 0, len(pending))

    triggering_emoji = _find_triggering_emoji(channel_id, msg_ids, HOOK_APPROVAL_EMOJIS)

    info(
        f"dispatch_approved[daily/{run_id}]: approval ({triggering_emoji}) detected; "
        f"posting {len(pending)} pending hook(s)"
    )

    processed = 0
    failed = 0
    channel = env_required("BUFFER_X_CHANNEL_ID")
    for p in pending:
        if p["hook_format"] != "x_thread":
            # Today we only auto-dispatch x_thread. Other formats stay
            # untouched in the digest for the user to handle manually.
            continue
        try:
            created = buffer_client.create_post(
                channel_id=channel,
                text=p["text"],
                mode="shareNow",
                scheduling_type="automatic",
            )
        except buffer_client.BufferError as e:
            warn(f"dispatch_approved[daily/{run_id}]: Buffer failed for post {p['id']}: {e}")
            signal_store.update_post_status(
                p["id"],
                status="failed",
                error=str(e)[:500],
                discord_reaction=triggering_emoji,
                approved=True,
            )
            failed += 1
            continue

        signal_store.update_post_status(
            p["id"],
            status="posted",
            buffer_post_id=created.id,
            discord_reaction=triggering_emoji,
            approved=True,
            posted=True,
        )
        processed += 1
        info(
            f"dispatch_approved[daily/{run_id}]: posted hook {p['id']} via Buffer "
            f"buffer_post_id={created.id}"
        )

    # Mark the messages as processed (or failed) so future polls skip them.
    marker = BOT_PROCESSED_MARKER if failed == 0 else BOT_FAILED_MARKER
    for mid in msg_ids:
        discord_inbox.add_bot_reaction(channel_id, mid, marker)

    return (processed, failed, 0)


# ---------------------------------------------------------------------------
# Synthesis run handler (synthesis -> Beehiiv draft)
# ---------------------------------------------------------------------------


def _handle_synthesis_run(
    channel_id: str,
    run_row: dict[str, Any],
    msg_ids: list[str],
) -> tuple[int, int, int]:
    run_id = run_row["id"]
    pending = signal_store.pending_posts_for_run(run_id)
    if not pending:
        return (0, 0, 0)

    if not beehiiv_client.is_configured():
        warn(f"dispatch_approved[synthesis/{run_id}]: Beehiiv not configured; cannot dispatch")
        return (0, 0, len(pending))

    if any(
        discord_inbox.has_bot_marker_reaction(channel_id, mid, BOT_PROCESSED_MARKER)
        for mid in msg_ids
    ):
        return (0, 0, len(pending))

    if not any(
        discord_inbox.message_has_reaction(channel_id, mid, SYNTHESIS_APPROVAL_EMOJI)
        for mid in msg_ids
    ):
        return (0, 0, len(pending))

    info(
        f"dispatch_approved[synthesis/{run_id}]: approval detected; "
        f"sending {len(pending)} draft(s) to Beehiiv"
    )

    processed = 0
    failed = 0
    for p in pending:
        if p["platform"] != "beehiiv":
            continue
        title = _derive_title_from_synthesis(p)
        try:
            created = beehiiv_client.create_draft_post(
                title=title,
                body_markdown=p["text"],
            )
        except beehiiv_client.BeehiivError as e:
            warn(
                f"dispatch_approved[synthesis/{run_id}]: Beehiiv failed for post {p['id']}: {e}"
            )
            signal_store.update_post_status(
                p["id"],
                status="failed",
                error=str(e)[:500],
                discord_reaction=SYNTHESIS_APPROVAL_EMOJI,
                approved=True,
            )
            failed += 1
            continue

        signal_store.update_post_status(
            p["id"],
            status="posted",
            beehiiv_post_id=created.id,
            external_url=created.web_url,
            discord_reaction=SYNTHESIS_APPROVAL_EMOJI,
            approved=True,
            posted=True,
        )
        processed += 1
        info(
            f"dispatch_approved[synthesis/{run_id}]: created Beehiiv draft "
            f"id={created.id} url={created.web_url}"
        )

    marker = BOT_PROCESSED_MARKER if failed == 0 else BOT_FAILED_MARKER
    for mid in msg_ids:
        discord_inbox.add_bot_reaction(channel_id, mid, marker)

    return (processed, failed, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_message_ids(blob: Any) -> list[str]:
    if not blob:
        return []
    if isinstance(blob, list):
        return [m for m in blob if isinstance(m, str)]
    if isinstance(blob, str):
        try:
            import json as _json

            parsed = _json.loads(blob)
            return [m for m in parsed if isinstance(m, str)] if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


def _derive_title_from_synthesis(post_row: dict[str, Any]) -> str:
    """Pull a sensible title from the synthesis metadata, falling back to a date stamp."""

    import json as _json

    meta_raw = post_row.get("metadata") or "{}"
    if isinstance(meta_raw, str):
        try:
            meta = _json.loads(meta_raw)
        except ValueError:
            meta = {}
    elif isinstance(meta_raw, dict):
        meta = meta_raw
    else:
        meta = {}

    theme = meta.get("dominant_theme") or ""
    period = meta.get("period") or ""

    if theme:
        return _truncate(theme, 200)
    if period:
        return f"Weekly signal — {period}"
    return f"Weekly signal — {datetime.now(timezone.utc).date().isoformat()}"


def _truncate(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else (s[: n - 1].rstrip() + "...")



def _find_triggering_emoji(
    channel_id: str,
    msg_ids: list[str],
    candidates: tuple[str, ...],
) -> str:
    """Return whichever approval emoji is present on any of the run's messages.

    Used by `update_post_status(discord_reaction=...)` so we can audit which
    convention the user actually used. Falls back to the first candidate if
    no match is found (shouldn't happen because we only call this after
    confirming a reaction exists, but defensive).
    """

    for emoji in candidates:
        for mid in msg_ids:
            if discord_inbox.message_has_reaction(channel_id, mid, emoji):
                return emoji
    return candidates[0]


def cli() -> None:
    """Entry point for `routr-dispatch` console script."""

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
