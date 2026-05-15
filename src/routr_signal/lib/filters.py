"""Cheap pre-Claude filters: substring keyword match, suppress-word match."""

from __future__ import annotations

from .config import keyword_phrases, suppress_phrases
from .types import RawItem


def matches_any_keyword(item: RawItem) -> bool:
    """Return True if the item's title or body contains at least one configured keyword."""

    haystack = f"{item.title}\n{item.body}".lower()
    return any(phrase in haystack for phrase in keyword_phrases())


def is_suppressed(item: RawItem) -> bool:
    """Return True if the item contains a suppress phrase."""

    haystack = f"{item.title}\n{item.body}".lower()
    return any(phrase in haystack for phrase in suppress_phrases())


def prefilter(items: list[RawItem]) -> list[RawItem]:
    """Drop items that don't match any keyword or are suppressed."""

    kept: list[RawItem] = []
    for item in items:
        if is_suppressed(item):
            continue
        if not matches_any_keyword(item):
            continue
        kept.append(item)
    return kept
