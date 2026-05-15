"""Draft 5 post hooks from today's top signals via Claude.

Output formats: x_thread, linkedin, reddit, hn_comment, devto_title.
"""

from __future__ import annotations

import json
from typing import Any, get_args

from ..lib.config import prompt
from ..lib.logging import info, warn
from ..lib.types import ClassifiedItem, HookFormat, PostHook


SYSTEM_PROMPT_NAME = "post_drafter"
TOP_N_SIGNALS = 12


def draft(top_signals: list[ClassifiedItem]) -> tuple[list[PostHook], bool]:
    """Return (hooks, low_signal_day).

    The drafter always returns 5 hooks; on a low-signal day they're tied to the long-running
    wedges rather than to specific items.
    """

    payload = _render_payload(top_signals[:TOP_N_SIGNALS])
    system = prompt(SYSTEM_PROMPT_NAME)

    from .client import call_json

    try:
        # Flagship model for drafts; classifier stays cheap.
        response = call_json(system=system, user=payload, role="drafter")
    except Exception as e:  # noqa: BLE001
        warn(f"post_drafter.draft: drafter call failed: {e}")
        return ([], True)

    hooks = _parse_response(response)
    low_signal = bool(response.get("low_signal_day", False))
    info(f"post_drafter: drafted {len(hooks)} hooks (low_signal_day={low_signal})")
    return (hooks, low_signal)


def _render_payload(signals: list[ClassifiedItem]) -> str:
    payload = {
        "todays_top_signals": [
            {
                "id": c.raw.id,
                "source": c.raw.source,
                "title": c.raw.title[:200],
                "body_excerpt": c.raw.body[:600],
                "url": c.raw.url,
                "wedge": c.wedge,
                "score": round(c.score, 2),
                "pain_summary": c.pain_summary,
                "suggested_angle": c.suggested_angle,
            }
            for c in signals
        ]
    }
    return (
        "Generate exactly five post hooks (x_thread, linkedin, reddit, hn_comment, devto_title) "
        "as specified in the system prompt. Anchor each hook to one of today's signals when "
        "possible by populating anchor_signal_id with the matching id; if none fit, leave "
        "anchor_signal_id null and set low_signal_day true.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _parse_response(response: dict[str, Any]) -> list[PostHook]:
    raw_hooks = response.get("hooks")
    if not isinstance(raw_hooks, list):
        return []

    valid_formats = set(get_args(HookFormat))
    out: list[PostHook] = []
    seen_formats: set[str] = set()
    for entry in raw_hooks:
        if not isinstance(entry, dict):
            continue
        fmt = entry.get("format")
        if fmt not in valid_formats or fmt in seen_formats:
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        anchor = entry.get("anchor_signal_id")
        if anchor is not None and not isinstance(anchor, str):
            anchor = None
        out.append(PostHook(format=fmt, anchor_signal_id=anchor, text=text.strip()))
        seen_formats.add(fmt)
    return out
