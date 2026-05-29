"""X-burst task: draft N standalone X posts for manual review.

Runs on its own GitHub Actions cron (twice a day; see `pipeline.yml`).
Independent of the daily-digest flow:

  - Reads the most-recent classified signals from intel.db (last 48h).
  - Computes 7-day topic frequency (same helper the daily drafter uses).
  - Asks the drafter for N standalone X posts via
    `classify/x_burst_drafter.py` + `config/prompts/x_burst.md`.
  - Voice-lints each draft (length cap 25,000; URL check; usual rules)
    and DROPS any draft that fails.
  - Sends every surviving post to the operator by Discord DM for manual review.
  - Records each post in the `posts` table with platform='x',
    kind='x_burst', metadata['ship_method']='discord_dm'.

Why manual review only: the first autonomous bursts repeated the same few
agent/gateway angles and short posts were going public without the deliberate
📤 approval gate. Until the content memory and voice quality are proven, this
lane must never publish directly to X.

What this task does NOT do:
  - Does not fetch from sources (the daily pipeline does that).
  - Does not classify (the daily pipeline does that).
  - Does not write to the Discord digest channel (the daily pipeline does that).
  - Does not publish directly to X.

Run locally:
    routr-burst --count=2 --dry-run

Environment knobs (all optional):
    ROUTR_X_BURST_COUNT       int, posts to draft per call (default 2)
    ROUTR_X_BURST_WINDOW_H    int, signal window in hours (default 48)
    ROUTR_X_BURST_MIN_SCORE   float, min combined_score (default 0.55)
    ROUTR_X_BURST_DRY_RUN     "1" to draft + lint but skip publish + DM
    DISCORD_USER_DM_ID        Discord user id of the operator (recipient
                              of all draft DMs).
"""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from typing import Any

from ..classify import voice_lint
from ..classify.x_burst_drafter import draft_x_burst
from ..lib import discord_inbox, signal_store
from ..lib.env import env, env_flag
from ..lib.logging import error, info, warn


