r"""End-to-end validation harness for routr-signal-engine.

Run:

    .\.venv\Scripts\python.exe tests\validate.py

The harness exercises every major component with controlled inputs, then runs the
full pipeline 3 times against live HN to measure LLM variance. It exits 0 only if
every deterministic check passes; LLM variance is reported but never fails the
suite (LLMs are not bit-exact even at temperature=0).

Output sections:
  [1] Deterministic component checks (must all PASS)
  [2] Discord payload structure (must be schema-valid)
  [3] Live LLM iteration variance (informational)
  [4] Idempotence check (rerun produces 0 new items)
"""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Force UTF-8 stdout on Windows (default is cp1252 which rejects many chars).
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

# Ensure the package is importable when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from routr_signal.classify import client as llm_client  # noqa: E402
from routr_signal.classify import pain_signal, post_drafter, lead_extractor  # noqa: E402
from routr_signal.lib import config as cfg  # noqa: E402
from routr_signal.lib.dedupe import SeenStore  # noqa: E402
from routr_signal.lib.filters import is_suppressed, matches_any_keyword, prefilter  # noqa: E402
from routr_signal.lib.types import (  # noqa: E402
    ClassifiedItem,
    Digest,
    Lead,
    PostHook,
    RawItem,
)
from routr_signal.output import discord as discord_out  # noqa: E402
from routr_signal.output import markdown_digest, slack as slack_out  # noqa: E402


# -----------------------------------------------------------------------------
# Tiny pass/fail helpers — no pytest dep
# -----------------------------------------------------------------------------

_pass = 0
_fail = 0
_section: str = "(none)"


def section(name: str) -> None:
    global _section
    _section = name
    print(f"\n=== {name} ===")


def check(label: str, cond: bool, detail: str = "") -> None:
    global _pass, _fail
    mark = "PASS" if cond else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"  [{mark}] {label}{suffix}")
    if cond:
        _pass += 1
    else:
        _fail += 1


def summary() -> int:
    print(f"\n=== summary: {_pass} passed, {_fail} failed ===")
    return 0 if _fail == 0 else 1


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

def fixture_raw_items() -> list[RawItem]:
    """A controlled set of items spanning relevance, suppression, and edge cases."""

    now = datetime.now(timezone.utc)
    return [
        # 1 — Clear pain signal (LiteLLM cold-start)
        RawItem(
            id="test-1",
            source="hn",
            title="Ask HN: LiteLLM cold-starts are killing our Lambda P99",
            body="We have 50k req/day going through LiteLLM on Lambda. Cold-starts are 3s+. "
            "Looking for a TypeScript alternative that runs on the edge.",
            url="https://news.ycombinator.com/item?id=999001",
            author="founderdev",
            created_at=now,
        ),
        # 2 — Clear pain signal (OpenRouter markup)
        RawItem(
            id="test-2",
            source="reddit",
            title="OpenRouter is charging us 5% on $40k/month — alternatives?",
            body="Looking for a self-hosted gateway that does direct provider routing.",
            url="https://reddit.com/r/LocalLLaMA/comments/abc",
            author="aibuilder99",
            created_at=now,
        ),
        # 3 — Off-topic (should NOT match keywords)
        RawItem(
            id="test-3",
            source="hn",
            title="My weekend project: a static site generator in Rust",
            body="Built this in 200 lines of Rust. Hot reload, markdown, fast.",
            url="https://news.ycombinator.com/item?id=999003",
            author="rusty",
            created_at=now,
        ),
        # 4 — Suppressed (hiring)
        RawItem(
            id="test-4",
            source="hn",
            title="We're hiring an LLM gateway engineer at $200k",
            body="Looking for a TypeScript engineer to work on our LLM gateway.",
            url="https://news.ycombinator.com/item?id=999004",
            author="recruiter",
            created_at=now,
        ),
        # 5 — Keyword match but not actually a pain signal (e.g., a marketing post)
        RawItem(
            id="test-5",
            source="hn",
            title="Show HN: My new blog about LLM gateway design philosophy",
            body="I wrote 5000 words about why I think LLM gateways are interesting.",
            url="https://news.ycombinator.com/item?id=999005",
            author="philosopher",
            created_at=now,
        ),
    ]


