"""Weekly synthesis: aggregate the last 7 days of signals into a single
publishable essay draft.

Runs as a separate cron (`.github/workflows/weekly-synthesis.yml`), not in
the daily pipeline. Output is:

  1. A JSON object with the dominant theme, contrarian read, prediction,
     and the actual essay draft.
  2. A Discord post containing the draft (so the user finalizes it in 30
     minutes).
  3. A `runs` row with `kind='synthesis'` and `digest_md` set to the
     rendered essay.
  4. A `posts` row with `kind='synthesis'`, `status='pending'`, ready to
     be promoted by the user pasting it elsewhere (Beehiiv newsletter,
     LinkedIn manually, etc.).

The model is the flagship drafter tier (Gemini 3 Pro by default), because
this is the one place where output quality matters most. The classifier
tier (Haiku) is also used here, for the lightweight "what's the dominant
theme" step that precedes the full draft.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..lib.config import prompt
from ..lib.db import get_db
from ..lib.logging import info, warn


SYSTEM_PROMPT_NAME = "weekly_synthesis"
LOOKBACK_DAYS = 7
TOP_SIGNALS = 10
MIN_COMBINED_SCORE = 0.55


@dataclass(slots=True)
class SynthesisResult:
    """Structured output of one synthesis run.

    `draft_post` is the actual essay. Everything else is the supporting
    structure that lives in the Discord message body so the user can see
    the reasoning before finalizing.
    """

    period: str                          # e.g. "2026-05-12..2026-05-18"
    dominant_theme: str
    evidence: list[dict[str, str]]       # [{signal_id, what_it_shows}, ...]
    contrarian_read: str
    where_this_goes: str
    draft_post: str
    routr_bridge: str
    top_signals: list[dict[str, Any]] = field(default_factory=list)
    topic_distribution: dict[str, int] = field(default_factory=dict)
    source_distribution: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "dominant_theme": self.dominant_theme,
            "evidence": self.evidence,
            "contrarian_read": self.contrarian_read,
            "where_this_goes": self.where_this_goes,
            "draft_post": self.draft_post,
            "routr_bridge": self.routr_bridge,
            "top_signals": self.top_signals,
            "topic_distribution": self.topic_distribution,
            "source_distribution": self.source_distribution,
        }


def synthesize(*, lookback_days: int = LOOKBACK_DAYS) -> SynthesisResult | None:
    """Aggregate the last `lookback_days` of signals and ask the drafter
    for a synthesis. Returns None if there's nothing to synthesize."""

    period_end = datetime.now(timezone.utc).date()
    period_start = period_end - timedelta(days=lookback_days)
    period = f"{period_start.isoformat()}..{period_end.isoformat()}"

    top_signals = _fetch_top_signals(period_start, period_end)
    if not top_signals:
        info(f"synthesize: no signals with combined_score >= {MIN_COMBINED_SCORE} in {period}; skipping")
        return None

    topic_distribution = _aggregate_topics(period_start, period_end)
    source_distribution = _aggregate_sources(period_start, period_end)

    payload = {
        "period": period,
        "top_signals": top_signals,
        "topic_distribution": topic_distribution,
        "source_distribution": source_distribution,
    }

    info(
        f"synthesize: drafting weekly essay for {period} "
        f"(top={len(top_signals)} topics={len(topic_distribution)} sources={sum(source_distribution.values())})"
    )

    from .client import call_json

    system = prompt(SYSTEM_PROMPT_NAME)
    user = (
        "Synthesize this week's signal cluster into a publishable essay per the "
        "system prompt. Return strict JSON only.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        response = call_json(system=system, user=user, role="drafter", max_tokens=4096)
    except Exception as e:  # noqa: BLE001
        warn(f"synthesize: drafter call failed: {e}")
        return None

    draft = _parse_response(response)
    if draft is None:
        warn("synthesize: drafter response did not match expected shape")
        return None

    return SynthesisResult(
        period=period,
        dominant_theme=draft["dominant_theme"],
        evidence=draft["evidence"],
        contrarian_read=draft["contrarian_read"],
        where_this_goes=draft["where_this_goes"],
        draft_post=draft["draft_post"],
        routr_bridge=draft["routr_bridge"],
        top_signals=top_signals,
        topic_distribution=topic_distribution,
        source_distribution=source_distribution,
    )


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------


def _fetch_top_signals(start_date: Any, end_date: Any) -> list[dict[str, Any]]:
    """Pull the top-N relevant signals from the period, ordered by combined_score."""

    sql = """
        SELECT id, source, author_handle, title, body, url, created_at,
               llm_score, cosine_score, combined_score, llm_topics,
               llm_pain_summary, llm_engagement_angle
          FROM signals
         WHERE llm_relevant = 1
           AND combined_score >= ?
           AND date(created_at) >= ?
           AND date(created_at) <= ?
         ORDER BY combined_score DESC NULLS LAST
         LIMIT ?
    """
    rows = get_db().execute(
        sql,
        (MIN_COMBINED_SCORE, start_date.isoformat(), end_date.isoformat(), TOP_SIGNALS),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            topics = json.loads(r["llm_topics"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            topics = []
        out.append(
            {
                "id": r["id"],
                "source": r["source"],
                "author": r["author_handle"],
                "title": (r["title"] or "")[:200],
                "body_excerpt": (r["body"] or "")[:500],
                "url": r["url"],
                "created_at": r["created_at"],
                "score": round(float(r["llm_score"] or 0.0), 3),
                "cosine": round(float(r["cosine_score"] or 0.0), 3),
                "combined_score": round(float(r["combined_score"] or 0.0), 3),
                "topics": topics,
                "pain_summary": r["llm_pain_summary"],
                "engagement_angle": r["llm_engagement_angle"],
            }
        )
    return out


def _aggregate_topics(start_date: Any, end_date: Any) -> dict[str, int]:
    """Count how often each topic appears across relevant signals this week."""

    rows = get_db().execute(
        """
        SELECT llm_topics FROM signals
         WHERE llm_relevant = 1
           AND date(created_at) >= ?
           AND date(created_at) <= ?
        """,
        (start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    counter: Counter[str] = Counter()
    for r in rows:
        try:
            topics = json.loads(r["llm_topics"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            topics = []
        for t in topics:
            if isinstance(t, str):
                counter[t] += 1
    return dict(counter.most_common(20))


def _aggregate_sources(start_date: Any, end_date: Any) -> dict[str, int]:
    rows = get_db().execute(
        """
        SELECT source, COUNT(*) AS n FROM signals
         WHERE llm_relevant = 1
           AND date(created_at) >= ?
           AND date(created_at) <= ?
         GROUP BY source
        """,
        (start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    return {r["source"]: r["n"] for r in rows}


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


REQUIRED_FIELDS = (
    "dominant_theme",
    "contrarian_read",
    "where_this_goes",
    "draft_post",
)


def _parse_response(response: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the drafter's JSON. Returns a normalized dict or None."""

    for key in REQUIRED_FIELDS:
        v = response.get(key)
        if not isinstance(v, str) or not v.strip():
            warn(f"synthesize: response missing required field {key!r}")
            return None

    evidence_raw = response.get("evidence") or []
    evidence: list[dict[str, str]] = []
    if isinstance(evidence_raw, list):
        for e in evidence_raw:
            if not isinstance(e, dict):
                continue
            sid = e.get("signal_id")
            what = e.get("what_it_shows")
            if isinstance(sid, str) and isinstance(what, str):
                evidence.append({"signal_id": sid, "what_it_shows": what})

    return {
        "dominant_theme": response["dominant_theme"].strip(),
        "evidence": evidence,
        "contrarian_read": response["contrarian_read"].strip(),
        "where_this_goes": response["where_this_goes"].strip(),
        "draft_post": response["draft_post"].strip(),
        "routr_bridge": (response.get("routr_bridge") or "").strip(),
    }
