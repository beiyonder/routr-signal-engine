"""Private Discord dump analyzer CLI."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..discord_dump.crawl_queue import CrawlLimits, build_crawl_queue
from ..discord_dump.enrich import enrich_dry_run, summarize_enrichment_health
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
    crawl_results = enrich_dry_run(queue) if dry_run else enrich_dry_run(queue)
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
    summary = _render_summary(manifest)
    (output_dir / "summary.md").write_text(summary, encoding="utf-8")
    (output_dir / "operator_brief.md").write_text(summary, encoding="utf-8")
    return DiscordDumpRunResult(output_dir=output_dir, manifest=manifest)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            data = asdict(row) if hasattr(row, "__dataclass_fields__") else row
            f.write(json.dumps(data, ensure_ascii=False) + "\n")


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


def cli() -> None:
    parser = argparse.ArgumentParser(description="Analyze a private Discord JSON dump")
    parser.add_argument("--input", default="../leads_output")
    parser.add_argument("--output-root", default="data/private/discord_dump")
    parser.add_argument("--run-id")
    parser.add_argument("--max-crawl-urls", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true", default=True)
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