# -----------------------------------------------------------------------------
# [1] Deterministic component checks
# -----------------------------------------------------------------------------

def check_filters() -> None:
    section("[1a] keyword filter")
    items = fixture_raw_items()

    # Items 1, 2 match many keywords; item 3 matches none; items 4, 5 contain 'gateway'.
    matches = {it.id: matches_any_keyword(it) for it in items}
    check("item 1 (cold-start, lambda) matches keywords", matches["test-1"])
    check("item 2 (openrouter, markup) matches keywords", matches["test-2"])
    check("item 3 (rust SSG) does NOT match keywords", not matches["test-3"])
    check("item 4 (hiring) matches keywords (will be suppressed downstream)", matches["test-4"])
    check("item 5 (blog) matches keywords", matches["test-5"])

    section("[1b] suppression filter")
    sup = {it.id: is_suppressed(it) for it in items}
    check("item 4 (we're hiring) is suppressed", sup["test-4"])
    check("item 1 is not suppressed", not sup["test-1"])
    check("item 2 is not suppressed", not sup["test-2"])

    section("[1c] prefilter composition")
    kept = prefilter(items)
    kept_ids = {it.id for it in kept}
    check("test-1 kept", "test-1" in kept_ids)
    check("test-2 kept", "test-2" in kept_ids)
    check("test-3 dropped (no keyword match)", "test-3" not in kept_ids)
    check("test-4 dropped (suppressed)", "test-4" not in kept_ids)
    check("test-5 kept", "test-5" in kept_ids)


def check_dedupe() -> None:
    section("[2] dedupe (idempotence primitive, SQLite-backed)")

    # v3: dedupe state lives in signals table; only full RawItems persist.
    from routr_signal.lib import db as _db
    from routr_signal.lib import signal_store as _ss
    from datetime import datetime, timezone as _tz

    # Use a non-Platform-literal source name so type-checkers don't complain;
    # SQLite doesn't care.
    tmp_source = "_validate"
    conn = _db.get_db()
    conn.execute("DELETE FROM signals WHERE source = ?", (tmp_source,))
    conn.commit()

    s1 = SeenStore(tmp_source)
    check("fresh SeenStore is empty", len(s1) == 0)

    def _mk(item_id: str) -> RawItem:
        return RawItem(
            id=item_id, source=tmp_source, title="t", body="b",
            url="http://x", author=None,
            created_at=datetime.now(_tz.utc), extra={},
        )

    s1.add_item(_mk("vid-a"))
    s1.add_item(_mk("vid-b"))
    s1.add_item(_mk("vid-c"))

    s2 = SeenStore(tmp_source)
    check("SeenStore persists across instances", len(s2) == 3)
    check("has() works for stored ids", s2.has("vid-a") and s2.has("vid-b") and s2.has("vid-c"))
    check("has() returns False for unstored", not s2.has("vid-z"))

    # cleanup
    conn.execute("DELETE FROM signals WHERE source = ?", (tmp_source,))
    conn.commit()


