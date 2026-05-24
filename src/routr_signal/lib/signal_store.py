"""SQLite-backed operations on the `signals` and `runs` tables.

Owns all DB writes for the daily pipeline. The flow is progressive enrichment:

    fetch        -> upsert_fetched()       (insert with cosine/llm columns NULL)
    cosine       -> update_cosine()        (set cosine_score, cosine_top_topic)
    classify     -> update_classified()    (set llm_score, llm_pain_summary, ...)
    rank         -> update_rank()          (set combined_score, rank_in_run)

Idempotent at every step. Re-running a pipeline run with the same items
ignores the duplicates (INSERT OR IGNORE) and re-updates the columns.

Dedupe surface: `is_seen(id)` is a primary-key existence check, fast at
millions of rows.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from .db import get_db
from .types import ClassifiedItem, RawItem


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def is_seen(item_id: str) -> bool:
    row = get_db().execute(
        "SELECT 1 FROM signals WHERE id = ? LIMIT 1", (item_id,)
    ).fetchone()
    return row is not None


def already_seen_in(source: str) -> set[str]:
    """All known item IDs for one source. Useful when a source wants to
    pre-load the seen-set into memory and skip individual queries."""

    rows = get_db().execute("SELECT id FROM signals WHERE source = ?", (source,)).fetchall()
    return {r[0] for r in rows}


def count_by_source() -> dict[str, int]:
    rows = get_db().execute(
        "SELECT source, COUNT(*) AS n FROM signals GROUP BY source"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


# ---------------------------------------------------------------------------
# Write -- progressive enrichment
# ---------------------------------------------------------------------------


def upsert_fetched(item: RawItem, run_id: str | None) -> bool:
    """Insert a freshly fetched item. Returns True if it was new, False if it
    already existed (in which case the row is left alone)."""

    conn = get_db()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO signals (
            id, source, author_handle, title, body, url,
            created_at, fetched_at, raw_extra, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item.id,
            item.source,
            item.author,
            item.title,
            item.body,
            item.url,
            item.created_at.astimezone(timezone.utc).isoformat(),
            _utcnow_iso(),
            json.dumps(item.extra, ensure_ascii=False, default=str),
            run_id,
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def update_cosine(
    item_id: str,
    *,
    score: float | None,
    top_topic: str | None,
) -> None:
    get_db().execute(
        """
        UPDATE signals
           SET cosine_score = ?, cosine_top_topic = ?, cosine_at = ?
         WHERE id = ?
        """,
        (score, top_topic, _utcnow_iso(), item_id),
    )
    get_db().commit()


def update_classified(item: ClassifiedItem) -> None:
    raw = item.raw
    get_db().execute(
        """
        UPDATE signals
           SET llm_score = ?,
               llm_relevant = ?,
               llm_topics = ?,
               llm_pain_summary = ?,
               llm_engagement_angle = ?,
               llm_do_not_engage = ?,
               llm_lead_handle = ?,
               llm_lead_platform = ?,
               classified_at = ?,
               combined_score = ?
         WHERE id = ?
        """,
        (
            item.score,
            1 if item.relevant else 0,
            json.dumps(raw.extra.get("topics") or [], ensure_ascii=False),
            item.pain_summary,
            item.suggested_angle,
            item.do_not_engage_reason,
            item.lead_handle,
            item.lead_platform,
            _utcnow_iso(),
            item.combined_score,
            raw.id,
        ),
    )
    get_db().commit()


def attribute_to_run(item_id: str, run_id: str) -> None:
    """Tag a signal row with the run that touched it (without setting rank)."""

    get_db().execute(
        "UPDATE signals SET run_id = COALESCE(run_id, ?) WHERE id = ?",
        (run_id, item_id),
    )
    get_db().commit()


def update_rank(item_id: str, rank: int, run_id: str) -> None:
    get_db().execute(
        "UPDATE signals SET rank_in_run = ?, run_id = ? WHERE id = ?",
        (rank, run_id, item_id),
    )
    get_db().commit()


def update_action_label(
    item_id: str,
    *,
    label: str,
    notes: str | None = None,
    engaged: bool = False,
) -> None:
    """Used by the future dashboard's mutation endpoints."""

    get_db().execute(
        """
        UPDATE signals
           SET action_label = ?,
               action_notes = COALESCE(?, action_notes),
               engaged_at   = CASE WHEN ? THEN ? ELSE engaged_at END
         WHERE id = ?
        """,
        (label, notes, 1 if engaged else 0, _utcnow_iso(), item_id),
    )
    get_db().commit()


