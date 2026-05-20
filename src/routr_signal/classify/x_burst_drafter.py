"""Draft N standalone X posts from recent classified signals.

Sibling to `post_drafter.py`, but produces X-only output (no LinkedIn /
Reddit / HN / Dev.to) for the auto-shipping burst pipeline.

The drafter is invoked by `tasks/x_burst.py`. It reuses the global
post_drafter voice rules via a sibling system prompt at
`config/prompts/x_burst.md`, which adds:

  1. Output schema: `{"posts": [{"anchor_signal_id":..., "text":...}, ...]}`
  2. X Premium length allowance (up to 25,000 chars).
  3. "Natural voice" allowance (lowercase starts, contractions, the odd
     typo) — explicitly OPT-IN here, NOT in the digest drafter, because
     the digest is for the operator to review and the burst is for
     direct shipping to a public audience where "looks human" matters.
"""

from __future__ import annotations

import json
from typing import Any

from ..lib.config import prompt
from ..lib.logging import info, warn
from ..lib.types import PostHook


SYSTEM_PROMPT_NAME = "x_burst"
TOP_N_SIGNALS = 12
DEFAULT_COUNT = 2


def draft_x_burst(
    top_signals: list[dict[str, Any]],
    *,
    count: int = DEFAULT_COUNT,
    topic_frequency: dict[str, int] | None = None,
    excluded_signal_ids: set[str] | None = None,
) -> list[PostHook]:
    """Return up to `count` standalone X posts as PostHook(format='x_thread').

    `top_signals` is the result of `signal_store.recent_classified_for_drafting`
    (row dicts). We slice to TOP_N_SIGNALS for the payload to keep the
    drafter prompt under token budget.

    `topic_frequency` is the same dict the daily drafter receives:
    `{topic: count_last_7_days}`. The prompt tells the model to avoid
    re-covering saturated angles unless it has a new data point.

    `excluded_signal_ids` is the set of signal IDs we've already drafted
    against earlier today; we surface them in the payload so the drafter
    skips them.

    Returns `[]` on drafter failure (caller logs and exits).
    """

    if count <= 0:
        return []

    payload = _render_payload(
        top_signals[:TOP_N_SIGNALS],
        count=count,
        topic_frequency=topic_frequency,
        excluded_signal_ids=excluded_signal_ids,
    )
    system = prompt(SYSTEM_PROMPT_NAME)

    from .client import call_json

    try:
        response = call_json(system=system, user=payload, role="drafter")
    except Exception as e:  # noqa: BLE001
        warn(f"x_burst_drafter.draft: drafter call failed: {e}")
        return []

    posts = _parse_response(response, requested=count)
    info(f"x_burst_drafter: drafted {len(posts)} of {count} requested X posts")
    return posts


def _render_payload(
    signals: list[dict[str, Any]],
    *,
    count: int,
    topic_frequency: dict[str, int] | None,
    excluded_signal_ids: set[str] | None,
) -> str:
    payload: dict[str, Any] = {
        "COUNT": count,
        "recent_top_signals": [
            {
                "id": s.get("id"),
                "source": s.get("source"),
                "title": (s.get("title") or "")[:200],
                "body_excerpt": (s.get("body") or "")[:600],
                "url": s.get("url"),
                "topics": _safe_json_list(s.get("llm_topics")),
                "score": round(float(s.get("combined_score") or 0.0), 2),
                "engagement_angle": s.get("llm_engagement_angle") or "",
                "pain_summary": s.get("llm_pain_summary") or "",
            }
            for s in signals
        ],
        "topic_frequency_last_7_days": topic_frequency or {},
        "signal_ids_already_posted_today": sorted(excluded_signal_ids or []),
    }
    return (
        f"Generate exactly {count} standalone X posts as specified in the system prompt. "
        "Each post is a COMPLETE STANDALONE unit; no cliffhangers, no thread affectations. "
        "Anchor each post to one of the recent_top_signals when possible by populating "
        "anchor_signal_id with the matching id; if no signal fits, leave anchor_signal_id "
        "null and ground the post in long-running topics.\n\n"
        "Vary post length within the burst (one short, one mid is a good default). "
        "Use topic_frequency_last_7_days to avoid re-covering saturated angles. "
        "Do NOT anchor to any id in signal_ids_already_posted_today.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _parse_response(response: dict[str, Any], *, requested: int) -> list[PostHook]:
    raw_posts = response.get("posts")
    if not isinstance(raw_posts, list):
        return []

    out: list[PostHook] = []
    for entry in raw_posts:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        anchor = entry.get("anchor_signal_id")
        if anchor is not None and not isinstance(anchor, str):
            anchor = None
        out.append(PostHook(format="x_thread", anchor_signal_id=anchor, text=text.strip()))
        if len(out) >= requested:
            break
    return out


def _safe_json_list(blob: Any) -> list[str]:
    """The llm_topics column is stored as a JSON-string list. Decode safely."""

    if not blob:
        return []
    if isinstance(blob, list):
        return [t for t in blob if isinstance(t, str)]
    if isinstance(blob, str):
        try:
            parsed = json.loads(blob)
        except (ValueError, json.JSONDecodeError):
            return []
        return [t for t in parsed if isinstance(t, str)] if isinstance(parsed, list) else []
    return []
