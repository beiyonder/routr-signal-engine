"""Build a bounded, deterministic crawl queue from extracted links."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .types import ExtractedLink


@dataclass(frozen=True, slots=True)
class CrawlLimits:
    max_urls: int = 1000
    max_urls_per_message: int = 3
    max_urls_per_domain_per_message: int = 1
    default_domain_cap: int = 2
    domain_caps: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CrawlQueueItem:
    link: ExtractedLink
    domain: str
    priority: int


_CLASS_PRIORITY = {
    "github": 10,
    "arxiv": 20,
    "huggingface": 30,
    "x": 35,
    "web": 40,
    "youtube": 50,
}


def build_crawl_queue(
    links: list[ExtractedLink],
    *,
    limits: CrawlLimits | None = None,
) -> list[CrawlQueueItem]:
    """Return selected links after global, per-message, and per-domain caps."""

    active_limits = limits or CrawlLimits()
    selected: list[CrawlQueueItem] = []
    seen_urls: set[str] = set()
    message_counts: dict[str, int] = defaultdict(int)
    message_domain_counts: dict[tuple[str, str], int] = defaultdict(int)
    domain_counts: dict[str, int] = defaultdict(int)

    for link in sorted(links, key=_sort_key):
        if len(selected) >= active_limits.max_urls:
            break
        if not link.crawl_eligible:
            continue
        if link.canonical_url in seen_urls:
            continue

        domain = _domain(link.canonical_url)
        if not domain:
            continue
        if message_counts[link.source_message_id] >= active_limits.max_urls_per_message:
            continue
        msg_domain_key = (link.source_message_id, domain)
        if message_domain_counts[msg_domain_key] >= active_limits.max_urls_per_domain_per_message:
            continue
        domain_cap = active_limits.domain_caps.get(domain, active_limits.default_domain_cap)
        if domain_counts[domain] >= domain_cap:
            continue

        item = CrawlQueueItem(link=link, domain=domain, priority=_priority(link))
        selected.append(item)
        seen_urls.add(link.canonical_url)
        message_counts[link.source_message_id] += 1
        message_domain_counts[msg_domain_key] += 1
        domain_counts[domain] += 1

    return selected


def _sort_key(link: ExtractedLink) -> tuple[int, str, str, str]:
    return (_priority(link), link.source_message_id, _domain(link.canonical_url), link.canonical_url)


def _priority(link: ExtractedLink) -> int:
    return _CLASS_PRIORITY.get(link.domain_class, 100)


def _domain(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host
