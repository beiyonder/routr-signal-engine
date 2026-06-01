"""Private Discord dump analyzer CLI."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..discord_dump.crawl_queue import CrawlLimits, build_crawl_queue
from ..discord_dump.enrich import Fetcher, enrich_dry_run, enrich_live, summarize_enrichment_health
from ..discord_dump.links import extract_links_from_message
from ..discord_dump.loader import load_json_records
from ..discord_dump.types import NormalizedLeadRecord, NormalizedMessage, UnsupportedRecord
from ..lib.logging import error, info


@dataclass(slots=True)
class DiscordDumpRunResult:
    output_dir: Path
    manifest: dict[str, Any]


def run_analysis(
    *,
    input_path: Path,
    output_root: Path,
    run_id: str | None = None,
    max_crawl_urls: int = 1000,
    dry_run: bool = True,
    fetcher: Fetcher | None = None,
) -> DiscordDumpRunResult:
    run = run_id or f"local-{uuid.uuid4().hex[:8]}"
    output_dir = output_root / run
    output_dir.mkdir(parents=True, exist_ok=True)

    records, stats = load_json_records(input_path)
    messages = [r for r in records if isinstance(r, NormalizedMessage)]
    leads = [r for r in records if isinstance(r, NormalizedLeadRecord)]
    unsupported = [r for r in records if isinstance(r, UnsupportedRecord)]

    links = []
    for message in messages:
        links.extend(extract_links_from_message(message))

    limits = CrawlLimits(
        max_urls=max_crawl_urls,
        max_urls_per_message=3,
        max_urls_per_domain_per_message=1,
        default_domain_cap=2,
        domain_caps={
            "github.com": 25,
            "arxiv.org": 25,
            "huggingface.co": 25,
            "x.com": 500,
            "youtube.com": 200,
        },
        class_caps={"x": 500, "youtube": 200, "github": 25, "arxiv": 25, "huggingface": 25},
    )
    queue = build_crawl_queue(links, limits=limits)
    crawl_results = enrich_dry_run(queue) if dry_run else enrich_live(queue, fetcher=fetcher or _fetch_url)
    health = summarize_enrichment_health(crawl_results)

    manifest: dict[str, Any] = {
        "run_id": run,
        "input_path": str(input_path),
        "output_dir": str(output_dir),
        "dry_run": dry_run,
        "files_seen": stats.files_seen,
        "records_seen": stats.records_seen,
        "discord_messages": len(messages),
        "lead_records": len(leads),
        "unsupported_records": len(unsupported),
        "crawl_eligible_links": len(links),
        "crawl_queue_size": len(queue),
        "crawl_results": len(crawl_results),
        "enrichment_health": health,
    }

    _write_json(output_dir / "run_manifest.json", manifest)
    _write_jsonl(output_dir / "messages.normalized.jsonl", messages)
    _write_jsonl(output_dir / "leads.normalized.jsonl", leads)
    _write_jsonl(output_dir / "unsupported.records.jsonl", unsupported)
    _write_jsonl(output_dir / "links.index.jsonl", links)
    _write_jsonl(output_dir / "crawl_queue.jsonl", [asdict(item) for item in queue])
    _write_jsonl(output_dir / "crawl_results.jsonl", crawl_results)
    message_rows = _message_rows(messages, links)
    link_rows = _link_rows(links, queue, crawl_results)
    person_rows = _person_rows(messages, links)
    _write_csv(output_dir / "messages.csv", message_rows)
    _write_csv(output_dir / "links.csv", link_rows)
    _write_csv(output_dir / "people.csv", person_rows)
    summary = _render_summary(manifest)
    (output_dir / "summary.md").write_text(summary, encoding="utf-8")
    brief = _render_operator_brief(manifest, person_rows, link_rows, message_rows)
    (output_dir / "operator_brief.md").write_text(brief, encoding="utf-8")
    return DiscordDumpRunResult(output_dir=output_dir, manifest=manifest)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _fetch_url(url: str) -> tuple[str, str, bool]:
    from ..lib import http

    resp = http.get(
        url,
        headers={"User-Agent": "routr-signal-discord-dump/0.1"},
        timeout=20.0,
        max_retries=1,
    )
    text = resp.text
    limit = 1_000_000
    return resp.headers.get("content-type", "text/plain"), text[:limit], len(text) > limit


def _write_jsonl(path: Path, rows: list[Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            data = asdict(row) if hasattr(row, "__dataclass_fields__") else row
            f.write(json.dumps(data, ensure_ascii=False) + "\n")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_value(row.get(k)) for k in fieldnames})


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _message_rows(
    messages: list[NormalizedMessage],
    links: list[Any],
) -> list[dict[str, Any]]:
    links_by_message: dict[str, list[str]] = defaultdict(list)
    for link in links:
        links_by_message[link.source_message_id].append(link.canonical_url)
    rows: list[dict[str, Any]] = []
    for msg in sorted(messages, key=lambda m: (m.timestamp or "", m.message_id)):
        author = _display_author(msg)
        msg_links = sorted(set(links_by_message.get(msg.message_id, [])))
        rows.append(
            {
                "message_id": msg.message_id,
                "timestamp": msg.timestamp or "",
                "author": author,
                "author_id": msg.author_id or "",
                "channel_id": msg.channel_id,
                "thread_name": msg.thread_name or "",
                "content_preview": _one_line(msg.content, 180),
                "content": msg.content,
                "link_count": len(msg_links),
                "links": msg_links,
                "source_file": msg.source_file,
            }
        )
    return rows


def _link_rows(
    links: list[Any],
    queue: list[Any],
    crawl_results: list[Any],
) -> list[dict[str, Any]]:
    queued = {item.link.canonical_url: item for item in queue}
    results = {result.canonical_url: result for result in crawl_results}
    rows: list[dict[str, Any]] = []
    for link in sorted(links, key=lambda l: (l.domain_class, l.source_message_id, l.canonical_url)):
        q = queued.get(link.canonical_url)
        result = results.get(link.canonical_url)
        rows.append(
            {
                "canonical_url": link.canonical_url,
                "raw_url": link.raw_url,
                "domain": _domain(link.canonical_url),
                "domain_class": link.domain_class,
                "source_message_id": link.source_message_id,
                "source_field": link.source_field,
                "queued": q is not None,
                "priority": q.priority if q is not None else "",
                "crawl_status": result.status if result is not None else "not_queued",
                "failure_reason": result.failure_reason if result is not None else "",
                "title": result.title if result is not None else "",
            }
        )
    return rows


def _person_rows(
    messages: list[NormalizedMessage],
    links: list[Any],
) -> list[dict[str, Any]]:
    links_by_message: dict[str, list[Any]] = defaultdict(list)
    for link in links:
        links_by_message[link.source_message_id].append(link)

    grouped: dict[str, dict[str, Any]] = {}
    for msg in messages:
        key = msg.author_id or (msg.author_username or msg.author_global_name or "unknown").lower()
        row = grouped.setdefault(
            key,
            {
                "person_key": key,
                "author": _display_author(msg),
                "author_id": msg.author_id or "",
                "message_count": 0,
                "first_seen": msg.timestamp or "",
                "last_seen": msg.timestamp or "",
                "channels": set(),
                "threads": set(),
                "link_count": 0,
                "top_domains": Counter(),
                "sample_message_ids": [],
                "sample_text": "",
            },
        )
        row["message_count"] += 1
        if msg.timestamp:
            row["first_seen"] = min([v for v in (row["first_seen"], msg.timestamp) if v])
            row["last_seen"] = max(row["last_seen"], msg.timestamp)
        row["channels"].add(msg.channel_id)
        if msg.thread_name:
            row["threads"].add(msg.thread_name)
        msg_links = links_by_message.get(msg.message_id, [])
        row["link_count"] += len(msg_links)
        row["top_domains"].update(_domain(link.canonical_url) for link in msg_links)
        if len(row["sample_message_ids"]) < 3:
            row["sample_message_ids"].append(msg.message_id)
        if not row["sample_text"] and msg.content.strip():
            row["sample_text"] = _one_line(msg.content, 220)

    rows: list[dict[str, Any]] = []
    for row in grouped.values():
        domains = row["top_domains"].most_common(5)
        rows.append(
            {
                "person_key": row["person_key"],
                "author": row["author"],
                "author_id": row["author_id"],
                "message_count": row["message_count"],
                "link_count": row["link_count"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "channels": sorted(row["channels"]),
                "threads": sorted(row["threads"]),
                "top_domains": [f"{domain} x{count}" for domain, count in domains if domain],
                "sample_message_ids": row["sample_message_ids"],
                "sample_text": row["sample_text"],
            }
        )
    rows.sort(key=lambda r: (-int(r["message_count"]), -int(r["link_count"]), str(r["author"])))
    return rows


def _render_summary(manifest: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Discord Dump Analysis Summary",
            "",
            f"Run id: `{manifest['run_id']}`",
            f"Records seen: `{manifest['records_seen']}`",
            f"Discord messages: `{manifest['discord_messages']}`",
            f"Lead records: `{manifest['lead_records']}`",
            f"Unsupported records: `{manifest['unsupported_records']}`",
            f"Crawl-eligible links: `{manifest['crawl_eligible_links']}`",
            f"Crawl queue size: `{manifest['crawl_queue_size']}`",
            f"Crawl results: `{manifest['crawl_results']}`",
            f"Dry run: `{manifest['dry_run']}`",
            "",
            "No network calls or model calls are made in dry-run mode.",
        ]
    ) + "\n"


def _render_operator_brief(
    manifest: dict[str, Any],
    people: list[dict[str, Any]],
    links: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> str:
    lines = [_render_summary(manifest).rstrip(), "", "## Files to Open First", ""]
    lines.extend(
        [
            "- `people.csv` — one row per Discord author with message/link counts and samples.",
            "- `messages.csv` — spreadsheet-friendly normalized messages with redacted content.",
            "- `links.csv` — crawl queue/status by URL, domain class, and source message.",
            "- `crawl_results.jsonl` — raw enrichment result records for downstream scripts.",
            "",
            "## People With Most Signal",
            "",
        ]
    )
    for row in people[:10]:
        lines.append(
            f"- {row['author']}: {row['message_count']} messages, {row['link_count']} links; "
            f"sample `{', '.join(row['sample_message_ids'])}` — {row['sample_text']}"
        )
    if not people:
        lines.append("- No Discord authors found.")

    class_counts = Counter(str(row["domain_class"]) for row in links)
    queued_counts = Counter(str(row["domain_class"]) for row in links if row["queued"])
    lines.extend(["", "## Link Queue", ""])
    if class_counts:
        for cls, count in class_counts.most_common():
            lines.append(f"- {cls}: {count} links, {queued_counts.get(cls, 0)} queued")
    else:
        lines.append("- No crawl-eligible links found.")

    lines.extend(["", "## Recent Message Samples", ""])
    for row in messages[:10]:
        lines.append(f"- `{row['timestamp']}` {row['author']}: {row['content_preview']}")
    if not messages:
        lines.append("- No Discord messages found.")
    return "\n".join(lines).rstrip() + "\n"


def _display_author(message: NormalizedMessage) -> str:
    return message.author_global_name or message.author_username or message.author_id or "unknown"


def _one_line(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "..."


def _domain(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def cli() -> None:
    parser = argparse.ArgumentParser(description="Analyze a private Discord JSON dump")
    parser.add_argument("--input", default="../leads_output")
    parser.add_argument("--output-root", default="data/private/discord_dump")
    parser.add_argument("--run-id")
    parser.add_argument("--max-crawl-urls", type=int, default=1000)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    parser.add_argument("--live", dest="dry_run", action="store_false")
    args = parser.parse_args()
    try:
        result = run_analysis(
            input_path=Path(args.input),
            output_root=Path(args.output_root),
            run_id=args.run_id,
            max_crawl_urls=args.max_crawl_urls,
            dry_run=args.dry_run,
        )
    except Exception as e:  # noqa: BLE001
        error(f"discord dump analysis failed: {e}")
        raise
    info(f"discord dump analysis wrote private artifacts to {result.output_dir}")
    sys.exit(0)


if __name__ == "__main__":
    cli()
