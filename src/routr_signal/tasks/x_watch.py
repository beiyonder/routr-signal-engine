from __future__ import annotations

import argparse
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ..classify import x_reply_scorer
from ..lib import discord_inbox
from ..lib import signal_store
from ..lib.config import x_fast_watch
from ..lib.env import env, env_flag
from ..lib.logging import info, warn
from ..lib.types import RawItem
from ..sources import twitter


DEFAULT_FRESH_WINDOW_MINUTES = 60
DEFAULT_MIN_AGE_MINUTES = 10
DEFAULT_ACCOUNTS_PER_GROUP = 8
DEFAULT_MAX_RESULTS_PER_GROUP = 12
DEFAULT_MAX_ALERTS = 3
DEFAULT_MIN_SCORE = 0.64


def _run_id() -> str:
    return env("GITHUB_RUN_ID") or f"local-xwatch-{uuid.uuid4().hex[:6]}"


def run(*, dry_run: bool = False) -> int:
    run_id = f"{_run_id()}-xwatch-{uuid.uuid4().hex[:6]}"
    signal_store.open_run(run_id, kind="x_watch")
    info(f"=== x_watch run {run_id} dry_run={dry_run} ===")

    cfg = x_fast_watch()
    if not cfg or not cfg.get("accounts"):
        warn("x_watch: config/x_fast_watch.yaml missing or empty")
        signal_store.close_run(
            run_id,
            status="skipped",
            source_counts={},
            cosine_kept={},
            classifier_relevant={},
            notes=["missing x_fast_watch config"],
            digest_md=None,
            hooks=None,
        )
        return 0

    fetch_cfg = cfg.get("fetch", {}) if isinstance(cfg.get("fetch"), dict) else {}
    fresh_window = int(
        env("ROUTR_X_WATCH_FRESH_WINDOW_MINUTES")
        or fetch_cfg.get("fresh_window_minutes", DEFAULT_FRESH_WINDOW_MINUTES)
    )
    min_age = int(env("ROUTR_X_WATCH_MIN_AGE_MINUTES") or fetch_cfg.get("min_age_minutes", DEFAULT_MIN_AGE_MINUTES))
    max_alerts = int(
        env("ROUTR_X_WATCH_MAX_ALERTS") or fetch_cfg.get("max_alerts_per_run", DEFAULT_MAX_ALERTS)
    )
    min_score = float(env("ROUTR_X_WATCH_MIN_SCORE") or fetch_cfg.get("min_score", DEFAULT_MIN_SCORE))

    search_cfg = _build_twitter_config(cfg)
    items = twitter.fetch_from_config(
        search_cfg,
        apply_keyword_prefilter=False,
        id_prefix="xwatch",
        # x_watch dedupes on sent alerts, not raw fetches. Marking every
        # fetched tweet as seen would suppress a later alert if the scorer
        # threshold/prompt changes or the operator widens the run window.
        persist_seen=False,
    )
    fresh = _fresh_items(items, min_age_minutes=min_age, window_minutes=fresh_window)
    fresh = [
        it
        for it in fresh
        if not signal_store.has_post_for_signal(kind="x_reply_alert", signal_id=it.id)
    ]
    if not fresh:
        info("x_watch: no fresh unalerted tweets")
        signal_store.close_run(
            run_id,
            status="empty",
            source_counts={"x": len(items)},
            cosine_kept={},
            classifier_relevant={},
            notes=[f"no fresh unalerted tweets within {min_age}-{fresh_window}m"],
            digest_md=None,
            hooks=None,
        )
        return 0

    account_meta = _account_meta(cfg)
    opportunities = x_reply_scorer.score(
        fresh,
        account_meta=account_meta,
        min_score=min_score,
        limit=max_alerts,
    )
    if not opportunities:
        info("x_watch: scorer found no DM-worthy opportunities")
        signal_store.close_run(
            run_id,
            status="empty",
            source_counts={"x": len(items)},
            cosine_kept={},
            classifier_relevant={},
            notes=[f"no opportunities above {min_score}"],
            digest_md=None,
            hooks=None,
        )
        return 0

    by_id = {it.id: it for it in fresh}
    user_id = env("DISCORD_USER_DM_ID") or ""
    dm_ok = bool(user_id) and discord_inbox.is_configured()
    sent = 0
    failed = 0

    for opp in opportunities:
        item = by_id.get(opp.signal_id)
        if item is None:
            continue
        body = _render_dm(item, opp, account_meta=account_meta)
        post_id = f"post-{uuid.uuid4().hex[:12]}"
        post_kind = "x_reply_alert_dry_run" if dry_run else "x_reply_alert"
        signal_store.insert_post(
            post_id=post_id,
            kind=post_kind,
            platform="x",
            text=opp.suggested_reply,
            status="dry_run" if dry_run else "dm_pending",
            run_id=run_id,
            hook_format="x_reply",
            signal_id=item.id,
            metadata={
                "score": opp.score,
                "tweet_url": item.url,
                "tweet_author": item.author,
                "reply_angle": opp.reply_angle,
                "reason": opp.reason,
            },
        )
        if dry_run:
            info(f"x_watch: dry-run alert score={opp.score:.2f} {item.author} {item.url}")
            print("\n--- X WATCH DRY RUN ALERT ---")
            print(body)
            print("--- END ALERT ---\n")
            sent += 1
            continue
        if not dm_ok:
            warn("x_watch: Discord DM not configured; cannot send alert")
            signal_store.update_post_status(
                post_id,
                status="failed",
                error="Discord DM not configured",
            )
            failed += 1
            continue
        msg_ids = discord_inbox.send_dm(
            user_id,
            body,
            header=f"Fast X reply opportunity: {item.author or 'unknown'} ({opp.score:.2f})",
            code_block=False,
        )
        if msg_ids:
            signal_store.update_post_status(post_id, status="dm_sent")
            info(f"x_watch: DM ok post={post_id} signal={item.id} score={opp.score:.2f}")
            sent += 1
        else:
            signal_store.update_post_status(post_id, status="failed", error="Discord DM send failed")
            failed += 1

    signal_store.close_run(
        run_id,
        status="success" if failed == 0 else "partial",
        source_counts={"x": len(items)},
        cosine_kept={},
        classifier_relevant={"x_reply_alerts": sent},
        notes=[f"fresh={len(fresh)} sent={sent} failed={failed}"],
        digest_md=None,
        hooks=None,
    )
    info(f"=== x_watch complete: sent={sent} failed={failed} ===")
    return 0 if failed == 0 else 2


