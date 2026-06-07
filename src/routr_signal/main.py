"""Pipeline orchestrator.

Order of operations:
  0. Open a run row in SQLite.
  1. Fetch from each enabled source (sequential to keep host throttling honest).
     Each fresh item is INSERTed into the `signals` table.
  2. Cosine prefilter. UPDATE signals SET cosine_score, cosine_top_topic.
  3. LLM classifier on cosine-kept items. UPDATE signals SET llm_*, combined_score.
  4. Rank top-N by combined_score. UPDATE signals SET rank_in_run.
  5. Drafter writes 5 post hooks.
  6. Persist digest markdown (gitignored locally, archived in runs table).
  7. Mark queued leads.
  8. Publish to Discord (Slack / email optional).
  9. Close the run row with status + counts.

State storage:
  Old: JSON files under data/seen/, data/raw/, data/leads/.
  New: SQLite at data/intel.db. The Actions cache covers it for CI.
"""

from __future__ import annotations

import sys
import uuid
from typing import Any

from .classify import lead_extractor, pain_signal, post_drafter
from .classify import voice_lint
from .lib import cosine as cosine_layer
from .lib import signal_store
from .lib.env import env, env_flag, env_list
from .lib.logging import error, info, warn
from .lib.paths import today_utc
from .lib.types import ClassifiedItem, Digest, Lead, RawItem
from .output import discord, email, leads_queue, markdown_digest, slack
from .sources import (
    discord_paste,
    github_issues,
    hf_papers,
    hn,
    newsletters,
    reddit,
    twitter,
)


SOURCE_REGISTRY = {
    "hn": hn.fetch,
    "reddit": reddit.fetch,
    "github_issues": github_issues.fetch,
    "twitter": twitter.fetch,
    "discord_paste": discord_paste.fetch,
    "hf_papers": hf_papers.fetch,
    "newsletters": newsletters.fetch,
}

DEFAULT_SOURCES = [
    "hn",
    "reddit",
    "github_issues",
    "twitter",
    "discord_paste",
    "hf_papers",
    "newsletters",
]
TOP_SIGNALS_FOR_DIGEST = 5
MIN_SCORE_FOR_DIGEST = 0.55


def _run_id() -> str:
    """Use the GitHub Actions run id when available, else a local uuid4."""

    gh = env("GITHUB_RUN_ID")
    if gh:
        return gh
    return f"local-{uuid.uuid4().hex[:12]}"


