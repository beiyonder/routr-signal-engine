from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..lib.config import prompt
from ..lib.logging import info, warn
from ..lib.types import RawItem


SYSTEM_PROMPT_NAME = "x_reply_scorer"


@dataclass(slots=True)
class ReplyOpportunity:
    signal_id: str
    score: float
    reason: str
    reply_angle: str
    suggested_reply: str


def score(
    items: list[RawItem],
    *,
    account_meta: dict[str, dict[str, Any]],
    min_score: float,
    limit: int,
) -> list[ReplyOpportunity]:
    if not items:
        return []

    payload = _render_payload(items, account_meta=account_meta)
    system = prompt(SYSTEM_PROMPT_NAME)

    from .client import call_json

    try:
        response = call_json(system=system, user=payload, role="classifier", max_tokens=4096)
    except Exception as e:  # noqa: BLE001
        warn(f"x_reply_scorer: scorer call failed: {e}")
        return []

    opportunities = _parse_response(response)
    opportunities = [o for o in opportunities if o.score >= min_score and o.suggested_reply]
    opportunities.sort(key=lambda o: o.score, reverse=True)
    out = opportunities[:limit]
    info(f"x_reply_scorer: {len(out)} opportunity/opportunities above {min_score}")
    return out


def _render_payload(items: list[RawItem], *, account_meta: dict[str, dict[str, Any]]) -> str:
    rows: list[dict[str, Any]] = []
    for it in items:
        handle = (it.author or "").lstrip("@").lower()
        meta = account_meta.get(handle, {})
        rows.append(
            {
                "id": it.id,
                "handle": it.author,
                "account_tier": meta.get("tier"),
                "account_tags": meta.get("tags", []),
                "created_at": it.created_at.isoformat(),
                "url": it.url,
                "text": it.body[:1200],
            }
        )

    return (
        "Score these fresh X posts for fast-reply opportunity. Return one entry per input id.\n\n"
        f"{json.dumps({'tweets': rows}, ensure_ascii=False)}"
    )


def _parse_response(response: dict[str, Any]) -> list[ReplyOpportunity]:
    raw = response.get("opportunities")
    if not isinstance(raw, list):
        return []

    out: list[ReplyOpportunity] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        signal_id = entry.get("id")
        if not isinstance(signal_id, str) or not signal_id:
            continue
        try:
            score_value = float(entry.get("score", 0.0))
        except (TypeError, ValueError):
            score_value = 0.0
        reason = entry.get("reason") if isinstance(entry.get("reason"), str) else ""
        angle = entry.get("reply_angle") if isinstance(entry.get("reply_angle"), str) else ""
        reply = entry.get("suggested_reply") if isinstance(entry.get("suggested_reply"), str) else ""
        out.append(
            ReplyOpportunity(
                signal_id=signal_id,
                score=max(0.0, min(1.0, score_value)),
                reason=reason.strip(),
                reply_angle=angle.strip(),
                suggested_reply=reply.strip(),
            )
        )
    return out
