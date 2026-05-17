"""HuggingFace Papers source.

Queries the undocumented but stable `/api/daily_papers` JSON endpoint that
backs https://huggingface.co/papers. Each response is a list of paper objects
with title, summary (abstract), arxiv id, upvotes, and submission timestamp.

We walk back N days (config: `lookback_days`), dedupe across days, optionally
filter on title/abstract substrings, and emit RawItems with `source="hf"`.

The Platform literal `"hf"` already exists in lib/types.py so downstream
classifier / drafter / dashboard consumers handle these uniformly.

Item id pattern: `hf-<arxiv-id-or-paper-id>`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..lib import http
from ..lib.config import hf_papers as hf_config
from ..lib.dedupe import SeenStore
from ..lib.filters import prefilter
from ..lib.logging import debug, info, warn
from ..lib.types import RawItem


SOURCE = "hf"
API_BASE = "https://huggingface.co/api/daily_papers"


def fetch() -> list[RawItem]:
    cfg = hf_config()
    if not cfg:
        debug("hf_papers: no config (config/hf_papers.yaml absent), skipping")
        return []

    lookback_days = int(cfg.get("lookback_days", 3))
    max_items_per_run = int(cfg.get("max_items_per_run", 40))
    must_match = [s.lower() for s in cfg.get("title_or_abstract_must_match_any", []) or []]
    ua = cfg.get(
        "user_agent",
        "routr-signal-engine/0.1 (+https://github.com/beiyonder/routr-signal-engine)",
    )
    throttle = float(cfg.get("min_seconds_between_requests", 1.5))
    http.set_budget("huggingface.co", throttle)

    headers = {
        "User-Agent": ua,
        "Accept": "application/json",
    }

    seen = SeenStore(SOURCE)
    collected: list[RawItem] = []
    seen_paper_ids: set[str] = set()  # within-run dedupe across overlapping day windows

    # Walk today backwards. Today's papers may not be published yet; the
    # endpoint silently returns [] for empty days.
    today = datetime.now(timezone.utc).date()
    for offset in range(lookback_days):
        day = today - timedelta(days=offset)
        params = {"date": day.isoformat()}
        try:
            resp = http.get(API_BASE, params=params, headers=headers, timeout=20.0, max_retries=2)
        except Exception as e:  # noqa: BLE001
            warn(f"hf_papers: request for {day.isoformat()} failed: {e}")
            continue

        try:
            payload = resp.json()
        except ValueError:
            warn(f"hf_papers: non-JSON response for {day.isoformat()}: {resp.text[:200]!r}")
            continue

        if not isinstance(payload, list):
            debug(f"hf_papers: unexpected payload type for {day.isoformat()}: {type(payload).__name__}")
            continue

        for entry in payload:
            if len(collected) >= max_items_per_run:
                break
            item = _entry_to_item(entry, query_date=day.isoformat())
            if item is None:
                continue
            if item.id in seen_paper_ids:
                continue
            seen_paper_ids.add(item.id)
            if seen.has(item.id):
                continue
            # Optional in-source filter before passing to global prefilter.
            if must_match:
                haystack = (item.title + "\n" + item.body).lower()
                if not any(p in haystack for p in must_match):
                    continue
            seen.add_item(item)
            collected.append(item)

        if len(collected) >= max_items_per_run:
            break

    filtered = prefilter(collected)
    info(f"hf_papers: {len(collected)} new items, {len(filtered)} pass keyword prefilter")
    seen.save()
    return filtered


def _entry_to_item(entry: dict[str, Any], *, query_date: str) -> RawItem | None:
    """Convert one daily-papers entry into a RawItem. Returns None if unparseable."""

    if not isinstance(entry, dict):
        return None

    paper = entry.get("paper") if isinstance(entry.get("paper"), dict) else entry
    if not isinstance(paper, dict):
        return None

    paper_id = paper.get("id") or paper.get("arxiv_id") or paper.get("paperId")
    if not paper_id or not isinstance(paper_id, str):
        return None

    title = (paper.get("title") or "").strip()
    summary = (paper.get("summary") or paper.get("abstract") or "").strip()
    if not title:
        return None

    authors_raw = paper.get("authors") or []
    author_names: list[str] = []
    if isinstance(authors_raw, list):
        for a in authors_raw:
            if isinstance(a, dict):
                name = a.get("name") or a.get("fullname") or a.get("user", {}).get("fullname")
                if isinstance(name, str) and name.strip():
                    author_names.append(name.strip())
            elif isinstance(a, str):
                author_names.append(a.strip())
    author = author_names[0] if author_names else None
    upvotes = paper.get("upvotes")
    submitted_at = (
        paper.get("submittedOnDailyAt")
        or paper.get("publishedAt")
        or paper.get("date")
        or entry.get("publishedAt")
    )

    created_at = _parse_dt(submitted_at) or datetime.now(timezone.utc)

    return RawItem(
        id=f"{SOURCE}-{paper_id}",
        source=SOURCE,
        title=title,
        body=summary,
        url=f"https://huggingface.co/papers/{paper_id}",
        author=author,
        created_at=created_at,
        extra={
            "arxiv_id": paper_id,
            "upvotes": upvotes,
            "authors": author_names,
            "query_date": query_date,
        },
    )


def _parse_dt(s: Any) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    # HF sometimes returns Z-suffixed ISO timestamps; fromisoformat handles them on 3.11+
    try:
        # Replace `Z` with explicit UTC offset for compatibility.
        normalized = s.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


if __name__ == "__main__":
    import json as _json

    out = fetch()
    print(_json.dumps([it.to_dict() for it in out], indent=2))
