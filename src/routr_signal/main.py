"""Pipeline orchestrator.

Order of operations:
  1. Fetch from each enabled source (sequential to keep host throttling honest).
  2. Classify each source's items via Claude (chunked).
  3. Build the Digest object.
  4. Render Markdown digest to data/digests/YYYY-MM-DD.md.
  5. Append qualified leads to data/leads/queue.jsonl.
  6. Publish to Slack / Discord / email (unless ROUTR_SIGNAL_PUBLISH=0).
  7. Exit nonzero only on unrecoverable failures.
"""

from __future__ import annotations

import json
import sys

from .classify import lead_extractor, pain_signal, post_drafter
from .classify import voice_lint
from .lib import cosine as cosine_layer
from .lib.env import env_flag, env_list
from .lib.logging import error, info, warn
from .lib.paths import raw_dir, today_utc
from .lib.types import ClassifiedItem, Digest, Lead, RawItem
from .output import discord, email, leads_queue, markdown_digest, slack
from .sources import discord_paste, github_issues, hn, reddit, twitter


SOURCE_REGISTRY = {
    "hn": hn.fetch,
    "reddit": reddit.fetch,
    "github_issues": github_issues.fetch,
    "twitter": twitter.fetch,
    "discord_paste": discord_paste.fetch,
}

DEFAULT_SOURCES = ["hn", "reddit", "github_issues", "twitter", "discord_paste"]
TOP_SIGNALS_FOR_DIGEST = 5
MIN_SCORE_FOR_DIGEST = 0.55


def run() -> int:
    today = today_utc()
    info(f"=== routr-signal-engine run for {today} ===")

    publish = env_flag("ROUTR_SIGNAL_PUBLISH", default=True)
    sources = env_list("ROUTR_SIGNAL_SOURCES", default=DEFAULT_SOURCES) or DEFAULT_SOURCES
    info(f"sources: {sources}; publish={publish}")

    # 1. Fetch
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
        _persist_raw(src, today, items)

    total_fetched = sum(len(v) for v in source_items.values())
    info(f"fetched {total_fetched} total items across {len(source_items)} sources")
    if total_fetched == 0:
        notes.append("All sources returned 0 items today.")

    # 1.5. Cosine relevance prefilter — semantic second layer between keyword filter and LLM.
    cosine_kept_by_source: dict[str, list[RawItem]] = {}
    cosine_dropped_count = 0
    for src, items in source_items.items():
        if not items:
            cosine_kept_by_source[src] = []
            continue
        kept_pairs, dropped = cosine_layer.score_items(items)
        cosine_kept_by_source[src] = [it for it, _ in kept_pairs]
        cosine_dropped_count += len(dropped)
        info(
            f"cosine[{src}]: {len(items)} -> {len(kept_pairs)} kept "
            f"(dropped {len(dropped)} below threshold)"
        )
    if cosine_dropped_count:
        notes.append(f"cosine prefilter dropped {cosine_dropped_count} items below threshold")

    # 2. LLM classify
    all_classified: list[ClassifiedItem] = []
    for src, items in cosine_kept_by_source.items():
        if not items:
            continue
        classified = pain_signal.classify(items)
        info(f"classify[{src}]: {sum(1 for c in classified if c.relevant)}/{len(classified)} relevant")
        all_classified.extend(classified)

    # 3. Compose digest
    digest = _build_digest(today, source_items, all_classified, notes)

    # Generate post hooks BEFORE writing the digest so they land in the markdown too.
    hooks, low_signal = post_drafter.draft(digest.pain_signals)
    if low_signal:
        digest.notes.append("low_signal_day: drafter fell back to long-running wedges")
    digest.hooks = hooks

    # Voice-lint the drafter output. Soft warning, not a hard fail.
    if hooks:
        lint_results = voice_lint.lint_all(hooks)
        lint_note = voice_lint.summarize_violations(lint_results)
        if lint_note:
            warn(lint_note)
            digest.notes.append(lint_note)

    # 4. Persist digest
    md_path = markdown_digest.write_to_disk(digest)
    info(f"digest written → {md_path}")

    # Also dump a machine-readable copy alongside the raw payloads.
    (raw_dir() / f"digest-{today}.json").write_text(
        json.dumps(digest.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 5. Leads
    leads = lead_extractor.extract(digest.pain_signals)
    if leads:
        leads_queue.append(leads)

    # 6. Publish
    if publish:
        ok_slack = slack.publish(digest)
        ok_discord = discord.publish(digest)
        ok_email = email.publish(digest)
        info(f"publish: slack={ok_slack} discord={ok_discord} email={ok_email}")
    else:
        info("publish: skipped (ROUTR_SIGNAL_PUBLISH=0)")

    info("=== run complete ===")
    return 0


def _build_digest(
    date: str,
    source_items: dict[str, list[RawItem]],
    classified: list[ClassifiedItem],
    notes: list[str],
) -> Digest:
    # Detect classifier-down fallback: every classified item has score 0 AND is relevant.
    # In that case the classifier didn't run (Claude was unreachable). Surface ALL pre-filtered
    # items so the human can still triage manually.
    classifier_down = (
        bool(classified)
        and all(c.relevant and c.score == 0.0 for c in classified)
        and all(
            (c.pain_summary or "").startswith("[UNCLASSIFIED]") for c in classified
        )
    )
    if classifier_down:
        notes = [*notes, "Classifier unreachable — falling back to keyword-filtered raw items."]
        # When the LLM is down, rank by cosine alone.
        classified_sorted = sorted(classified, key=lambda c: c.cosine_score, reverse=True)
        top_signals = classified_sorted[:TOP_SIGNALS_FOR_DIGEST]
    else:
        relevant = [c for c in classified if c.relevant and c.combined_score >= MIN_SCORE_FOR_DIGEST]
        relevant.sort(key=lambda c: c.combined_score, reverse=True)
        top_signals = relevant[:TOP_SIGNALS_FOR_DIGEST]

    leads = lead_extractor.extract(top_signals)
    # Dedup leads against pain signals list — same author shouldn't appear in both
    # signals AND "active accounts" sections. We keep them in active_accounts only when
    # there's a *separate* relevant signal beyond their primary one.
    deduped_leads: list[Lead] = []
    primary_handles = {(c.lead_handle or c.raw.author or "").lower() for c in top_signals}
    primary_handles.discard("")
    for l in leads:
        if l.handle.lower() in primary_handles:
            # Already represented in top_signals; surface them only once.
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


def _persist_raw(source: str, date: str, items: list[RawItem]) -> None:
    """Snapshot the raw fetched items so we can debug classifier decisions later."""

    path = raw_dir(source) / f"{date}.json"
    path.write_text(
        json.dumps([it.to_dict() for it in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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
