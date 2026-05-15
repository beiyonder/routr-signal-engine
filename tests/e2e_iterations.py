"""Three end-to-end iterations on live data.

  Iteration 1: fresh seen state, run pipeline with Anthropic, DRY-RUN (no Discord post).
  Iteration 2: same state — verify dedupe = 0 new items (idempotence check).
  Iteration 3: reset seen, run pipeline with Gemini — compare classifier picks vs iter 1.

Then report: agreement, score drift, hooks-format coverage.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from routr_signal.lib.paths import data_dir, seen_dir
from routr_signal.sources import github_issues, hn


def _reset_seen() -> None:
    for f in seen_dir().iterdir():
        if f.suffix == ".json":
            f.unlink()


def _run_pipeline() -> dict[str, Any]:
    """Run the pipeline in-process and capture digest + counts."""

    from routr_signal import main as pipeline

    pipeline.run()

    # Snapshot the digest written by this run
    digest_path = data_dir() / "raw" / f"digest-{pipeline.today_utc()}.json"
    payload = json.loads(digest_path.read_text(encoding="utf-8"))
    return payload


def _summarize(label: str, payload: dict[str, Any]) -> dict[str, Any]:
    signals = payload.get("pain_signals", [])
    hooks = payload.get("hooks", [])
    leads = payload.get("active_accounts", [])

    sig_ids = [s["raw"]["id"] for s in signals]
    sig_scores = {s["raw"]["id"]: round(s["score"], 3) for s in signals}
    sig_wedges = {s["raw"]["id"]: s["wedge"] for s in signals}
    hook_formats = [h["format"] for h in hooks]

    summary = {
        "label": label,
        "source_counts": payload.get("source_counts", {}),
        "signal_count": len(signals),
        "signal_ids": sig_ids,
        "signal_scores": sig_scores,
        "signal_wedges": sig_wedges,
        "lead_count": len(leads),
        "hook_count": len(hooks),
        "hook_formats": hook_formats,
        "notes": payload.get("notes", []),
    }
    return summary


def _print_summary(s: dict[str, Any]) -> None:
    print(f"\n  --- {s['label']} ---")
    print(f"  source counts: {s['source_counts']}")
    print(f"  signals: {s['signal_count']} (ids: {s['signal_ids']})")
    print(f"  scores : {s['signal_scores']}")
    print(f"  wedges : {s['signal_wedges']}")
    print(f"  leads  : {s['lead_count']}")
    print(f"  hooks  : {s['hook_count']} (formats: {s['hook_formats']})")
    print(f"  notes  : {s['notes']}")


def _compare(a: dict[str, Any], b: dict[str, Any]) -> None:
    print(f"\n  --- cross-iteration comparison: {a['label']} vs {b['label']} ---")
    set_a = set(a["signal_ids"])
    set_b = set(b["signal_ids"])
    inter = set_a & set_b
    union = set_a | set_b
    jaccard = (len(inter) / len(union)) if union else 1.0
    only_a = set_a - set_b
    only_b = set_b - set_a
    print(f"  in both:       {sorted(inter)}")
    print(f"  only in A:     {sorted(only_a)}")
    print(f"  only in B:     {sorted(only_b)}")
    print(f"  Jaccard:       {jaccard:.3f}")

    # Score drift on items present in both
    drifts: list[tuple[str, float]] = []
    for sid in inter:
        sa = a["signal_scores"].get(sid)
        sb = b["signal_scores"].get(sid)
        if sa is not None and sb is not None:
            drifts.append((sid, abs(sa - sb)))
    if drifts:
        max_d = max(d for _, d in drifts)
        mean_d = sum(d for _, d in drifts) / len(drifts)
        print(f"  score drift:   max={max_d:.3f}, mean={mean_d:.3f}")
        for sid, d in sorted(drifts, key=lambda x: -x[1])[:5]:
            print(f"    {sid}: |{a['signal_scores'][sid]:.3f} - {b['signal_scores'][sid]:.3f}| = {d:.3f}")


def main() -> int:
    print("=== Three end-to-end iterations on live data ===")
    # Use HN + GitHub issues; Reddit is IP-banned on this dev box.
    os.environ["ROUTR_SIGNAL_SOURCES"] = "hn,github_issues"
    os.environ["ROUTR_SIGNAL_PUBLISH"] = "0"  # Don't double-post to Discord during the test
    os.environ["ROUTR_SIGNAL_COMMIT"] = "0"

    print("\n--- iter 1: fresh seen state, Anthropic Claude Haiku 4.5 ---")
    _reset_seen()
    os.environ["ROUTR_SIGNAL_LLM_PROVIDER"] = "anthropic"
    os.environ["ROUTR_SIGNAL_LLM_MODEL"] = "claude-haiku-4-5"
    payload1 = _run_pipeline()
    sum1 = _summarize("iter1-anthropic", payload1)
    _print_summary(sum1)

    print("\n--- iter 2: immediate rerun (idempotence check) ---")
    time.sleep(2)
    payload2 = _run_pipeline()
    sum2 = _summarize("iter2-rerun-same-state", payload2)
    _print_summary(sum2)

    print("\n--- iter 3: reset seen, Gemini 3 Flash Preview ---")
    _reset_seen()
    os.environ["ROUTR_SIGNAL_LLM_PROVIDER"] = "gemini"
    os.environ["ROUTR_SIGNAL_LLM_MODEL"] = "gemini-3-flash-preview"
    payload3 = _run_pipeline()
    sum3 = _summarize("iter3-gemini", payload3)
    _print_summary(sum3)

    # Comparisons
    _compare(sum1, sum2)
    _compare(sum1, sum3)

    print("\n=== conclusions ===")
    # Idempotence
    iter2_empty = sum2["signal_count"] == 0 and sum(sum2["source_counts"].values()) == 0
    print(f"  iter 2 idempotence (rerun produces 0 fetched items): {'PASS' if iter2_empty else 'FAIL'}")

    # Hook coverage: every iteration must produce 5 distinct hook formats (or be a low-signal day)
    expected_formats = {"x_thread", "linkedin", "reddit", "hn_comment", "devto_title"}
    for s in (sum1, sum3):
        got_formats = set(s["hook_formats"])
        ok = got_formats == expected_formats
        print(f"  {s['label']}: 5 distinct hook formats: {'PASS' if ok else 'FAIL'} (got {sorted(got_formats)})")

    # Cross-provider Jaccard
    set1 = set(sum1["signal_ids"])
    set3 = set(sum3["signal_ids"])
    if set1 or set3:
        j13 = len(set1 & set3) / len(set1 | set3) if (set1 | set3) else 1.0
        print(f"  Anthropic vs Gemini classifier agreement (Jaccard on signal picks): {j13:.3f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