def run() -> int:
    today = today_utc()
    run_id = _run_id()
    signal_store.open_run(run_id, kind="daily")
    info(f"=== routr-signal-engine run {run_id} for {today} ===")

    publish = env_flag("ROUTR_SIGNAL_PUBLISH", default=True)
    sources = env_list("ROUTR_SIGNAL_SOURCES", default=DEFAULT_SOURCES) or DEFAULT_SOURCES
    info(f"sources: {sources}; publish={publish}")

    # 1. Fetch + upsert into signals table.
    source_items: dict[str, list[RawItem]] = {}
    notes: list[str] = []
    for src in sources:
        fn = SOURCE_REGISTRY.get(src)
        if fn is None:
            warn(f"unknown source {src!r}; skipping")
            notes.append(f"unknown source {src!r}")
            continue
        try:
            items = fn()
        except Exception as e:  # noqa: BLE001
            error(f"source {src} crashed: {e}")
            notes.append(f"source `{src}` crashed: {e}")
            items = []
        source_items[src] = items
        # Sources persist their own RawItems via seen.add_item(); we just
        # tag this run as the one that surfaced them.
        for it in items:
            signal_store.attribute_to_run(it.id, run_id)

    total_fetched = sum(len(v) for v in source_items.values())
    info(f"fetched {total_fetched} total items across {len(source_items)} sources")
    if total_fetched == 0:
        notes.append("All sources returned 0 items today.")

    # 1.5. Cosine relevance prefilter -- semantic second layer between keyword and LLM.
    cosine_kept_by_source: dict[str, list[RawItem]] = {}
    cosine_kept_counts: dict[str, int] = {}
    cosine_dropped_count = 0
    for src, items in source_items.items():
        if not items:
            cosine_kept_by_source[src] = []
            cosine_kept_counts[src] = 0
            continue
        kept_pairs, dropped = cosine_layer.score_items(items)
        cosine_kept_by_source[src] = [it for it, _ in kept_pairs]
        cosine_kept_counts[src] = len(kept_pairs)
        cosine_dropped_count += len(dropped)
        info(
            f"cosine[{src}]: {len(items)} -> {len(kept_pairs)} kept "
            f"(dropped {len(dropped)} below threshold)"
        )
        # Write cosine scores back to DB for every item, kept or dropped.
        for it, cs in kept_pairs:
            signal_store.update_cosine(it.id, score=cs.score, top_topic=cs.top_topic)
        for it in dropped:
            signal_store.update_cosine(
                it.id,
                score=float(it.extra.get("cosine_score", 0.0)),
                top_topic=it.extra.get("cosine_top_topic"),
            )
    if cosine_dropped_count:
        notes.append(f"cosine prefilter dropped {cosine_dropped_count} items below threshold")

    # 2. LLM classify (only cosine-kept items)
    all_classified: list[ClassifiedItem] = []
    classifier_relevant_counts: dict[str, int] = {}
    for src, items in cosine_kept_by_source.items():
        if not items:
            classifier_relevant_counts[src] = 0
            continue
        classified = pain_signal.classify(items)
        rel = sum(1 for c in classified if c.relevant)
        info(f"classify[{src}]: {rel}/{len(classified)} relevant")
        classifier_relevant_counts[src] = rel
        all_classified.extend(classified)
        for c in classified:
            signal_store.update_classified(c)

    # 3. Compose digest
    digest = _build_digest(today, source_items, all_classified, notes)

    # 3a. Write rank_in_run back to DB for the top signals
    for rank, c in enumerate(digest.pain_signals, start=1):
        signal_store.update_rank(c.raw.id, rank=rank, run_id=run_id)

    # 3aa. Refresh person aggregates after classification/ranking so the
    # people tables are queryable even before the future dashboard exists.
    people_count = signal_store.rebuild_people_from_signals()
    if people_count:
        info(f"people: refreshed {people_count} aggregate row(s)")

    # 3b. Compute weekly topic frequency. The drafter uses it to avoid
    #     re-covering already-saturated angles; the digest footer surfaces
    #     it so the operator can see the topic distribution at a glance.
    topic_freq = signal_store.topic_frequency(window_days=7)
    if topic_freq:
        # Render compact footer: top 6 topics shown, "topic xN" pairs separated by " · ".
        top_six = list(topic_freq.items())[:6]
        freq_line = " · ".join(f"{t} x{n}" for t, n in top_six)
        digest.notes.append(f"7d topic frequency (top 6): {freq_line}")

    # 4. Generate post hooks (passing topic_frequency for repetition-avoidance)
    recent_x_posts = signal_store.recent_post_texts(kind="hook", platform="x", days=14, limit=20)
    hooks, low_signal = post_drafter.draft(
        digest.pain_signals,
        topic_frequency=topic_freq,
        recent_posts=recent_x_posts,
    )
    if low_signal:
        digest.notes.append("low_signal_day: drafter fell back to long-running wedges")
    digest.hooks = hooks

    # 4a. Voice-lint the drafter output.
    if hooks:
        lint_results = voice_lint.lint_all(hooks)
        lint_note = voice_lint.summarize_violations(lint_results)
        if lint_note:
            warn(lint_note)
            digest.notes.append(lint_note)

    # 5. Persist digest locally (gitignored). The DB / runs table holds the canonical archive.
    md_path = markdown_digest.write_to_disk(digest)
    info(f"digest written -> {md_path}")
    digest_md = md_path.read_text(encoding="utf-8")

    # 6. Mark queued leads in the DB (action_label='queued')
    leads = lead_extractor.extract(digest.pain_signals)
    if leads:
        leads_queue.append(leads)

    # 7. Publish
    discord_msg_ids: list[str] = []
    if publish:
        ok_slack = slack.publish(digest)
        discord_msg_ids = discord.publish(digest)
        ok_email = email.publish(digest)
        info(
            f"publish: slack={ok_slack} "
            f"discord={len(discord_msg_ids)}-msg(s) "
            f"email={ok_email}"
        )
        # 7a. Record digest message IDs for the dispatch worker to poll later,
        # and pre-create one 'pending' post row per auto-postable hook.
        if discord_msg_ids:
            signal_store.record_run_discord_messages(run_id, discord_msg_ids)
            _enqueue_pending_hooks(run_id, hooks, discord_msg_ids)
    else:
        info("publish: skipped (ROUTR_SIGNAL_PUBLISH=0)")

    # 8. Close the run row.
    signal_store.close_run(
        run_id,
        status="success",
        source_counts={s: len(v) for s, v in source_items.items()},
        cosine_kept=cosine_kept_counts,
        classifier_relevant=classifier_relevant_counts,
        notes=digest.notes,
        digest_md=digest_md,
        hooks=[h.to_dict() for h in hooks],
    )

    info("=== run complete ===")
    return 0


