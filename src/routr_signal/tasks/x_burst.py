"""X-burst task: draft N standalone X posts and route them by length.

Runs on its own GitHub Actions cron (twice a day; see `pipeline.yml`).
Independent of the daily-digest flow:

  - Reads the most-recent classified signals from intel.db (last 48h).
  - Computes 7-day topic frequency (same helper the daily drafter uses).
  - Asks the drafter for N standalone X posts via
    `classify/x_burst_drafter.py` + `config/prompts/x_burst.md`.
  - Voice-lints each draft (length cap 25,000; URL check; usual rules)
    and DROPS any draft that fails. No Discord review, voice_lint is
    the safety net.
  - Routes each surviving post by length:
        text length <= 270 chars   -> Buffer `createPost(shareNow)` to X
        text length 271 ..  25,000 -> Discord DM to the operator (copy-paste flow)
        text length >   25,000     -> dropped (X Premium hard cap)
  - Records each post in the `posts` table with platform='x',
    kind='x_burst', metadata['ship_method'] in {'buffer', 'discord_dm'}.

Why the split routing: Buffer's GraphQL `createPost` mutation enforces
X's legacy 280-char cap on this account regardless of the user's X
Premium status (observed 2026-05-20: `UnexpectedError: Invalid post:
Twitter / X posts cannot exceed 280 characters`). Until Buffer fixes
their X-Premium detection, the long-form posts (271-25k) go to the
operator's Discord DM in a copy-paste-ready code block.

What this task does NOT do:
  - Does not fetch from sources (the daily pipeline does that).
  - Does not classify (the daily pipeline does that).
  - Does not write to the Discord digest channel (the daily pipeline does that).
  - Does not require the 📤 reaction.

Run locally:
    routr-burst --count=2 --dry-run

Environment knobs (all optional):
    ROUTR_X_BURST_COUNT       int, posts to draft per call (default 2)
    ROUTR_X_BURST_WINDOW_H    int, signal window in hours (default 48)
    ROUTR_X_BURST_MIN_SCORE   float, min combined_score (default 0.55)
    ROUTR_X_BURST_DRY_RUN     "1" to draft + lint but skip publish + DM
    DISCORD_USER_DM_ID        Discord user id of the operator (recipient
                              of the long-form DM); REQUIRED for any post
                              over 270 chars to ship anywhere.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from typing import Any

from ..classify import voice_lint
from ..classify.x_burst_drafter import draft_x_burst
from ..lib import buffer_client, discord_inbox, signal_store
from ..lib.env import env, env_flag, env_required
from ..lib.logging import error, info, warn


DEFAULT_COUNT = 2
DEFAULT_WINDOW_HOURS = 48
DEFAULT_MIN_SCORE = 0.55
DEFAULT_SIGNAL_LIMIT = 20


def _run_id() -> str:
    """Use the GitHub Actions run id when available, else a local uuid4.

    Multiple bursts may share a GITHUB_RUN_ID (morning + afternoon both
    fire under the same workflow file). To keep `runs` rows unique we
    append a short random suffix so the morning and afternoon bursts
    don't INSERT-OR-REPLACE each other.
    """

    gh = env("GITHUB_RUN_ID")
    suffix = uuid.uuid4().hex[:6]
    if gh:
        return f"{gh}-burst-{suffix}"
    return f"local-burst-{suffix}"


def run(
    *,
    count: int = DEFAULT_COUNT,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    min_score: float = DEFAULT_MIN_SCORE,
    dry_run: bool = False,
) -> int:
    run_id = _run_id()
    signal_store.open_run(run_id, kind="x_burst")
    info(
        f"=== routr-burst run {run_id} "
        f"(count={count} window_h={window_hours} min_score={min_score} dry_run={dry_run}) ==="
    )

    buffer_ok = buffer_client.is_configured()
    dm_user_id = env("DISCORD_USER_DM_ID") or ""
    dm_ok = bool(dm_user_id) and discord_inbox.is_configured()

    if not dry_run and not (buffer_ok or dm_ok):
        warn(
            "routr-burst: neither Buffer nor Discord DM is configured. "
            "Need at least one. Exiting clean."
        )
        signal_store.close_run(
            run_id,
            status="skipped",
            source_counts={},
            cosine_kept={},
            classifier_relevant={},
            notes=["no ship method configured"],
            digest_md=None,
            hooks=None,
        )
        return 0
    if not buffer_ok:
        warn("routr-burst: Buffer not configured; short posts (<=270) will be dropped this run")
    if not dm_ok:
        warn("routr-burst: Discord DM not configured; long posts (>270) will be dropped this run")

    # 1. Recent classified signals.
    signals = signal_store.recent_classified_for_drafting(
        window_hours=window_hours,
        min_score=min_score,
        limit=DEFAULT_SIGNAL_LIMIT,
    )
    if not signals:
        warn("routr-burst: no recent classified signals in window; nothing to draft.")
        signal_store.close_run(
            run_id,
            status="skipped",
            source_counts={},
            cosine_kept={},
            classifier_relevant={},
            notes=["no recent classified signals"],
            digest_md=None,
            hooks=None,
        )
        return 0
    info(f"routr-burst: drafting against {len(signals)} recent classified signals")

    # 2. Topic frequency.
    topic_freq = signal_store.topic_frequency(window_days=7)
    if topic_freq:
        top_six = list(topic_freq.items())[:6]
        info(
            "routr-burst: 7d topic frequency (top 6): "
            + ", ".join(f"{t} x{n}" for t, n in top_six)
        )

    # 3. Signals already drafted-against today.
    excluded = signal_store.signal_ids_posted_today(kind="x_burst", platform="x")
    if excluded:
        info(f"routr-burst: excluding {len(excluded)} signal id(s) already drafted today")

    # 4. Draft.
    posts = draft_x_burst(
        signals,
        count=count,
        topic_frequency=topic_freq,
        excluded_signal_ids=excluded,
    )
    if not posts:
        warn("routr-burst: drafter returned no usable posts; exiting clean.")
        signal_store.close_run(
            run_id,
            status="empty",
            source_counts={},
            cosine_kept={},
            classifier_relevant={},
            notes=["drafter returned no usable posts"],
            digest_md=None,
            hooks=None,
        )
        return 0

    # 5. Voice-lint. Drop any post that fails.
    lint_results = voice_lint.lint_x_burst_all(posts)
    clean: list[Any] = []
    lint_notes: list[str] = []
    for r in lint_results:
        if r.violations:
            warn(
                f"routr-burst: dropping draft due to lint: {r.violations} "
                f":: {r.hook.text[:140]!r}"
            )
            lint_notes.append(f"dropped: {', '.join(r.violations)}")
            continue
        clean.append(r.hook)
    if not clean:
        warn("routr-burst: every draft failed lint; shipping nothing this burst.")
        signal_store.close_run(
            run_id,
            status="lint_blocked",
            source_counts={},
            cosine_kept={},
            classifier_relevant={},
            notes=lint_notes,
            digest_md=None,
            hooks=[h.to_dict() for h in posts],
        )
        return 0

    info(f"routr-burst: {len(clean)} of {len(posts)} drafts passed lint; routing by length")

    # Categorize.
    auto_cap = voice_lint.X_BURST_AUTO_SHIP_CAP
    short_posts = [h for h in clean if len(h.text) <= auto_cap]
    long_posts = [h for h in clean if len(h.text) > auto_cap]
    info(
        f"routr-burst: routing: {len(short_posts)} <={auto_cap}ch -> Buffer auto; "
        f"{len(long_posts)} >{auto_cap}ch -> Discord DM"
    )

    if dry_run:
        info("routr-burst: dry-run, skipping all publish + DM")
        _record_dry_run(run_id, short_posts, ship_method="buffer")
        _record_dry_run(run_id, long_posts, ship_method="discord_dm")
        signal_store.close_run(
            run_id,
            status="dry_run",
            source_counts={},
            cosine_kept={},
            classifier_relevant={},
            notes=lint_notes
            + [
                f"dry_run: would ship {len(short_posts)} short via Buffer, "
                f"{len(long_posts)} long via Discord DM"
            ],
            digest_md=None,
            hooks=[h.to_dict() for h in clean],
        )
        return 0

    # 6. Buffer-ship short posts.
    buffer_shipped = 0
    buffer_failed = 0
    if short_posts:
        if not buffer_ok:
            warn(f"routr-burst: dropping {len(short_posts)} short post(s) (Buffer not configured)")
        else:
            channel = env_required("BUFFER_X_CHANNEL_ID")
            for hook in short_posts:
                post_id = f"post-{uuid.uuid4().hex[:12]}"
                signal_store.insert_post(
                    post_id=post_id,
                    kind="x_burst",
                    platform="x",
                    text=hook.text,
                    status="shipping",
                    run_id=run_id,
                    hook_format="x_thread",
                    signal_id=hook.anchor_signal_id,
                    metadata={"ship_method": "buffer", "length": len(hook.text)},
                )
                try:
                    created = buffer_client.create_post(
                        channel_id=channel,
                        text=hook.text,
                        mode="shareNow",
                        scheduling_type="automatic",
                    )
                except buffer_client.BufferError as e:
                    warn(f"routr-burst: Buffer failed for {post_id}: {e}")
                    signal_store.update_post_status(
                        post_id,
                        status="failed",
                        error=str(e)[:500],
                    )
                    buffer_failed += 1
                    continue
                signal_store.update_post_status(
                    post_id,
                    status="posted",
                    buffer_post_id=created.id,
                    posted=True,
                )
                info(
                    f"routr-burst: Buffer ok post={post_id} buffer_post_id={created.id} "
                    f"signal={hook.anchor_signal_id} len={len(hook.text)}"
                )
                buffer_shipped += 1

    # 7. DM long posts.
    dm_sent = 0
    dm_failed = 0
    if long_posts:
        if not dm_ok:
            warn(f"routr-burst: dropping {len(long_posts)} long post(s) (Discord DM not configured)")
        else:
            for hook in long_posts:
                post_id = f"post-{uuid.uuid4().hex[:12]}"
                signal_store.insert_post(
                    post_id=post_id,
                    kind="x_burst",
                    platform="x",
                    text=hook.text,
                    status="pending_manual",
                    run_id=run_id,
                    hook_format="x_thread",
                    signal_id=hook.anchor_signal_id,
                    metadata={"ship_method": "discord_dm", "length": len(hook.text)},
                )
                header = _build_dm_header(hook, post_id, signals)
                msg_ids = discord_inbox.send_dm(dm_user_id, hook.text, header=header)
                if not msg_ids:
                    warn(f"routr-burst: Discord DM failed for {post_id}")
                    signal_store.update_post_status(
                        post_id,
                        status="failed",
                        error="Discord DM send failed",
                    )
                    dm_failed += 1
                    continue
                signal_store.update_post_status(
                    post_id,
                    status="awaiting_manual_post",
                )
                info(
                    f"routr-burst: DM ok post={post_id} msgs={len(msg_ids)} "
                    f"signal={hook.anchor_signal_id} len={len(hook.text)}"
                )
                dm_sent += 1

    posted_total = buffer_shipped + dm_sent
    failed_total = buffer_failed + dm_failed
    notes = list(lint_notes)
    notes.append(
        f"buffer_shipped={buffer_shipped} buffer_failed={buffer_failed} "
        f"dm_sent={dm_sent} dm_failed={dm_failed}"
    )
    signal_store.close_run(
        run_id,
        status="success" if failed_total == 0 else "partial",
        source_counts={},
        cosine_kept={},
        classifier_relevant={},
        notes=notes,
        digest_md=None,
        hooks=[h.to_dict() for h in clean],
    )

    info(
        f"=== routr-burst complete: buffer={buffer_shipped}/{buffer_shipped + buffer_failed} "
        f"dm={dm_sent}/{dm_sent + dm_failed} total_ok={posted_total} ==="
    )
    return 0 if failed_total == 0 else 2


def _record_dry_run(run_id: str, hooks: list[Any], *, ship_method: str) -> None:
    for hook in hooks:
        post_id = f"post-{uuid.uuid4().hex[:12]}"
        signal_store.insert_post(
            post_id=post_id,
            kind="x_burst",
            platform="x",
            text=hook.text,
            status="dry_run",
            run_id=run_id,
            hook_format="x_thread",
            signal_id=hook.anchor_signal_id,
            metadata={"dry_run": True, "ship_method": ship_method, "length": len(hook.text)},
        )


def _build_dm_header(hook: Any, post_id: str, signals: list[dict[str, Any]]) -> str:
    """Compose a one-message header that frames the manual-post payload."""

    anchor_url = ""
    if hook.anchor_signal_id:
        for s in signals:
            if s.get("id") == hook.anchor_signal_id:
                anchor_url = s.get("url") or ""
                break

    lines = [
        f"Manual X post (length {len(hook.text)} chars, post id `{post_id}`).",
        "Copy the block below and paste into X. The system prompt has been"
        " followed but a sanity-skim is still wise.",
    ]
    if anchor_url:
        lines.append(f"Source signal: <{anchor_url}>")
    if hook.anchor_signal_id:
        lines.append(f"Signal id: `{hook.anchor_signal_id}`")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="routr-burst", description="Draft + ship N standalone X posts.")
    p.add_argument(
        "--count",
        type=int,
        default=int(env("ROUTR_X_BURST_COUNT") or DEFAULT_COUNT),
        help="Number of posts to draft (default 2)",
    )
    p.add_argument(
        "--window-hours",
        type=int,
        default=int(env("ROUTR_X_BURST_WINDOW_H") or DEFAULT_WINDOW_HOURS),
        help="Signal window in hours (default 48)",
    )
    p.add_argument(
        "--min-score",
        type=float,
        default=float(env("ROUTR_X_BURST_MIN_SCORE") or DEFAULT_MIN_SCORE),
        help="Minimum combined_score for candidate signals (default 0.55)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=env_flag("ROUTR_X_BURST_DRY_RUN", default=False),
        help="Draft + lint but skip Buffer publish and Discord DM",
    )
    return p.parse_args(argv)


def cli() -> None:
    """Entry point for the `routr-burst` console script."""

    args = _parse_args()
    try:
        sys.exit(
            run(
                count=args.count,
                window_hours=args.window_hours,
                min_score=args.min_score,
                dry_run=args.dry_run,
            )
        )
    except KeyboardInterrupt:
        error("interrupted")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        error(f"unhandled error: {e}")
        raise


if __name__ == "__main__":
    cli()