def check_json_extractor() -> None:
    section("[3] JSON extractor robustness")
    from routr_signal.classify.client import _extract_json

    plain = '{"items": [{"id": "x", "relevant": true}]}'
    obj = _extract_json(plain)
    check("plain JSON parses", obj == {"items": [{"id": "x", "relevant": True}]})

    fenced = "Here you go:\n```json\n" + plain + "\n```\nLet me know!"
    obj = _extract_json(fenced)
    check("```json-fenced JSON parses", obj == {"items": [{"id": "x", "relevant": True}]})

    fenced2 = "```\n" + plain + "\n```"
    obj = _extract_json(fenced2)
    check("bare fenced JSON parses", obj == {"items": [{"id": "x", "relevant": True}]})

    preamble = 'Sure, here is the result: ' + plain + ' — done.'
    obj = _extract_json(preamble)
    check("prose-preamble JSON parses", obj == {"items": [{"id": "x", "relevant": True}]})

    try:
        _extract_json("no json here at all")
        ok = False
    except json.JSONDecodeError:
        ok = True
    check("invalid input raises JSONDecodeError", ok)


def check_markdown_render_determinism() -> None:
    section("[4] markdown render is byte-deterministic for fixed input")

    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    raw = RawItem(
        id="hn-1",
        source="hn",
        title="Ask HN: LiteLLM cold-start",
        body="...",
        url="https://example.com/1",
        author="alice",
        created_at=now,
    )
    classified = ClassifiedItem(
        raw=raw,
        relevant=True,
        score=0.83,
        wedge="cold_start",
        pain_summary="LiteLLM cold-start 3s on Lambda",
        suggested_angle="reference edge gateway architecture",
        lead_handle="alice",
        lead_platform="hn",
    )
    hook = PostHook(format="x_thread", anchor_signal_id="hn-1", text="A claim with a number.")
    digest = Digest(
        date="2026-05-13",
        pain_signals=[classified],
        active_accounts=[],
        hooks=[hook],
        source_counts={"hn": 1},
        notes=["test"],
    )

    rendered_a = markdown_digest.render(digest)
    rendered_b = markdown_digest.render(copy.deepcopy(digest))
    check("two renders of identical digest are byte-identical", rendered_a == rendered_b)
    expected_sections = ["Routr Daily Signal Digest", "Top pain signals", "Pre-drafted post hooks", "X thread opener"]
    missing = [s for s in expected_sections if s not in rendered_a]
    check(
        "render contains key sections",
        not missing,
        f"missing: {missing}" if missing else "",
    )


def check_lead_extractor() -> None:
    section("[5] lead extractor")

    now = datetime.now(timezone.utc)
    raw1 = RawItem(
        id="hn-7", source="hn", title="t", body="b",
        url="https://x.com/1", author="alice", created_at=now,
    )
    raw2 = RawItem(
        id="reddit-9", source="reddit", title="t", body="b",
        url="https://reddit.com/u/bob", author="bob", created_at=now,
    )
    raw3 = RawItem(  # duplicate handle as raw1, different source
        id="hn-8", source="hn", title="t", body="b",
        url="https://x.com/2", author="alice", created_at=now,
    )
    raw4 = RawItem(  # no author
        id="hn-9", source="hn", title="t", body="b",
        url="https://x.com/3", author=None, created_at=now,
    )
    raw5 = RawItem(  # below min_score
        id="hn-10", source="hn", title="t", body="b",
        url="https://x.com/4", author="lowscore", created_at=now,
    )

    high1 = ClassifiedItem(raw=raw1, relevant=True, score=0.8, wedge="other",
                           pain_summary="pain", suggested_angle="angle",
                           lead_handle="alice", lead_platform="hn")
    high2 = ClassifiedItem(raw=raw2, relevant=True, score=0.7, wedge="other",
                           pain_summary="pain", suggested_angle="angle",
                           lead_handle="bob", lead_platform="reddit")
    dup = ClassifiedItem(raw=raw3, relevant=True, score=0.75, wedge="other",
                         pain_summary="pain", suggested_angle="angle",
                         lead_handle="alice", lead_platform="hn")
    no_author = ClassifiedItem(raw=raw4, relevant=True, score=0.9, wedge="other",
                               pain_summary="pain", suggested_angle="angle",
                               lead_handle=None, lead_platform="hn")
    low = ClassifiedItem(raw=raw5, relevant=True, score=0.3, wedge="other",
                         pain_summary="pain", suggested_angle="angle",
                         lead_handle="lowscore", lead_platform="hn")

    leads = lead_extractor.extract([high1, high2, dup, no_author, low])
    handles = sorted(l.handle.lower() for l in leads)
    check("dedup by handle within same platform", handles == ["alice", "bob"])
    check(
        "alice's profile URL is HN-shaped",
        any(l.handle == "alice" and l.profile_url == "https://news.ycombinator.com/user?id=alice" for l in leads),
    )
    check(
        "bob's profile URL is Reddit-shaped",
        any(l.handle == "bob" and l.profile_url == "https://www.reddit.com/user/bob" for l in leads),
    )
    check("low-score lead excluded", not any(l.handle == "lowscore" for l in leads))