# ---------------------------------------------------------------------------
# Bulk reads (used by ranker / digest / dashboard sync)
# ---------------------------------------------------------------------------


def signals_for_run(run_id: str) -> list[dict[str, Any]]:
    rows = get_db().execute(
        """
        SELECT * FROM signals
         WHERE run_id = ?
         ORDER BY combined_score DESC NULLS LAST
        """,
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_signals(limit: int = 200) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM signals ORDER BY fetched_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_classified_for_drafting(
    *,
    window_hours: int = 48,
    min_score: float = 0.55,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Top relevant classified signals from the last `window_hours` UTC hours.

    Powers the standalone X-burst drafter, which runs outside the daily
    pipeline and draws from whatever signals the most recent daily run
    classified. Returns raw row dicts (not ClassifiedItem) because the
    consumer only needs id / title / body / url / topics / score for
    rendering the drafter payload.

    Ordered by `combined_score DESC` so the highest-quality stuff is at
    the top; the drafter takes the first N.
    """

    from datetime import datetime, timedelta, timezone

    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    rows = get_db().execute(
        """
        SELECT *
          FROM signals
         WHERE llm_relevant = 1
           AND classified_at IS NOT NULL
           AND classified_at >= ?
           AND COALESCE(combined_score, 0) >= ?
         ORDER BY combined_score DESC
         LIMIT ?
        """,
        (cutoff_iso, min_score, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def signal_ids_posted_today(
    *,
    kind: str = "x_burst",
    platform: str = "x",
) -> set[str]:
    """Signal IDs we already drafted X-burst posts against today (UTC).

    Used by the X-burst task to avoid re-anchoring to the same signal in
    the morning run and again in the afternoon run. We key on signal_id
    because the same signal in two different post drafts looks redundant
    to the reader even when the text is novel.

    Empty set if `kind` is unknown or no rows match.
    """

    from datetime import datetime, time, timezone

    midnight_iso = datetime.combine(
        datetime.now(timezone.utc).date(),
        time.min,
        tzinfo=timezone.utc,
    ).isoformat()
    rows = get_db().execute(
        """
        SELECT DISTINCT signal_id
          FROM posts
         WHERE kind = ?
           AND platform = ?
           AND signal_id IS NOT NULL
           AND created_at >= ?
        """,
        (kind, platform, midnight_iso),
    ).fetchall()
    return {r[0] for r in rows if r[0]}



def topic_frequency(window_days: int = 7) -> dict[str, int]:
    """Count how many times each LLM-assigned topic has appeared in the last
    `window_days` of CLASSIFIED signals. Used by the drafter to avoid
    re-covering angles already saturated in recent runs, and surfaced in
    the digest footer so the operator can see the weekly distribution.

    Only signals where `llm_relevant = 1` count (irrelevant ones don't shape
    the conversation we'd be drafting against).
    """

    from collections import Counter as _Counter
    from datetime import datetime, timedelta, timezone

    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    rows = get_db().execute(
        """
        SELECT llm_topics FROM signals
         WHERE llm_relevant = 1
           AND classified_at IS NOT NULL
           AND classified_at >= ?
        """,
        (cutoff_iso,),
    ).fetchall()

    counter: _Counter[str] = _Counter()
    for r in rows:
        raw = r[0] or "[]"
        try:
            topics = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(topics, list):
            continue
        for t in topics:
            if isinstance(t, str) and t:
                counter[t] += 1

    # Return as a plain dict ordered by frequency desc, top 20.
    return dict(counter.most_common(20))


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def open_run(run_id: str, *, kind: str = "daily") -> None:
    """Insert (or reset) a `runs` row. `kind` is `daily` for the main pipeline
    and `synthesis` for the weekly synthesis cron."""

    get_db().execute(
        "INSERT OR REPLACE INTO runs (id, started_at, kind) VALUES (?, ?, ?)",
        (run_id, _utcnow_iso(), kind),
    )
    get_db().commit()


def close_run(
    run_id: str,
    *,
    status: str,
    source_counts: dict[str, int],
    cosine_kept: dict[str, int],
    classifier_relevant: dict[str, int],
    notes: Iterable[str],
    digest_md: str | None,
    hooks: list[dict[str, Any]] | None,
    cost_estimate_usd: float | None = None,
) -> None:
    get_db().execute(
        """
        UPDATE runs
           SET finished_at = ?,
               status = ?,
               source_counts = ?,
               cosine_kept = ?,
               classifier_relevant = ?,
               notes = ?,
               cost_estimate_usd = ?,
               digest_md = ?,
               hooks_json = ?
         WHERE id = ?
        """,
        (
            _utcnow_iso(),
            status,
            json.dumps(source_counts, ensure_ascii=False),
            json.dumps(cosine_kept, ensure_ascii=False),
            json.dumps(classifier_relevant, ensure_ascii=False),
            json.dumps(list(notes), ensure_ascii=False),
            cost_estimate_usd,
            digest_md,
            json.dumps(hooks or [], ensure_ascii=False),
            run_id,
        ),
    )
    get_db().commit()


def recent_runs(limit: int = 50) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]



# ---------------------------------------------------------------------------
# Discord message tracking (per-run)
# ---------------------------------------------------------------------------


def record_run_discord_messages(run_id: str, message_ids: list[str]) -> None:
    """Save the Discord message IDs that contained this run's digest so the
    dispatch worker can later check them for reactions.

    Stored as a JSON array on the runs row; idempotent overwrite by run_id.
    """

    get_db().execute(
        "UPDATE runs SET discord_message_ids = ? WHERE id = ?",
        (json.dumps(list(message_ids), ensure_ascii=False), run_id),
    )
    get_db().commit()


def runs_with_pending_messages(
    *,
    kind: str | None = None,
    since_iso: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return recent runs that have at least one Discord message id recorded.

    Used by the dispatch worker to know which messages to poll for reactions.
    `since_iso` is an inclusive lower bound on `started_at`.
    """

    sql = (
        "SELECT * FROM runs "
        "WHERE discord_message_ids IS NOT NULL "
        "  AND discord_message_ids != '[]' "
        "  AND discord_message_ids != '' "
    )
    params: list[Any] = []
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    if since_iso is not None:
        sql += " AND started_at >= ?"
        params.append(since_iso)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    rows = get_db().execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Posts table: outgoing cross-channel publishes
# ---------------------------------------------------------------------------


def insert_post(
    *,
    post_id: str,
    kind: str,
    platform: str,
    text: str,
    status: str = "pending",
    signal_id: str | None = None,
    run_id: str | None = None,
    hook_format: str | None = None,
    discord_message_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Create a new posts row. Idempotent on post_id via INSERT OR IGNORE."""

    get_db().execute(
        """
        INSERT OR IGNORE INTO posts (
            id, kind, signal_id, run_id, hook_format, platform, text,
            discord_message_id, status, created_at, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            post_id,
            kind,
            signal_id,
            run_id,
            hook_format,
            platform,
            text,
            discord_message_id,
            status,
            _utcnow_iso(),
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )
    get_db().commit()


def update_post_status(
    post_id: str,
    *,
    status: str,
    error: str | None = None,
    buffer_post_id: str | None = None,
    beehiiv_post_id: str | None = None,
    external_url: str | None = None,
    discord_reaction: str | None = None,
    approved: bool = False,
    posted: bool = False,
) -> None:
    """Promote a post through its status lifecycle. Each field updates only
    when explicitly provided; existing values are preserved otherwise."""

    now = _utcnow_iso()
    get_db().execute(
        """
        UPDATE posts
           SET status              = ?,
               error               = COALESCE(?, error),
               buffer_post_id      = COALESCE(?, buffer_post_id),
               beehiiv_post_id     = COALESCE(?, beehiiv_post_id),
               external_url        = COALESCE(?, external_url),
               discord_reaction    = COALESCE(?, discord_reaction),
               approved_at         = CASE WHEN ? THEN ? ELSE approved_at END,
               posted_at           = CASE WHEN ? THEN ? ELSE posted_at END
         WHERE id = ?
        """,
        (
            status,
            error,
            buffer_post_id,
            beehiiv_post_id,
            external_url,
            discord_reaction,
            1 if approved else 0,
            now,
            1 if posted else 0,
            now,
            post_id,
        ),
    )
    get_db().commit()


def pending_posts_for_run(run_id: str) -> list[dict[str, Any]]:
    """All `pending` posts attached to a run (typically created when the
    digest publishes). Used by dispatch worker to find what to post once a
    reaction shows up."""

    rows = get_db().execute(
        "SELECT * FROM posts WHERE run_id = ? AND status = 'pending' ORDER BY created_at ASC",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def posts_by_status(status: str, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT * FROM posts WHERE status = ? ORDER BY created_at DESC LIMIT ?",
        (status, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_post(post_id: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    return dict(row) if row else None


def has_post_for_signal(*, kind: str, signal_id: str) -> bool:
    row = get_db().execute(
        "SELECT 1 FROM posts WHERE kind = ? AND signal_id = ? LIMIT 1",
        (kind, signal_id),
    ).fetchone()
    return row is not None
