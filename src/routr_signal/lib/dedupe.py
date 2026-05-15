"""Simple persistent set of seen item IDs per source.

Each source writes to data/seen/<source>.json. The file is a JSON object:

    {
      "seen": ["hn-1", "hn-2", ...],
      "updated_at": "2026-05-13T07:00:00Z"
    }

We keep at most `MAX_SEEN` ids per source to bound the file size. The oldest are dropped
first (FIFO), but since we add in chronological order, the trailing tail is what we keep.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .paths import seen_dir


MAX_SEEN = 5000


class SeenStore:
    def __init__(self, source: str) -> None:
        self.source = source
        self.path = seen_dir() / f"{source}.json"
        self._set: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._set = set()
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self._set = set(payload.get("seen", []))
        except (json.JSONDecodeError, OSError):
            # Corrupt file shouldn't break the pipeline. Reset.
            self._set = set()

    def has(self, item_id: str) -> bool:
        return item_id in self._set

    def add(self, item_id: str) -> None:
        self._set.add(item_id)

    def add_many(self, ids: list[str]) -> None:
        self._set.update(ids)

    def save(self) -> None:
        # Bound the set. We don't preserve insertion order (Python sets are insertion-ordered
        # for small sets but not contract), so we just sort lexicographically and keep last N.
        # Item ids are time-prefixed for HN/Reddit; lexicographic sort approximates chronological.
        if len(self._set) > MAX_SEEN:
            kept = sorted(self._set)[-MAX_SEEN:]
            self._set = set(kept)

        payload = {
            "seen": sorted(self._set),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def __len__(self) -> int:
        return len(self._set)