def check_pain_signal_fallback() -> None:
    section("[6] pain_signal classifier fallback when LLM unreachable")

    items = fixture_raw_items()[:3]
    # Force failure by clearing provider key.
    saved_provider = os.environ.get("ROUTR_SIGNAL_LLM_PROVIDER")
    saved_anth = os.environ.get("ANTHROPIC_API_KEY")
    saved_gem = os.environ.get("GEMINI_API_KEY")
    saved_oai = os.environ.get("OPENAI_API_KEY")
    os.environ["ROUTR_SIGNAL_LLM_PROVIDER"] = "anthropic"
    # Wipe all keys so call_json's env_required raises
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        if k in os.environ:
            del os.environ[k]

    # Reset cached client so the deletion takes effect.
    llm_client._anthropic_client = None
    llm_client._gemini_client = None
    llm_client._openai_client = None

    classified = pain_signal.classify(items)
    check("fallback returns same count of items as input", len(classified) == len(items))
    check(
        "every item is marked UNCLASSIFIED in fallback",
        all((c.pain_summary or "").startswith("[UNCLASSIFIED]") for c in classified),
    )
    check("every item has score 0.0 in fallback", all(c.score == 0.0 for c in classified))

    # Restore env so later checks can use the real API.
    for k, v in [
        ("ROUTR_SIGNAL_LLM_PROVIDER", saved_provider),
        ("ANTHROPIC_API_KEY", saved_anth),
        ("GEMINI_API_KEY", saved_gem),
        ("OPENAI_API_KEY", saved_oai),
    ]:
        if v is not None:
            os.environ[k] = v


# -----------------------------------------------------------------------------
# Discord payload structure validation
# -----------------------------------------------------------------------------

