"""Smoke test: cosine relevance scoring picks the right topic for known inputs."""

from __future__ import annotations

import io
import sys
from datetime import datetime, timezone
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from routr_signal.lib import cosine as cos
from routr_signal.lib.types import RawItem


CASES = [
    # (id, title, body, expected_topic_hint, must_pass_threshold)
    ("on-topic-cold-start",
     "Ask HN: LiteLLM cold-starts are killing our Lambda P99",
     "We have 50k req/day going through LiteLLM on Lambda. Cold-starts are 3s+.",
     "cold_start", True),
    ("on-topic-multi-provider",
     "OpenRouter 5% markup is killing our margins, what alternative?",
     "Looking for a way to route directly to providers and keep cost attribution per request.",
     "cost_attribution", True),
    ("on-topic-mcp",
     "MCP tool filtering reduces tokens but busts KV cache",
     "Has anyone measured the trade off between tool list filtering and prompt cache hit rate?",
     "mcp", True),
    ("on-topic-failover",
     "Provider fallback without losing conversation state",
     "We hand rolled a retry/fallback state machine across openai and anthropic and it is brittle.",
     "failover", True),
    ("off-topic-rust",
     "My weekend Rust static site generator",
     "200 lines of Rust. Hot reload, markdown, fast.",
     "other", False),
    ("off-topic-recipe",
     "Best chocolate chip cookie recipe",
     "Brown butter, two yolks, rest the dough 24h.",
     "other", False),
]


def main() -> int:
    now = datetime.now(timezone.utc)
    items = [
        RawItem(id=c[0], source="hn", title=c[1], body=c[2], url="https://x", author="alice", created_at=now)
        for c in CASES
    ]

    kept, dropped = cos.score_items(items)  # use production default 0.68
    kept_ids = {it.id for it, _ in kept}
    dropped_ids = {it.id for it in dropped}

    print("\n--- per-item scores ---")
    for it in items:
        score = it.extra.get("cosine_score", 0.0)
        topic = it.extra.get("cosine_top_topic", "?")
        status = "KEPT  " if it.id in kept_ids else "DROP  "
        print(f"  [{status}] {it.id:30s}  cosine={score:.3f}  top_topic={topic}")

    print("\n--- pass/fail per expected hint ---")
    fails = 0
    for case in CASES:
        cid, _, _, hint, should_pass = case
        item = next(it for it in items if it.id == cid)
        score = item.extra.get("cosine_score", 0.0)
        top_topic = item.extra.get("cosine_top_topic", "?")
        if should_pass:
            ok = cid in kept_ids and (top_topic == hint or score >= 0.6)
            mark = "PASS" if ok else "FAIL"
            if not ok:
                fails += 1
            print(f"  [{mark}] {cid}: expected to be kept under topic '{hint}'; got '{top_topic}' score={score:.3f}")
        else:
            ok = cid in dropped_ids
            mark = "PASS" if ok else "FAIL"
            if not ok:
                fails += 1
            print(f"  [{mark}] {cid}: expected to be dropped (off-topic); status: {'dropped' if ok else 'KEPT (wrong)'}, top_topic={top_topic}, score={score:.3f}")

    print(f"\n{'PASS' if fails == 0 else 'FAIL'}: {len(CASES) - fails}/{len(CASES)} cases passed")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
