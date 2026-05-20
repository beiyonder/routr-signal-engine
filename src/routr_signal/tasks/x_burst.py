"""X-burst task: draft N standalone X posts and auto-ship them via Buffer.

Runs on its own GitHub Actions cron (twice a day; see `pipeline.yml`).
Independent of the daily-digest flow:

  - Reads the most-recent classified signals from intel.db (last 48h).
  - Computes 7-day topic frequency (same helper the daily drafter uses).
  - Asks the drafter for N standalone X posts via
    `classify/x_burst_drafter.py` + `config/prompts/x_burst.md`.
  - Voice-lints each draft (relaxed length cap; URL check) and DROPS
    any draft that fails (these go nowhere; no Discord review). On a
    bad-luck day where every draft fails lint, we ship nothing for that
    burst and exit clean.
  - Ships each surviving draft via `buffer_client.create_post(mode="shareNow")`.
  - Records each post in the `posts` table for tracking and de-dup.

What this task does NOT do:
  - Does not fetch from sources (the daily pipeline does that).
  - Does not classify (the daily pipeline does that).
  - Does not write to Discord (the digest flow stays untouched).
  - Does not require the 📤 reaction. These posts are auto-shipped; the
    safety net is voice_lint + per-run cap + the operator's ability to
    delete from X after the fact.

Run locally:
    routr-burst --count=2 --dry-run

Environment knobs (all optional):
    ROUTR_X_BURST_COUNT       — int, posts to draft per call (default 2)
    ROUTR_X_BURST_WINDOW_H    — int, signal window in hours (default 48)
    ROUTR_X_BURST_MIN_SCORE   — float, min combined_score (default 0.55)
    ROUTR_X_BURST_DRY_RUN     — "1" to draft + lint but not ship
"""

from __future__ import annotations

import argparse
import sys
import uuid
from typing import Any

from ..classify import voice_lint
from ..classify.x_burst_drafter import draft_x_burst
from ..lib import buffer_client, signal_store
from ..lib.env import env, env_flag, env_required
from ..lib.logging import error, info, warn


# Defaults are conservative; CI overrides via env vars or CLI flags.
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

    if not dry_run and not buffer_client.is_configured():
        warn("routr-burst: Buffer not configured; nothing to ship. exiting clean.")
        signal_store.close_run(
            run_id,
            status="skipped",
            source_counts={},
            cosine_kept={},
            classifier_relevant={},
            notes=["Buffer not configured"],
            digest_md=None,
            hooks=None,
        )
        return 0

    # 1. Recent classified signals -- raw row dicts; we only need ids + text fields.
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

    # 2. Topic frequency (for "avoid saturated topics" rule in the prompt).
    topic_freq = signal_store.topic_frequency(window_days=7)
    if topic_freq:
        top_six = list(topic_freq.items())[:6]
        info("routr-burst: 7d topic frequency (top 6): " + ", ".join(f"{t} x{n}" for t, n in top_six))

    # 3. Signals already drafted-against today (cross-burst de-dup).
    excluded = signal_store.signal_ids_posted_today(kind="x_burst", platform="x")
    if excluded:
        info(f"routr-burst: excluding {len(excluded)} signal id(s) already drafted today")

    # 4. Draft N posts.
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

    # 5. Voice-lint with the RELAXED X-burst rules. Drop any post that fails.
    lint_results = voice_lint.lint_x_burst_all(posts)
    clean: list[Any] = []
    lint_notes: list[str] = []
    for r in lint_results:
        if r.violations:
            warn(f"routr-burst: dropping draft due to lint: {r.violations} :: {r.hook.text[:140]!r}")
            lint_notes.append(f"dropped 1 draft: {', '.join(r.violations)}")
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

    info(f"routr-burst: {len(clean)} of {len(posts)} drafts passed lint; shipping")

    # 6. Ship each surviving post via Buffer (mode=shareNow) unless dry-run.
    posted = 0
    failed = 0
    if dry_run:
        info("routr-burst: dry-run mode, skipping Buffer publish")
        for hook in clean:
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
                metadata={"dry_run": True, "length": len(hook.text)},
            )
        signal_store.close_run(
            run_id,
            status="dry_run",
            source_counts={},
            cosine_kept={},
            classifier_relevant={},
            notes=lint_notes + [f"dry_run: would ship {len(clean)} post(s)"],
            digest_md=None,
            hooks=[h.to_dict() for h in clean],
        )
        return 0

    channel = env_required("BUFFER_X_CHANNEL_ID")
    for hook in clean:
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
            metadata={"length": len(hook.text)},
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
            failed += 1
            continue
        signal_store.update_post_status(
            post_id,
            status="posted",
            buffer_post_id=created.id,
            posted=True,
        )
        info(
            f"routr-burst: posted {post_id} via Buffer "
            f"buffer_post_id={created.id} signal={hook.anchor_signal_id} len={len(hook.text)}"
        )
        posted += 1

    notes = list(lint_notes)
    notes.append(f"shipped={posted} failed={failed}")
    signal_store.close_run(
        run_id,
        status="success" if failed == 0 else "partial",
        source_counts={},
        cosine_kept={},
        classifier_relevant={},
        notes=notes,
        digest_md=None,
        hooks=[h.to_dict() for h in clean],
    )

    info(f"=== routr-burst complete: posted={posted} failed={failed} ===")
    return 0 if failed == 0 else 2


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
        help="Draft + lint but skip Buffer publish",
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