def check_discord_payload() -> None:
    section("[7] Discord payload schema")

    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    raws = [
        RawItem(
            id=f"hn-{i}", source="hn", title=f"Pain signal {i}", body=f"body {i}",
            url=f"https://news.ycombinator.com/item?id={i}",
            author=f"u{i}", created_at=now,
        )
        for i in range(5)
    ]
    signals = [
        ClassifiedItem(
            raw=r, relevant=True, score=0.8 - i * 0.05, wedge="cold_start",
            pain_summary=f"Pain summary {i}" * 20,  # exercise truncation
            suggested_angle=f"Angle {i}",
            lead_handle=r.author, lead_platform="hn",
        )
        for i, r in enumerate(raws)
    ]
    hooks = [
        PostHook(format=f, anchor_signal_id=f"hn-{i}", text=f"Hook text for {f}")
        for i, f in enumerate(["x_thread", "linkedin", "reddit", "hn_comment", "devto_title"])
    ]
    leads = [
        Lead(
            source_id=f"hn-{i}", handle=f"lead{i}", platform="hn",
            profile_url=f"https://news.ycombinator.com/user?id=lead{i}",
            pain_in_their_words="x", pitch_angle="y",
            first_seen_at=now,
        )
        for i in range(3)
    ]
    digest = Digest(
        date="2026-05-13",
        pain_signals=signals,
        active_accounts=leads,
        hooks=hooks,
        source_counts={"hn": 5},
        notes=[],
    )

    messages = discord_out._build_messages(digest)
    check("at least one Discord message produced", len(messages) >= 1)
    for i, msg in enumerate(messages):
        check(
            f"message {i + 1}: <= {discord_out.MAX_EMBEDS_PER_MESSAGE} embeds",
            len(msg.get("embeds") or []) <= discord_out.MAX_EMBEDS_PER_MESSAGE,
        )
        check(
            f"message {i + 1}: total embed chars <= {discord_out.MAX_TOTAL_EMBED_CHARS}",
            discord_out._embeds_size(msg.get("embeds") or []) <= discord_out.MAX_TOTAL_EMBED_CHARS,
        )
        check(
            f"message {i + 1}: content <= {discord_out.MAX_CONTENT}",
            len(msg.get("content", "")) <= discord_out.MAX_CONTENT,
        )
        for ei, emb in enumerate(msg.get("embeds") or []):
            for fi, field in enumerate(emb.get("fields") or []):
                check(
                    f"msg {i + 1} embed {ei + 1} field {fi + 1} value <= {discord_out.MAX_FIELD_VALUE}",
                    len(field.get("value", "")) <= discord_out.MAX_FIELD_VALUE,
                )

    # URL normalization
    cases = [
        ("https://discord.com/api/webhooks/123/abc", "https://discord.com/api/webhooks/123/abc"),
        ("https://discord.com/api/webhooks/123/abc/slack", "https://discord.com/api/webhooks/123/abc"),
        ("https://discord.com/api/webhooks/123/abc/", "https://discord.com/api/webhooks/123/abc"),
        ("https://discord.com/api/webhooks/123/abc/slack/", "https://discord.com/api/webhooks/123/abc"),
    ]
    for raw, expected in cases:
        got = discord_out._normalize_url(raw)
        check(f"_normalize_url({raw!r}) → {expected!r}", got == expected, f"got {got!r}")


# -----------------------------------------------------------------------------
# Live LLM iteration variance
# -----------------------------------------------------------------------------

def run_live_iterations(provider: str, model: str, items: list[RawItem], n: int = 3) -> dict[str, Any]:
    """Run the classifier N times with the given provider and report stats."""

    os.environ["ROUTR_SIGNAL_LLM_PROVIDER"] = provider
    if model:
        os.environ["ROUTR_SIGNAL_LLM_MODEL"] = model
    # Reset cached clients.
    llm_client._anthropic_client = None
    llm_client._gemini_client = None
    llm_client._openai_client = None

    runs: list[dict[str, Any]] = []
    for i in range(n):
        t0 = time.monotonic()
        try:
            classified = pain_signal.classify(items)
            dt = time.monotonic() - t0
            relevant_ids = sorted(c.raw.id for c in classified if c.relevant)
            scores = {c.raw.id: round(c.score, 3) for c in classified}
            runs.append({
                "i": i + 1,
                "ok": True,
                "secs": round(dt, 2),
                "relevant_ids": relevant_ids,
                "scores": scores,
            })
        except Exception as e:
            dt = time.monotonic() - t0
            runs.append({"i": i + 1, "ok": False, "secs": round(dt, 2), "error": str(e)})
        time.sleep(0.5)

    return {"provider": provider, "model": model, "runs": runs}


