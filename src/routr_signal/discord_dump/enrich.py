"""No-network enrichment fallbacks for Discord dump crawl candidates."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from .crawl_queue import CrawlQueueItem
from .privacy import redact_sensitive_text
from .types import CrawlResult, ExtractedLink


FetchResult = tuple[str, str, bool]
Fetcher = Callable[[str], FetchResult]


def crawl_result_from_embed(link: ExtractedLink, embed: dict[str, Any]) -> CrawlResult:
    """Build a crawl result from Discord embed metadata without fetching the URL."""

    title = _optional_text(embed.get("title"))
    description = _optional_text(embed.get("description"))
    text = _join_text(title, description)
    return _result(
        link,
        status="metadata_only",
        content_type=_optional_text(embed.get("content_type")) or "discord_embed",
        title=title,
        text=text,
    )


def x_blocked_fallback(
    link: ExtractedLink,
    *,
    embed: dict[str, Any] | None = None,
    reason: str = "x crawl blocked",
) -> CrawlResult:
    """Record a failed X crawl while preserving available embed context."""

    title = _optional_text((embed or {}).get("title"))
    description = _optional_text((embed or {}).get("description"))
    return _result(
        link,
        status="failed",
        content_type="discord_embed" if embed else None,
        title=title,
        text=_join_text(title, description),
        failure_reason=reason,
    )


def youtube_transcript_unavailable(
    link: ExtractedLink,
    *,
    embed: dict[str, Any] | None = None,
) -> CrawlResult:
    """Record YouTube metadata when transcript retrieval is unavailable."""

    title = _optional_text((embed or {}).get("title"))
    description = _optional_text((embed or {}).get("description"))
    return _result(
        link,
        status="metadata_only",
        content_type="youtube_metadata",
        title=title,
        text=_join_text(title, description),
        failure_reason="youtube transcript unavailable",
    )


def enrich_dry_run(queue: list[CrawlQueueItem]) -> list[CrawlResult]:
    """Return one skipped result per queue item without network or model calls."""

    return [
        _result(
            item.link,
            status="skipped",
            failure_reason="dry run: live crawl disabled",
        )
        for item in queue
    ]


def enrich_live(queue: list[CrawlQueueItem], *, fetcher: Fetcher) -> list[CrawlResult]:
    """Enrich queue items, attempting fetches before falling back.

    The fetcher is injected so tests can prove we attempt live enrichment without
    making network calls. Live HTTP/Playwright wiring can be added behind CLI flags
    later without changing fallback semantics.
    """

    results: list[CrawlResult] = []
    for item in queue:
        link = item.link
        if link.domain_class == "x":
            results.append(x_blocked_fallback(link, reason="x live fetcher not enabled"))
            continue
        if link.domain_class == "youtube":
            results.append(youtube_transcript_unavailable(link))
            continue

        try:
            content_type, body, truncated = fetcher(link.canonical_url)
        except Exception as e:  # noqa: BLE001
            results.append(
                _result(
                    link,
                    status="failed",
                    failure_reason=f"fetch failed: {e}",
                )
            )
            continue

        text = _html_to_text(body) if "html" in content_type.lower() else body
        results.append(
            _result(
                link,
                status="fetched",
                content_type=content_type,
                title=_extract_title(body) if "html" in content_type.lower() else None,
                text=text,
                truncated=truncated,
            )
        )
    return results


def summarize_enrichment_health(
    results: list[CrawlResult],
    *,
    max_fallback_rate: float = 0.75,
) -> dict[str, float | bool | int]:
    """Return aggregate health so fallback-heavy runs are visible."""

    total = len(results)
    fallback_count = sum(1 for result in results if result.status != "fetched")
    fallback_rate = fallback_count / total if total else 0.0
    return {
        "total": total,
        "fallback_count": fallback_count,
        "fallback_rate": fallback_rate,
        "healthy": fallback_rate <= max_fallback_rate,
    }


def _result(
    link: ExtractedLink,
    *,
    status: str,
    content_type: str | None = None,
    title: str | None = None,
    text: str = "",
    truncated: bool = False,
    failure_reason: str | None = None,
) -> CrawlResult:
    safe_text = redact_sensitive_text(text)
    return CrawlResult(
        source_url=link.raw_url,
        canonical_url=link.canonical_url,
        source_message_id=link.source_message_id,
        domain_class=link.domain_class,
        status=status,
        content_type=content_type,
        title=redact_sensitive_text(title) if title else None,
        text=safe_text,
        text_hash=_short_hash(safe_text) if safe_text else None,
        truncated=truncated,
        failure_reason=failure_reason,
    )


def _join_text(*parts: str | None) -> str:
    return "\n".join(part for part in parts if part)


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _extract_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    if not match:
        return None
    return _html_to_text(match.group(1)) or None


def _html_to_text(html: str) -> str:
    without_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", without_scripts)
    return re.sub(r"\s+", " ", text).strip()
