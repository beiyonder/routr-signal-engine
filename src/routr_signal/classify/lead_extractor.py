"""Turn classified pain signals into Lead records suitable for outbound.

We prefer the structured `lead_handle`/`lead_platform` Claude already produced in
pain_signal classification — saves a second Claude call. If those are missing but the
RawItem has an author, we fall back to that.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..lib.types import ClassifiedItem, Lead


def extract(signals: list[ClassifiedItem], *, min_score: float = 0.5) -> list[Lead]:
    """Convert relevant + scored signals into Leads. Dedupe per (platform, handle)."""

    seen: set[tuple[str, str]] = set()
    leads: list[Lead] = []

    for c in signals:
        if not c.relevant or c.score < min_score:
            continue

        handle = c.lead_handle or c.raw.author
        if not handle:
            continue

        platform = c.lead_platform or c.raw.source
        key = (platform or "other", handle.lower())
        if key in seen:
            continue
        seen.add(key)

        pain = c.pain_summary or c.raw.title[:200]
        angle = c.suggested_angle or "Cold open with their stated pain; no product mention."

        leads.append(
            Lead(
                source_id=c.raw.id,
                handle=handle,
                platform=platform or "other",
                profile_url=_profile_url(platform, handle),
                pain_in_their_words=pain,
                pitch_angle=angle,
                first_seen_at=datetime.now(timezone.utc),
            )
        )

    return leads


def _profile_url(platform: str | None, handle: str) -> str | None:
    h = handle.lstrip("@").lstrip("u/").lstrip("/")
    match platform:
        case "hn":
            return f"https://news.ycombinator.com/user?id={h}"
        case "reddit":
            return f"https://www.reddit.com/user/{h}"
        case "github":
            return f"https://github.com/{h}"
        case "x":
            return f"https://x.com/{h}"
        case _:
            return None
