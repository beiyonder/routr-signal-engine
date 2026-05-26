"""Local JSON loader and normalizer for private Discord dump analysis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .privacy import redact_sensitive_text
from .types import LoadStats, NormalizedLeadRecord, NormalizedMessage, UnsupportedRecord


def normalize_record(
    record: dict[str, Any],
    *,
    source_file: str,
) -> NormalizedMessage | NormalizedLeadRecord | UnsupportedRecord:
    """Normalize one exported row without assuming a single global schema."""

    if _looks_like_discord_message(record):
        author = record.get("author") if isinstance(record.get("author"), dict) else {}
        thread = record.get("thread") if isinstance(record.get("thread"), dict) else {}
        reference = record.get("message_reference") if isinstance(record.get("message_reference"), dict) else {}
        return NormalizedMessage(
            message_id=str(record.get("id") or ""),
            channel_id=str(record.get("channel_id") or ""),
            content=redact_sensitive_text(str(record.get("content") or "")),
            timestamp=_optional_str(record.get("timestamp")),
            source_file=source_file,
            author_id=_optional_str(author.get("id")),
            author_username=_optional_str(author.get("username")),
            author_global_name=_optional_str(author.get("global_name")),
            thread_id=_optional_str(thread.get("id")),
            thread_name=_optional_str(thread.get("name")),
            embeds=_list_of_dicts(record.get("embeds")),
            attachments=_list_of_dicts(record.get("attachments")),
            referenced_message_id=_optional_str(reference.get("message_id")),
        )

    if _looks_like_lead_record(record):
        return NormalizedLeadRecord(source_file=source_file, raw=dict(record))

    return UnsupportedRecord(
        source_file=source_file,
        reason="unknown record shape",
        raw_keys=sorted(str(k) for k in record),
    )


def load_json_records(input_path: Path) -> tuple[list[NormalizedMessage | NormalizedLeadRecord | UnsupportedRecord], LoadStats]:
    """Load all JSON records under a path and return normalized records plus stats."""

    files = [input_path] if input_path.is_file() else sorted(input_path.glob("**/*.json"))
    stats = LoadStats(files_seen=len(files))
    out: list[NormalizedMessage | NormalizedLeadRecord | UnsupportedRecord] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        rows = _rows_from_json(data)
        for row in rows:
            if not isinstance(row, dict):
                continue
            stats.records_seen += 1
            normalized = normalize_record(row, source_file=str(path))
            out.append(normalized)
            if isinstance(normalized, NormalizedMessage):
                stats.discord_messages += 1
            elif isinstance(normalized, NormalizedLeadRecord):
                stats.lead_records += 1
            else:
                stats.unsupported_records += 1
    return out, stats


def _looks_like_discord_message(record: dict[str, Any]) -> bool:
    return "id" in record and "channel_id" in record and "content" in record


def _looks_like_lead_record(record: dict[str, Any]) -> bool:
    lead_keys = {"repo_full_name", "html_url", "lead_score", "owner", "twitter", "company"}
    return bool(lead_keys.intersection(record))


def _rows_from_json(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("messages", "data", "records", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
