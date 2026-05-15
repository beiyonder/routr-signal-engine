"""User-intelligence classifier: batch RawItems → ClassifiedItems via cheap LLM.

The classifier is intentionally on the cheap tier (Haiku 4.5 / Gemini Flash). It is run
on every item that survives the cosine prefilter. It is **not** the place for prose
generation — that happens in `post_drafter.py` with a flagship model.

Output shape from the LLM (per `config/prompts/pain_signal_classifier.md`):

```json
{
  "items": [
    {
      "id": "...",
      "relevant": true,
      "score": 0.85,
      "topics": ["multi_provider", "failover"],
      "person": {
        "handle": "...",
        "platform": "hn",
        "snapshot": "...",
        "seriousness": 0.75
      },
      "pain_summary": "...",
      "engagement_angle": "...",
      "do_not_engage_reason": null
    }
  ]
}
```

We map this onto `ClassifiedItem`, keeping the legacy `wedge` field for backward-compat
by inferring it from `topics[0]` (mapped through a small table). `topics` itself is
stored in `extra` for downstream use (dashboard, drafter).
"""

from __future__ import annotations

import json
from typing import Any, get_args

from ..lib.config import prompt
from ..lib.logging import info, warn
from ..lib.types import ClassifiedItem, Platform, RawItem, Wedge


SYSTEM_PROMPT_NAME = "pain_signal_classifier"
MAX_ITEMS_PER_CALL = 30


# Map new topic taxonomy → legacy Wedge enum (for ClassifiedItem.wedge).
# Multiple topics may collapse to one wedge; we take the first match.
TOPIC_TO_WEDGE: dict[str, Wedge] = {
    "cold_start": "cold_start",
    "latency": "cold_start",
    "cost_attribution": "markup",
    "self_host": "self_host",
    "security": "self_host",
    "mcp": "mcp",
    "agent_reliability": "reliability",
    "failover": "reliability",
    "observability": "reliability",
    "multi_provider": "other",
    "caching": "other",
    "routing": "other",
    "benchmarks": "other",
    "model_release": "other",
    "community": "other",
    "other": "other",
}


def classify(items: list[RawItem]) -> list[ClassifiedItem]:
    """Classify items in chunks, returning ALL items (relevant + irrelevant) with scores."""

    if not items:
        return []

    classified: list[ClassifiedItem] = []
    system = prompt(SYSTEM_PROMPT_NAME)
    by_id = {it.id: it for it in items}

    for chunk_start in range(0, len(items), MAX_ITEMS_PER_CALL):
        chunk = items[chunk_start : chunk_start + MAX_ITEMS_PER_CALL]
        user = _render_user_payload(chunk)

        from .client import call_json

        try:
            response = call_json(system=system, user=user, role="classifier")
        except Exception as e:  # noqa: BLE001
            warn(f"pain_signal.classify: classifier call failed for chunk of {len(chunk)}: {e}")
            classified.extend(_unclassified_fallback(chunk))
            continue

        chunk_classified = _parse_response(response, by_id)
        classified.extend(chunk_classified)
        info(
            f"pain_signal: chunk of {len(chunk)} -> "
            f"{sum(1 for c in chunk_classified if c.relevant)} relevant"
        )

    return classified


def _render_user_payload(chunk: list[RawItem]) -> str:
    payload = {
        "items": [
            {
                "id": it.id,
                "source": it.source,
                "title": it.title[:300],
                "body": it.body[:1500],
                "author": it.author,
                "url": it.url,
                "created_at": it.created_at.isoformat(),
            }
            for it in chunk
        ]
    }
    return (
        "Classify the following items per the user-intelligence schema in the system prompt. "
        "Return strict JSON. Pass each id through unchanged.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _parse_response(
    response: dict[str, Any], by_id: dict[str, RawItem]
) -> list[ClassifiedItem]:
    out_items = response.get("items")
    if not isinstance(out_items, list):
        warn("pain_signal: classifier response missing 'items' array; treating as empty.")
        return []

    valid_platforms = set(get_args(Platform))
    classified: list[ClassifiedItem] = []
    for entry in out_items:
        if not isinstance(entry, dict):
            continue
        item_id = entry.get("id")
        if not item_id or item_id not in by_id:
            continue
        raw = by_id[item_id]

        topics_raw = entry.get("topics") or []
        topics = [t for t in topics_raw if isinstance(t, str)] if isinstance(topics_raw, list) else []
        if not topics:
            topics = ["other"]

        # Derive legacy wedge from the first known topic.
        wedge: Wedge = "other"
        for t in topics:
            if t in TOPIC_TO_WEDGE:
                wedge = TOPIC_TO_WEDGE[t]
                break

        person = entry.get("person") or {}
        if not isinstance(person, dict):
            person = {}

        lead_handle = _clean_str(person.get("handle")) or _clean_str(entry.get("lead_handle"))
        lead_platform_raw = person.get("platform") or entry.get("lead_platform")
        lead_platform: Platform | None = (
            lead_platform_raw if lead_platform_raw in valid_platforms else None
        )

        try:
            score = float(entry.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0

        try:
            seriousness = float(person.get("seriousness", 0.0))
        except (TypeError, ValueError):
            seriousness = 0.0

        ci = ClassifiedItem(
            raw=raw,
            relevant=bool(entry.get("relevant", False)),
            score=max(0.0, min(1.0, score)),
            wedge=wedge,
            pain_summary=_clean_str(entry.get("pain_summary")),
            suggested_angle=_clean_str(entry.get("engagement_angle"))
            or _clean_str(entry.get("suggested_angle")),
            lead_handle=lead_handle,
            lead_platform=lead_platform,
            do_not_engage_reason=_clean_str(entry.get("do_not_engage_reason")),
        )
        # Stash topics + seriousness on the raw item's extra for downstream consumers
        # (dashboard, person aggregation).
        raw.extra.setdefault("topics", topics)
        raw.extra.setdefault("seriousness", max(0.0, min(1.0, seriousness)))
        raw.extra.setdefault("person_snapshot", _clean_str(person.get("snapshot")))
        classified.append(ci)
    return classified


def _unclassified_fallback(chunk: list[RawItem]) -> list[ClassifiedItem]:
    """When the classifier is down, mark items relevant with score 0 so the human still sees them."""

    return [
        ClassifiedItem(
            raw=it,
            relevant=True,
            score=0.0,
            wedge="other",
            pain_summary="[UNCLASSIFIED] LLM call failed; raw item preserved.",
            suggested_angle=None,
            lead_handle=it.author,
            lead_platform=it.source if it.source in {"hn", "reddit", "github"} else None,
            do_not_engage_reason=None,
        )
        for it in chunk
    ]


def _clean_str(v: Any) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        v = str(v)
    v = v.strip()
    return v or None