def _build_digest(
    date: str,
    source_items: dict[str, list[RawItem]],
    classified: list[ClassifiedItem],
    notes: list[str],
) -> Digest:
    # Detect classifier-down fallback.
    classifier_down = (
        bool(classified)
        and all(c.relevant and c.score == 0.0 for c in classified)
        and all((c.pain_summary or "").startswith("[UNCLASSIFIED]") for c in classified)
    )
    if classifier_down:
        notes = [*notes, "Classifier unreachable; falling back to keyword-filtered raw items."]
        classified_sorted = sorted(classified, key=lambda c: c.cosine_score, reverse=True)
        top_signals = classified_sorted[:TOP_SIGNALS_FOR_DIGEST]
    else:
        relevant = [c for c in classified if c.relevant and c.combined_score >= MIN_SCORE_FOR_DIGEST]
        relevant.sort(key=lambda c: c.combined_score, reverse=True)
        top_signals = relevant[:TOP_SIGNALS_FOR_DIGEST]

    leads = lead_extractor.extract(top_signals)
    deduped_leads: list[Lead] = []
    primary_handles = {(c.lead_handle or c.raw.author or "").lower() for c in top_signals}
    primary_handles.discard("")
    for l in leads:
        if l.handle.lower() in primary_handles:
            continue
        deduped_leads.append(l)

    source_counts = {src: len(items) for src, items in source_items.items()}

    return Digest(
        date=date,
        pain_signals=top_signals,
        active_accounts=deduped_leads[:5],
        hooks=[],
        source_counts=source_counts,
        notes=list(notes),
    )



# Hook formats that the dispatch worker can auto-publish on approval.
# Today: only x_thread (via Buffer + the user's connected X profile).
# Reddit / HN / Dev.to / LinkedIn drafts remain reference text for the user.
AUTO_DISPATCHABLE_HOOK_FORMATS: tuple[str, ...] = ("x_thread",)


def _enqueue_pending_hooks(
    run_id: str,
    hooks: list[Any],
    discord_msg_ids: list[str],
) -> None:
    """Pre-create one `pending` post per auto-dispatchable hook so the
    dispatch worker has a concrete row to promote when it sees a reaction.

    We attach the pending post to the LAST Discord message that landed --
    when the digest splits across two messages, hooks live in the second
    one. If only one message exists, we use it. The dispatch worker checks
    reactions on every message id recorded against the run, so this
    attachment is informational rather than strictly required.
    """

    import uuid as _uuid

    if not hooks or not discord_msg_ids:
        return

    anchor_msg_id = discord_msg_ids[-1]
    for hook in hooks:
        fmt = getattr(hook, "format", None)
        if fmt not in AUTO_DISPATCHABLE_HOOK_FORMATS:
            continue
        lint_result = voice_lint.lint_hook(hook)
        if lint_result.violations:
            warn(
                "posts: skipped pending dispatch for lint-failing "
                f"{fmt} hook (signal={getattr(hook, 'anchor_signal_id', None)}): "
                f"{', '.join(lint_result.violations)}"
            )
            continue
        post_id = f"post-{_uuid.uuid4().hex[:12]}"
        signal_store.insert_post(
            post_id=post_id,
            kind="hook",
            platform="x",
            text=hook.text,
            status="pending",
            run_id=run_id,
            hook_format=fmt,
            signal_id=getattr(hook, "anchor_signal_id", None),
            discord_message_id=anchor_msg_id,
            metadata={"source_message_ids": list(discord_msg_ids)},
        )
        info(f"posts: enqueued pending {fmt} hook -> {post_id} (signal={hook.anchor_signal_id})")


def cli() -> None:
    """Entry point for the `routr-signal` console script."""

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