DEFAULT_COUNT = 2
DEFAULT_WINDOW_HOURS = 48
DEFAULT_MIN_SCORE = 0.55
DEFAULT_SIGNAL_LIMIT = 20
RECENT_POST_MEMORY_DAYS = 14
SIMILARITY_DROP_THRESHOLD = 0.34
X_BURST_DISPATCH_CAP = voice_lint.X_BURST_AUTO_SHIP_CAP


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

    dm_user_id = env("DISCORD_USER_DM_ID") or ""
    dm_ok = bool(dm_user_id) and discord_inbox.is_dm_configured()

    if not dry_run and not dm_ok:
        warn(
            "routr-burst: Discord DM is not configured. "
            "Manual-review mode requires DISCORD_USER_DM_ID + bot config. Exiting clean."
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
    if not dm_ok:
        warn("routr-burst: Discord DM not configured; non-dry-run drafts will be skipped")

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

    # 3. Recent post memory. Avoid same source material and same angle family.
    excluded = signal_store.signal_ids_posted_since(
        kind="x_burst",
        platform="x",
        days=RECENT_POST_MEMORY_DAYS,
    )
    if excluded:
        info(
            f"routr-burst: excluding {len(excluded)} signal id(s) drafted in "
            f"last {RECENT_POST_MEMORY_DAYS}d"
        )
        signals = [s for s in signals if s.get("id") not in excluded]
    if not signals:
        warn("routr-burst: every recent signal was already drafted recently; nothing novel to draft.")
        signal_store.close_run(
            run_id,
            status="empty",
            source_counts={},
            cosine_kept={},
            classifier_relevant={},
            notes=[f"all candidate signals drafted in last {RECENT_POST_MEMORY_DAYS}d"],
            digest_md=None,
            hooks=None,
        )
        return 0
    recent_posts = signal_store.recent_post_texts(
        kind="x_burst",
        platform="x",
        days=RECENT_POST_MEMORY_DAYS,
        limit=50,
    )

    # 4. Draft.
    posts = draft_x_burst(
        signals,
        count=count,
        topic_frequency=topic_freq,
        excluded_signal_ids=excluded,
        recent_posts=recent_posts,
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

    # 5. Voice-lint and novelty-lint. Drop any post that fails.
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
        similar_to = _most_similar_recent_post(r.hook.text, recent_posts)
        if similar_to is not None:
            score, post_id = similar_to
            warn(
                f"routr-burst: dropping draft due to recent-text similarity "
                f"score={score:.2f} recent_post={post_id} :: {r.hook.text[:140]!r}"
            )
            lint_notes.append(f"dropped: similar_to_recent:{score:.2f}:{post_id}")
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

    info(f"routr-burst: {len(clean)} of {len(posts)} drafts passed lint; routing all to Discord DM")

    if dry_run:
        info("routr-burst: dry-run, skipping all publish + DM")
        _record_dry_run(run_id, clean, ship_method="discord_dm_manual_review")
        signal_store.close_run(
            run_id,
            status="dry_run",
            source_counts={},
            cosine_kept={},
            classifier_relevant={},
            notes=lint_notes
            + [
                f"dry_run: would send {len(clean)} draft(s) via Discord DM for manual review"
            ],
            digest_md=None,
            hooks=[h.to_dict() for h in clean],
        )
        return 0

    # 6. DM every post. Short Buffer-safe posts can be approved from the DM
    # with 📤 and dispatched by routr-dispatch; long-form remains manual-only.
    dm_sent = 0
    dm_failed = 0
    dispatch_refs: list[str] = []
    if not dm_ok:
        warn(f"routr-burst: dropping {len(clean)} draft(s) (Discord DM not configured)")
        dm_failed = len(clean)
    else:
        for hook in clean:
            post_id = f"post-{uuid.uuid4().hex[:12]}"
            header = _build_dm_header(hook, post_id, signals)
            dm_result = discord_inbox.send_dm_with_channel(dm_user_id, hook.text, header=header)
            if not dm_result:
                warn(f"routr-burst: Discord DM failed for {post_id}")
                signal_store.insert_post(
                    post_id=post_id,
                    kind="x_burst",
                    platform="x",
                    text=hook.text,
                    status="failed",
                    run_id=run_id,
                    hook_format="x_thread",
                    signal_id=hook.anchor_signal_id,
                    metadata={"ship_method": "discord_dm_failed", "length": len(hook.text)},
                )
                signal_store.update_post_status(
                    post_id,
                    status="failed",
                    error="Discord DM send failed",
                )
                dm_failed += 1
                continue
            dm_channel_id, msg_ids = dm_result
            is_dispatchable = len(hook.text) <= X_BURST_DISPATCH_CAP
            dispatch_refs.extend(f"{dm_channel_id}:{mid}" for mid in msg_ids)
            if is_dispatchable:
                signal_store.insert_post(
                    post_id=post_id,
                    kind="x_burst",
                    platform="x",
                    text=hook.text,
                    status="pending",
                    run_id=run_id,
                    hook_format="x_thread",
                    signal_id=hook.anchor_signal_id,
                    metadata={
                        "ship_method": "discord_dm_approval_to_buffer",
                        "length": len(hook.text),
                        "discord_channel_id": dm_channel_id,
                        "discord_message_ids": msg_ids,
                    },
                )
            else:
                signal_store.insert_post(
                    post_id=post_id,
                    kind="x_burst",
                    platform="x",
                    text=hook.text,
                    status="pending_manual",
                    run_id=run_id,
                    hook_format="x_thread",
                    signal_id=hook.anchor_signal_id,
                    metadata={
                        "ship_method": "discord_dm_manual_review",
                        "length": len(hook.text),
                        "discord_channel_id": dm_channel_id,
                        "discord_message_ids": msg_ids,
                    },
                )
                signal_store.update_post_status(
                    post_id,
                    status="awaiting_manual_post",
                )
            info(
                f"routr-burst: DM ok post={post_id} msgs={len(msg_ids)} "
                f"signal={hook.anchor_signal_id} len={len(hook.text)}"
            )
            dm_sent += 1

    if dispatch_refs:
        signal_store.record_run_discord_messages(run_id, dispatch_refs)

    failed_total = dm_failed
    notes = list(lint_notes)
    notes.append(f"manual_review_only=true dm_sent={dm_sent} dm_failed={dm_failed}")
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
        f"=== routr-burst complete: manual_review_dm={dm_sent}/{dm_sent + dm_failed} ==="
    )
    return 0 if failed_total == 0 else 2


def _record_dry_run(run_id: str, hooks: list[Any], *, ship_method: str) -> None:
    for hook in hooks:
        post_id = f"post-{uuid.uuid4().hex[:12]}"
        signal_store.insert_post(
            post_id=post_id,
            kind="x_burst_dry_run",
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


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_'-]{2,}", re.IGNORECASE)
_STOPWORDS = {
    "about", "after", "again", "against", "agent", "agents", "because", "being",
    "between", "could", "every", "from", "have", "into", "just", "like", "more",
    "most", "much", "need", "needs", "only", "people", "post", "posts", "same",
    "should", "system", "systems", "that", "their", "there", "these", "they", "this",
    "through", "when", "where", "which", "while", "with", "without", "would", "your",
}


def _text_tokens(text: str) -> set[str]:
    return {t.lower().strip("'_") for t in _TOKEN_RE.findall(text) if t.lower() not in _STOPWORDS}


def _similarity(a: str, b: str) -> float:
    ta = _text_tokens(a)
    tb = _text_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _most_similar_recent_post(
    text: str,
    recent_posts: list[dict[str, Any]],
) -> tuple[float, str] | None:
    best_score = 0.0
    best_id = ""
    for post in recent_posts:
        prior = str(post.get("text") or "")
        if not prior:
            continue
        score = _similarity(text, prior)
        if score > best_score:
            best_score = score
            best_id = str(post.get("id") or post.get("signal_id") or "unknown")
    if best_score >= SIMILARITY_DROP_THRESHOLD:
        return (best_score, best_id)
    return None


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
