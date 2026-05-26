"""Typed records for private Discord dump analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


RecordKind = Literal["discord_message", "lead_record", "unsupported"]
DomainClass = Literal[
    "x",
    "youtube",
    "github",
    "arxiv",
    "huggingface",
    "discord_private",
    "media",
    "web",
]


@dataclass(slots=True)
class NormalizedMessage:
    message_id: str
    channel_id: str
    content: str
    timestamp: str | None
    source_file: str
    author_id: str | None = None
    author_username: str | None = None
    author_global_name: str | None = None
    thread_id: str | None = None
    thread_name: str | None = None
    embeds: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    referenced_message_id: str | None = None


@dataclass(slots=True)
class NormalizedLeadRecord:
    source_file: str
    raw: dict[str, Any]


@dataclass(slots=True)
class UnsupportedRecord:
    source_file: str
    reason: str
    raw_keys: list[str]


@dataclass(slots=True)
class ExtractedLink:
    raw_url: str
    canonical_url: str
    source_message_id: str
    source_field: str
    domain_class: DomainClass
    crawl_eligible: bool
    reason: str = ""


@dataclass(slots=True)
class LoadStats:
    files_seen: int = 0
    records_seen: int = 0
    discord_messages: int = 0
    lead_records: int = 0
    unsupported_records: int = 0
