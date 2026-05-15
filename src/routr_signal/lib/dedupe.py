"""SQLite-backed dedupe with the old SeenStore shape.

Pre-v3 we used a JSON file at data/seen/<source>.json. v3 moves dedupe into
the SQLite `signals` table -- the existence of a row IS the dedupe state.

API for sources (hn.py, reddit.py, github_issues.py, twitter.py,
discord_paste.py):

    seen = SeenStore("hn")
    if seen.has(item.id):     # primary key existence check (cached)
        continue
    seen.add_item(item)       # persist the full RawItem to signals + memo

`add(id)` is kept for back-compat with tests / callers that only have an id;
it tracks in memory but does NOT persist (a stub row is meaningless without
title/url/etc). Real persistence requires the full RawItem via add_item().
"""

from __future__ import annotations

from . import signal_store
from .types import RawItem


class SeenStore:
    """SQLite-backed dedupe with the old JSON-store API plus add_item()."""

    def __init__(self, source: str) -> None:
        self.source = source
        # Pre-load the seen-set into memory for O(1) has() during a source's
        # fetch loop. Acceptable cost: a source touches at most a few thousand
        # of its own ids per run.
        self._mem: set[str] = signal_store.already_seen_in(source)

    def has(self, item_id: str) -> bool:
        if item_id in self._mem:
            return True
        if signal_store.is_seen(item_id):
            self._mem.add(item_id)
            return True
        return False

    def add(self, item_id: str) -> None:
        """Track in memory only. For full persistence, use add_item()."""

        self._mem.add(item_id)

    def add_many(self, ids: list[str]) -> None:
        self._mem.update(ids)

    def add_item(self, item: RawItem) -> bool:
        """Persist the full RawItem to the signals table AND remember it.

        Returns True if newly inserted, False if it was already there.
        """

        self._mem.add(item.id)
        return signal_store.upsert_fetched(item, run_id=None)

    def save(self) -> None:
        # No-op; SQLite commits are immediate inside signal_store.
        return

    def __len__(self) -> int:
        return len(self._mem)