def variance_report(stats: dict[str, Any]) -> None:
    provider = stats["provider"]
    model = stats["model"]
    runs = stats["runs"]
    ok_runs = [r for r in runs if r["ok"]]
    if not ok_runs:
        print(f"  [WARN] all {len(runs)} runs for {provider}/{model} failed")
        for r in runs:
            print(f"    run {r['i']}: {r.get('error', '?')}")
        return

    # Cross-run agreement on which items are relevant
    sets = [set(r["relevant_ids"]) for r in ok_runs]
    intersection = set.intersection(*sets) if sets else set()
    union = set.union(*sets) if sets else set()
    jaccard = (len(intersection) / len(union)) if union else 1.0

    # Score stability per item
    score_drift: dict[str, list[float]] = {}
    for r in ok_runs:
        for item_id, score in r["scores"].items():
            score_drift.setdefault(item_id, []).append(score)

    max_drift = 0.0
    for item_id, scores in score_drift.items():
        if len(scores) > 1:
            spread = max(scores) - min(scores)
            if spread > max_drift:
                max_drift = spread

    print(f"  provider={provider} model={model}")
    print(f"  runs ok: {len(ok_runs)}/{len(runs)}")
    print(f"  latency: " + ", ".join(f"{r['secs']}s" for r in ok_runs))
    print(f"  relevant-set per run:")
    for r in ok_runs:
        print(f"    run {r['i']}: {r['relevant_ids']}")
    print(f"  intersection (in EVERY run): {sorted(intersection)}")
    print(f"  union (in ANY run):          {sorted(union)}")
    print(f"  Jaccard similarity:          {jaccard:.3f}")
    print(f"  max score drift across runs: {max_drift:.3f}")


# -----------------------------------------------------------------------------
# Idempotence check (live; uses HN source)
# -----------------------------------------------------------------------------

def check_idempotence_live() -> None:
    section("[9] live idempotence: rerun produces 0 new HN items")

    from routr_signal.sources import hn
    from routr_signal.lib import db as _db

    # Clear any prior HN state in the signals table so the first fetch is fresh.
    conn = _db.get_db()
    backup_rows = conn.execute("SELECT * FROM signals WHERE source = 'hn'").fetchall()
    conn.execute("DELETE FROM signals WHERE source = 'hn'")
    conn.commit()

    try:
        first = hn.fetch()
        n1 = len(first)
        second = hn.fetch()
        n2 = len(second)
        print(f"  first fetch: {n1} new items; second fetch: {n2} new items")
        check("first fetch is non-empty (sanity)", n1 >= 1)
        check("second fetch returns 0 new items (dedupe is idempotent)", n2 == 0)
    finally:
        # Restore prior state so subsequent runs are not surprised.
        conn.execute("DELETE FROM signals WHERE source = 'hn'")
        for row in backup_rows:
            cols = list(row.keys())
            placeholders = ",".join("?" for _ in cols)
            conn.execute(
                f"INSERT INTO signals ({','.join(cols)}) VALUES ({placeholders})",
                tuple(row[c] for c in cols),
            )
        conn.commit()


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> int:
    print("=== routr-signal-engine validation suite ===\n")

    # Force .env loading
    from routr_signal.lib.env import env  # noqa
    env("ANTHROPIC_API_KEY")  # triggers _load_dotenv

    # ----- Deterministic ------
    check_filters()
    check_dedupe()
    check_json_extractor()
    check_markdown_render_determinism()
    check_lead_extractor()
    check_pain_signal_fallback()
    check_discord_payload()

    # ----- Live idempotence ------
    try:
        check_idempotence_live()
    except Exception as e:
        check("idempotence check executed without crash", False, str(e))

    # ----- LLM variance (informational) ------
    section("[8] LLM classifier variance across iterations (informational)")
    items = fixture_raw_items()
    print("  Using 5-item fixture (item 4 should be suppressed pre-LLM).")
    items_after_prefilter = prefilter(items)
    print(f"  Items going to LLM after prefilter: {[i.id for i in items_after_prefilter]}")

    for provider, model in (("anthropic", "claude-haiku-4-5"), ("gemini", "gemini-3-flash-preview")):
        print(f"\n  --- {provider} {model} ---")
        try:
            stats = run_live_iterations(provider, model, items_after_prefilter, n=3)
            variance_report(stats)
        except Exception as e:
            print(f"  [WARN] provider {provider} failed: {e}")

    return summary()


if __name__ == "__main__":
    sys.exit(main())