def _build_twitter_config(cfg: dict[str, Any]) -> dict[str, Any]:
    fetch_cfg = cfg.get("fetch", {}) if isinstance(cfg.get("fetch"), dict) else {}
    accounts = _prioritized_accounts(cfg)
    per_group = int(fetch_cfg.get("accounts_per_group", DEFAULT_ACCOUNTS_PER_GROUP))
    max_results = int(fetch_cfg.get("max_results_per_group", DEFAULT_MAX_RESULTS_PER_GROUP))
    delay = float(fetch_cfg.get("request_delay_seconds", 10))
    total_max = int(fetch_cfg.get("total_max_items", 60))
    include_replies = bool(fetch_cfg.get("include_replies", False))

    searches: list[str] = []
    for i in range(0, len(accounts), max(1, per_group)):
        group = accounts[i : i + per_group]
        clauses = [f"from:{str(a['handle']).lstrip('@')}" for a in group]
        query = "(" + " OR ".join(clauses) + ")"
        if not include_replies:
            query += " -filter:replies"
        searches.append(query)

    return {
        "fetch": {
            "max_search_results": max_results,
            "request_delay_seconds": delay,
            "total_max_items": total_max,
        },
        "searches": searches,
        "users": [],
    }


def _prioritized_accounts(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    accounts = [a for a in cfg.get("accounts", []) if isinstance(a, dict) and a.get("handle")]

    def _rank(account: dict[str, Any]) -> tuple[int, int]:
        tags = {str(t) for t in account.get("tags", []) if t}
        tier = int(account.get("tier") or 9)
        if "vc" in tags:
            lane = 2
        elif tier == 1:
            lane = 3
        else:
            lane = 0 if tier == 3 else 1
        return (lane, tier)

    return sorted(accounts, key=_rank)


def _fresh_items(items: list[RawItem], *, min_age_minutes: int, window_minutes: int) -> list[RawItem]:
    now = datetime.now(timezone.utc)
    oldest = now - timedelta(minutes=window_minutes)
    newest = now - timedelta(minutes=min_age_minutes)
    fresh = [
        it
        for it in items
        if oldest <= it.created_at.astimezone(timezone.utc) <= newest
    ]
    fresh.sort(key=lambda it: it.created_at, reverse=True)
    return fresh


def _account_meta(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for account in cfg.get("accounts", []):
        if not isinstance(account, dict):
            continue
        handle = str(account.get("handle") or "").lstrip("@").lower()
        if not handle:
            continue
        out[handle] = {
            "tier": account.get("tier"),
            "tags": account.get("tags", []),
        }
    return out


def _render_dm(
    item: RawItem,
    opp: x_reply_scorer.ReplyOpportunity,
    *,
    account_meta: dict[str, dict[str, Any]],
) -> str:
    handle = (item.author or "").lstrip("@").lower()
    meta = account_meta.get(handle, {})
    age_minutes = int(
        (datetime.now(timezone.utc) - item.created_at.astimezone(timezone.utc)).total_seconds() // 60
    )
    tags = ", ".join(str(t) for t in meta.get("tags", [])) or "n/a"
    return "\n".join(
        [
            "COPY SUGGESTED REPLY ONLY:",
            "```",
            opp.suggested_reply,
            "```",
            "",
            f"Account: {item.author or 'unknown'} | tier {meta.get('tier', 'n/a')} | {tags}",
            f"Age: {age_minutes}m",
            f"Source: {item.url}",
            "",
            "Tweet:",
            item.body.strip(),
            "",
            f"Why reply: {opp.reason}",
            f"Angle: {opp.reply_angle}",
        ]
    )


def cli() -> None:
    parser = argparse.ArgumentParser(description="Fast X reply opportunity monitor")
    parser.add_argument("--dry-run", action="store_true", help="score and record, but skip Discord DM")
    args = parser.parse_args()
    dry_run = args.dry_run or env_flag("ROUTR_X_WATCH_DRY_RUN", default=False)
    raise SystemExit(run(dry_run=dry_run))


if __name__ == "__main__":
    cli()
