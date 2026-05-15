"""Append-only JSONL queue at data/leads/queue.jsonl."""

from __future__ import annotations

import json

from ..lib.logging import info
from ..lib.paths import leads_dir
from ..lib.types import Lead


def append(leads: list[Lead]) -> int:
    """Append leads to data/leads/queue.jsonl. Returns the number written."""

    if not leads:
        return 0

    path = leads_dir() / "queue.jsonl"
    written = 0
    with path.open("a", encoding="utf-8") as f:
        for lead in leads:
            f.write(json.dumps(lead.to_dict(), ensure_ascii=False) + "\n")
            written += 1

    info(f"leads_queue: appended {written} lead(s) → {path}")
    return written
