"""Shared dataclasses used across sources, classifiers, and outputs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


Platform = Literal[
    "hn",
    "reddit",
    "github",
    "x",
    "discord",
    "devto",
    "hf",
    "newsletter",
    "other",
]
Wedge = Literal["cold_start", "markup", "self_host", "mcp", "reliability", "other"]
HookFormat = Literal["x_thread", "linkedin", "reddit", "hn_comment", "devto_title"]


@dataclass(slots=True)
class RawItem:
    """A single fetched item before any classification."""

    id: str                       # globally-unique id, prefixed by source: "hn-123", "reddit-abc"
    source: Platform
    title: str
    body: str                     # may be empty for some sources
    url: str                      # canonical link the human will click
    author: str | None            # username/handle if visible
    created_at: datetime          # in UTC
    extra: dict[str, Any] = field(default_factory=dict)  # source-specific noise

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.astimezone(timezone.utc).isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RawItem":
        ts = d["created_at"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return cls(
            id=d["id"],
            source=d["source"],
            title=d["title"],
            body=d.get("body", ""),
            url=d["url"],
            author=d.get("author"),
            created_at=ts,
            extra=d.get("extra", {}),
        )


@dataclass(slots=True)
class ClassifiedItem:
    """A RawItem after the LLM classifier scored it.

    `score` is the LLM-derived relevance in [0, 1].
    `cosine_score` is the deterministic embedding-cosine score, read from `raw.extra`.
    `combined_score` is what the pipeline ranks by; computed in `combined()`.
    """

    raw: RawItem
    relevant: bool
    score: float
    wedge: Wedge
    pain_summary: str | None
    suggested_angle: str | None
    lead_handle: str | None
    lead_platform: Platform | None
    do_not_engage_reason: str | None = None

    @property
    def cosine_score(self) -> float:
        v = self.raw.extra.get("cosine_score", 0.0)
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    @property
    def combined_score(self) -> float:
        """0.6 * LLM score + 0.4 * cosine, both already in [0, 1]."""

        return round(0.6 * self.score + 0.4 * min(1.0, max(0.0, self.cosine_score)), 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw.to_dict(),
            "relevant": self.relevant,
            "score": self.score,
            "cosine_score": self.cosine_score,
            "combined_score": self.combined_score,
            "wedge": self.wedge,
            "pain_summary": self.pain_summary,
            "suggested_angle": self.suggested_angle,
            "lead_handle": self.lead_handle,
            "lead_platform": self.lead_platform,
            "do_not_engage_reason": self.do_not_engage_reason,
        }


@dataclass(slots=True)
class PostHook:
    """One of the 5 draft hooks produced by the post drafter."""

    format: HookFormat
    anchor_signal_id: str | None
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Lead:
    """A qualified lead, ready to be appended to data/leads/queue.jsonl."""

    source_id: str
    handle: str
    platform: Platform
    profile_url: str | None
    pain_in_their_words: str
    pitch_angle: str
    first_seen_at: datetime

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["first_seen_at"] = self.first_seen_at.astimezone(timezone.utc).isoformat()
        return d


@dataclass(slots=True)
class Digest:
    """The composed daily digest before it's rendered for any specific output."""

    date: str                     # YYYY-MM-DD UTC
    pain_signals: list[ClassifiedItem] = field(default_factory=list)
    active_accounts: list[Lead] = field(default_factory=list)
    hooks: list[PostHook] = field(default_factory=list)
    source_counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # warnings, low-signal-day, etc.

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "pain_signals": [c.to_dict() for c in self.pain_signals],
            "active_accounts": [l.to_dict() for l in self.active_accounts],
            "hooks": [h.to_dict() for h in self.hooks],
            "source_counts": self.source_counts,
            "notes": self.notes,
        }
