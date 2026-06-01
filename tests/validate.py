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
import inspect
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

def check_distribution_modules() -> None:
    """Smoke-check the distribution stack modules import + have the expected surface."""

    section("[9] distribution stack: imports + surface")

    # Buffer
    from routr_signal.lib import buffer_client
    check("buffer_client exports CreatedPost", hasattr(buffer_client, "CreatedPost"))
    check("buffer_client exports create_post", callable(getattr(buffer_client, "create_post", None)))
    check("buffer_client exports list_channels", callable(getattr(buffer_client, "list_channels", None)))
    check("buffer_client exports account", callable(getattr(buffer_client, "account", None)))
    check(
        "buffer_client.VALID_SHARE_MODES includes shareNow + addToQueue",
        "shareNow" in buffer_client.VALID_SHARE_MODES and "addToQueue" in buffer_client.VALID_SHARE_MODES,
    )

    # Beehiiv
    from routr_signal.lib import beehiiv_client
    check(
        "beehiiv_client exports create_draft_post",
        callable(getattr(beehiiv_client, "create_draft_post", None)),
    )

    # Discord inbox
    from routr_signal.lib import discord_inbox
    check(
        "discord_inbox exports get_message + message_has_reaction + add_bot_reaction",
        all(
            callable(getattr(discord_inbox, n, None))
            for n in ("get_message", "message_has_reaction", "add_bot_reaction")
        ),
    )

    # Synthesize
    from routr_signal.classify import synthesize
    check(
        "synthesize exports SynthesisResult + synthesize",
        hasattr(synthesize, "SynthesisResult") and callable(getattr(synthesize, "synthesize", None)),
    )

    # Tasks: weekly_synthesis + dispatch_approved CLI hooks
    from routr_signal.tasks import dispatch_approved, weekly_synthesis
    check(
        "weekly_synthesis exports cli + run",
        all(callable(getattr(weekly_synthesis, n, None)) for n in ("cli", "run")),
    )
    # Dispatch: 📤 is the deliberate ship trigger (was ✅ / 👍 before 2026-05-18).
    # The change forces deliberation; casual reactions no longer ship.
    check(
        "dispatch_approved exports cli + run",
        all(callable(getattr(dispatch_approved, n, None)) for n in ("cli", "run")),
    )
    check(
        "dispatch_approved.HOOK_APPROVAL_EMOJIS contains only the outbox emoji 📤",
        dispatch_approved.HOOK_APPROVAL_EMOJIS == ("\U0001F4E4",),
    )
    check(
        "dispatch_approved.SYNTHESIS_APPROVAL_EMOJI is newspaper 📰",
        dispatch_approved.SYNTHESIS_APPROVAL_EMOJI == "\U0001F4F0",
    )
    check(
        "dispatch_approved.BOT_PROCESSED_MARKER is rocket 🚀",
        dispatch_approved.BOT_PROCESSED_MARKER == "\U0001F680",
    )

    repo_root = Path(__file__).resolve().parent.parent
    pipeline_yml = (repo_root / ".github" / "workflows" / "pipeline.yml").read_text(encoding="utf-8")
    check("pipeline.yml does not upload private runtime artifacts", "actions/upload-artifact" not in pipeline_yml)
    check("pipeline.yml explicitly keeps outputs off artifacts", "Keep private outputs off Actions artifacts" in pipeline_yml)

    # _parse_message_ids handles all input shapes
    parse = dispatch_approved._parse_message_ids
    check("_parse_message_ids handles None", parse(None) == [])
    check("_parse_message_ids handles empty string", parse("") == [])
    check("_parse_message_ids handles list", parse(["a", "b"]) == ["a", "b"])
    check("_parse_message_ids handles JSON string", parse('["x","y"]') == ["x", "y"])
    check("_parse_message_ids handles bad JSON", parse("not json") == [])
    check("_parse_message_ids filters non-strings", parse(["a", 1, None, "b"]) == ["a", "b"])


def check_posts_table() -> None:
    """Round-trip the posts table via signal_store helpers."""

    section("[10] posts table + signal_store helpers")

    import json as _json
    import uuid as _uuid
    from routr_signal.lib import db as _db
    from routr_signal.lib import signal_store as _ss

    conn = _db.get_db()

    # Ensure the new columns are present after migration.
    runs_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    check("runs has 'kind' column", "kind" in runs_cols)
    check("runs has 'discord_message_ids' column", "discord_message_ids" in runs_cols)
    posts_cols = {row[1] for row in conn.execute("PRAGMA table_info(posts)").fetchall()}
    expected_cols = {
        "id", "kind", "signal_id", "run_id", "hook_format", "platform", "text",
        "discord_message_id", "discord_reaction", "buffer_post_id", "beehiiv_post_id",
        "external_url", "status", "error", "created_at", "approved_at", "posted_at",
        "metadata",
    }
    check(
        "posts has all expected columns",
        expected_cols.issubset(posts_cols),
        f"missing: {sorted(expected_cols - posts_cols)}" if not expected_cols.issubset(posts_cols) else "",
    )

    # Use a one-off run id so we don't pollute real data.
    run_id = f"_validate-{_uuid.uuid4().hex[:8]}"
    _ss.open_run(run_id, kind="daily")

    # record_run_discord_messages + runs_with_pending_messages
    _ss.record_run_discord_messages(run_id, ["msg-A", "msg-B"])
    pending = _ss.runs_with_pending_messages(limit=10)
    found = any(r["id"] == run_id for r in pending)
    check("runs_with_pending_messages includes our run", found)

    # insert_post + pending_posts_for_run
    post_id = f"_test-post-{_uuid.uuid4().hex[:8]}"
    _ss.insert_post(
        post_id=post_id,
        kind="hook",
        platform="x",
        text="test hook text",
        status="pending",
        run_id=run_id,
        hook_format="x_thread",
        discord_message_id="msg-B",
        metadata={"approval_emoji": "\u2705"},
    )
    pending_posts = _ss.pending_posts_for_run(run_id)
    check("pending_posts_for_run returns our inserted post", any(p["id"] == post_id for p in pending_posts))

    # update_post_status: pending -> posted
    _ss.update_post_status(
        post_id,
        status="posted",
        buffer_post_id="buffer-xxx",
        discord_reaction="\u2705",
        approved=True,
        posted=True,
    )
    row = _ss.get_post(post_id)
    check("update_post_status promotes pending->posted", row is not None and row["status"] == "posted")
    check("update_post_status records buffer_post_id", row is not None and row["buffer_post_id"] == "buffer-xxx")
    check(
        "update_post_status sets approved_at + posted_at",
        row is not None and row["approved_at"] is not None and row["posted_at"] is not None,
    )

    # Cleanup
    conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    conn.commit()


def check_new_sources_register() -> None:
    """The new sources/hf_papers and sources/newsletters should be in the registry."""

    section("[11] new sources registered in main pipeline")

    from routr_signal.main import DEFAULT_SOURCES, SOURCE_REGISTRY
    check("SOURCE_REGISTRY includes hf_papers", "hf_papers" in SOURCE_REGISTRY)
    check("SOURCE_REGISTRY includes newsletters", "newsletters" in SOURCE_REGISTRY)
    check("DEFAULT_SOURCES includes hf_papers", "hf_papers" in DEFAULT_SOURCES)
    check("DEFAULT_SOURCES includes newsletters", "newsletters" in DEFAULT_SOURCES)

    # hf_papers + newsletters source modules are import-safe and have a fetch() callable.
    from routr_signal.sources import hf_papers, newsletters
    check("hf_papers.fetch is callable", callable(hf_papers.fetch))
    check("newsletters.fetch is callable", callable(newsletters.fetch))
    check("hf_papers SOURCE == 'hf'", hf_papers.SOURCE == "hf")
    check("newsletters SOURCE == 'newsletter'", newsletters.SOURCE == "newsletter")



def check_topic_frequency() -> None:
    """signal_store.topic_frequency aggregates llm_topics across recent classified signals."""

    section("[12] topic frequency helper")

    import json as _json
    import uuid as _uuid
    from datetime import datetime, timezone

    from routr_signal.lib import db as _db
    from routr_signal.lib import signal_store as _ss

    conn = _db.get_db()

    # Seed three throwaway signals with distinct topic distributions.
    src = "_validate_freq"
    conn.execute("DELETE FROM signals WHERE source = ?", (src,))
    conn.commit()

    now_iso = datetime.now(timezone.utc).isoformat()
    rows = [
        (f"_freq-{_uuid.uuid4().hex[:6]}", "mcp"),
        (f"_freq-{_uuid.uuid4().hex[:6]}", "mcp"),
        (f"_freq-{_uuid.uuid4().hex[:6]}", "cold_start"),
    ]
    for sid, topic in rows:
        conn.execute(
            """
            INSERT INTO signals (id, source, title, url, created_at, fetched_at,
                                 raw_extra, llm_relevant, llm_topics, classified_at)
            VALUES (?, ?, 't', 'http://x', ?, ?, '{}', 1, ?, ?)
            """,
            (sid, src, now_iso, now_iso, _json.dumps([topic]), now_iso),
        )
    conn.commit()

    freq = _ss.topic_frequency(window_days=7)
    check("topic_frequency returns mcp ≥ 2", freq.get("mcp", 0) >= 2)
    check("topic_frequency returns cold_start ≥ 1", freq.get("cold_start", 0) >= 1)
    check(
        "topic_frequency is sorted by count desc (mcp before cold_start in iteration)",
        list(freq.keys()).index("mcp") < list(freq.keys()).index("cold_start"),
    )

    # cleanup
    conn.execute("DELETE FROM signals WHERE source = ?", (src,))
    conn.commit()


def check_people_tables() -> None:
    """people, signal_people, and weekly_people stay queryable from signals."""

    section("[12b] people aggregation tables")

    import json as _json
    import uuid as _uuid
    from datetime import datetime, timezone

    from routr_signal.lib import db as _db
    from routr_signal.lib import signal_store as _ss

    conn = _db.get_db()
    people_cols = {row[1] for row in conn.execute("PRAGMA table_info(people)").fetchall()}
    signal_people_cols = {row[1] for row in conn.execute("PRAGMA table_info(signal_people)").fetchall()}
    weekly_cols = {row[1] for row in conn.execute("PRAGMA table_info(weekly_people)").fetchall()}
    check("people table exists with handle and signal counts", {"id", "handle", "signal_count"}.issubset(people_cols))
    check("signal_people table exists with role evidence", {"signal_id", "person_id", "role"}.issubset(signal_people_cols))
    check("weekly_people table exists with summary", {"week_start", "person_id", "summary"}.issubset(weekly_cols))

    src = "_validate_people"
    sid = f"_people-{_uuid.uuid4().hex[:6]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM signals WHERE source = ?", (src,))
    conn.execute("DELETE FROM signal_people WHERE signal_id = ?", (sid,))
    conn.commit()
    conn.execute(
        """
        INSERT INTO signals (id, source, author_handle, title, body, url, created_at, fetched_at,
                             raw_extra, llm_relevant, llm_topics, llm_lead_handle,
                             llm_lead_platform, classified_at, combined_score)
        VALUES (?, ?, '@Builder', 't', 'b', 'https://x.com/Builder/status/1', ?, ?, '{}',
                1, ?, '@LeadUser', 'x', ?, 0.77)
        """,
        (sid, src, now_iso, now_iso, _json.dumps(["agent_reliability"]), now_iso),
    )
    conn.commit()

    count = _ss.rebuild_people_from_signals()
    check("rebuild_people_from_signals returns aggregate count", count >= 2)
    author = conn.execute("SELECT * FROM people WHERE id = ?", (f"{src}:builder",)).fetchone()
    lead = conn.execute("SELECT * FROM people WHERE id = 'x:leaduser'").fetchone()
    joins = conn.execute("SELECT role FROM signal_people WHERE signal_id = ?", (sid,)).fetchall()
    check("people table records signal author", author is not None and author["signal_count"] >= 1)
    check("people table records classifier lead", lead is not None and lead["platform"] == "x")
    check("signal_people records author and lead roles", {r["role"] for r in joins} >= {"author", "lead"})

    snapshot = _ss.weekly_people_snapshot(window_days=7, limit=10)
    check("weekly_people_snapshot returns people", any(p["id"] == f"{src}:builder" for p in snapshot))
    weekly = conn.execute("SELECT * FROM weekly_people WHERE person_id = ?", (f"{src}:builder",)).fetchone()
    check("weekly_people table stores snapshot summary", weekly is not None and "surfaced" in weekly["summary"])

    # cleanup
    conn.execute("DELETE FROM signals WHERE source = ?", (src,))
    conn.execute("DELETE FROM signal_people WHERE signal_id = ?", (sid,))
    conn.execute("DELETE FROM weekly_people WHERE person_id IN (?, 'x:leaduser')", (f"{src}:builder",))
    conn.execute("DELETE FROM people WHERE id IN (?, 'x:leaduser')", (f"{src}:builder",))
    conn.commit()


def check_hook_source_link() -> None:
    """The Discord hooks_embed renders a clickable source link when a hook
    anchors to a known signal."""

    section("[13] hook source link in digest")

    from datetime import datetime, timezone

    from routr_signal.output import discord as discord_out

    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    signals = [
        ClassifiedItem(
            raw=RawItem(
                id="hn-source-test",
                source="hn",
                title="t",
                body="b",
                url="https://news.ycombinator.com/item?id=999999",
                author="u",
                created_at=now,
            ),
            relevant=True,
            score=0.8,
            wedge="cold_start",
            pain_summary="p",
            suggested_angle="a",
            lead_handle="u",
            lead_platform="hn",
        )
    ]
    hooks = [PostHook(format="x_thread", anchor_signal_id="hn-source-test", text="standalone post")]
    digest = Digest(
        date="2026-05-13",
        pain_signals=signals,
        active_accounts=[],
        hooks=hooks,
        source_counts={"hn": 1},
        notes=[],
    )

    messages = discord_out._build_messages(digest)
    # Find the hooks embed in any message; locate the hook field; check its value
    found_link = False
    for msg in messages:
        for emb in msg.get("embeds") or []:
            if emb.get("title") == "Pre-drafted post hooks":
                for f in emb.get("fields") or []:
                    val = f.get("value") or ""
                    if "https://news.ycombinator.com/item?id=999999" in val:
                        found_link = True
                        break
    check("hooks_embed includes the anchored signal's source URL", found_link)

    # Markdown digest should also embed the link
    from routr_signal.output import markdown_digest as md
    rendered = md.render(digest)
    check(
        "markdown digest includes [source](url) for anchored hook",
        "[source](https://news.ycombinator.com/item?id=999999)" in rendered,
    )


def check_x_burst_surface() -> None:
    """The standalone X-burst task: drafter, voice_lint variant, signal_store
    helpers, task module, console script, pipeline.yml schedule.

    This lane used to auto-ship short posts. It is now manual-review only,
    so validation locks that safety property.
    """

    section("[14] x_burst standalone task surface")

    # The x_burst prompt file must exist (loaded via lib.config.prompt).
    from routr_signal.lib.config import prompt as _prompt
    try:
        burst_prompt = _prompt("x_burst")
    except FileNotFoundError:
        burst_prompt = ""
    check("config/prompts/x_burst.md exists and is readable", bool(burst_prompt))
    check(
        "x_burst prompt allows X Premium long-form (25,000 chars)",
        "25,000" in burst_prompt or "25000" in burst_prompt,
    )
    check(
        "x_burst prompt says nothing auto-ships to X",
        "Nothing from this lane auto-ships to X" in burst_prompt,
    )
    check(
        "x_burst prompt declares natural-imperfections allowance",
        "natural" in burst_prompt.lower() and ("typo" in burst_prompt.lower() or "imperfection" in burst_prompt.lower()),
    )
    check(
        "x_burst prompt bans em-dash + emoji + cliffhangers (carried from post_drafter)",
        all(s in burst_prompt for s in ("em-dash", "emoji", "cliffhanger")),
    )
    check(
        "x_burst prompt includes recent post novelty memory",
        "recent_x_posts_last_14_days" in burst_prompt and "same post with nouns swapped" in burst_prompt,
    )

    # Drafter module
    from routr_signal.classify import x_burst_drafter
    check("x_burst_drafter exports draft_x_burst", callable(getattr(x_burst_drafter, "draft_x_burst", None)))
    check(
        "x_burst_drafter sets SYSTEM_PROMPT_NAME=='x_burst'",
        getattr(x_burst_drafter, "SYSTEM_PROMPT_NAME", "") == "x_burst",
    )

    # Voice-lint variant
    from routr_signal.classify import voice_lint
    check(
        "voice_lint exports lint_x_burst_post + lint_x_burst_all",
        callable(getattr(voice_lint, "lint_x_burst_post", None))
        and callable(getattr(voice_lint, "lint_x_burst_all", None)),
    )
    check(
        "voice_lint.X_BURST_LENGTH_CAP == 25,000 (X Premium hard cap)",
        getattr(voice_lint, "X_BURST_LENGTH_CAP", 0) == 25_000,
    )
    check("voice_lint still defines Buffer legacy cap for lint/router compatibility", getattr(voice_lint, "X_BURST_AUTO_SHIP_CAP", 0) == 270)

    # A 250-char clean post should pass.
    short_clean = (
        "ran 50k requests through a typescript llm proxy on cloudflare workers last night. "
        "cold-start p99 was 90ms compared to 470ms on python lambda. the runtime swap matters "
        "more than the routing layer for serverless gateway latency."
    )
    res = voice_lint.lint_x_burst_post(PostHook(format="x_thread", anchor_signal_id=None, text=short_clean))
    check(
        f"lint_x_burst_post accepts a {len(short_clean)}-char clean post",
        not res.violations,
        f"violations: {res.violations}" if res.violations else "",
    )

    # A 5,000-char clean post should ALSO pass (under the 25k hard cap).
    long_clean = (
        "ran a benchmark across 50000 requests last night and the numbers were interesting. "
    ) * 50  # ~4100 chars; no banned words, no em-dash, no emoji
    long_clean += "p99 latency dropped from 1.4 seconds to 380 milliseconds after the rewrite."
    res = voice_lint.lint_x_burst_post(PostHook(format="x_thread", anchor_signal_id=None, text=long_clean))
    check(
        f"lint_x_burst_post accepts a {len(long_clean)}-char clean post (DM-routed)",
        not res.violations,
        f"violations: {res.violations}" if res.violations else "",
    )

    # A 30,000-char post should FAIL the 25k cap.
    too_long = "a" * 30_000
    res = voice_lint.lint_x_burst_post(PostHook(format="x_thread", anchor_signal_id=None, text=too_long))
    check(
        "lint_x_burst_post rejects a 30k-char post (over the X Premium hard cap)",
        any("length" in v.lower() for v in res.violations),
    )

    # Discord DM surface
    from routr_signal.lib import discord_inbox
    check(
        "discord_inbox exports send_dm (long-form delivery channel)",
        callable(getattr(discord_inbox, "send_dm", None)),
    )
    check(
        "discord_inbox exports _chunk_long_text helper",
        callable(getattr(discord_inbox, "_chunk_long_text", None)),
    )
    # Chunking: a 5000-char input should split into 3+ chunks of <=1900 chars each.
    chunks = discord_inbox._chunk_long_text("a" * 5000, max_chars=1900)
    check(
        f"_chunk_long_text splits 5000-char input into multiple chunks (got {len(chunks)})",
        len(chunks) >= 3 and all(len(c) <= 1900 for c in chunks),
    )
    # Chunking: a 1000-char input should not split.
    chunks = discord_inbox._chunk_long_text("a" * 1000, max_chars=1900)
    check("_chunk_long_text leaves a 1000-char input as a single chunk", len(chunks) == 1)

    # Lint rule: terminal colon still flags (cliffhanger remains banned).
    res = voice_lint.lint_x_burst_post(
        PostHook(format="x_thread", anchor_signal_id=None, text="some thoughts on llm gateways:")
    )
    check(
        "lint_x_burst_post flags terminal-colon cliffhanger",
        any("cliffhanger" in v for v in res.violations),
    )

    # Lint rule: raw URL flags (X penalizes link posts).
    res = voice_lint.lint_x_burst_post(
        PostHook(format="x_thread", anchor_signal_id=None, text="see https://example.com for details.")
    )
    check(
        "lint_x_burst_post flags raw http(s) URL",
        any("URL" in v or "url" in v.lower() for v in res.violations),
    )

    # Lint rule: em-dash still flags.
    res = voice_lint.lint_x_burst_post(
        PostHook(format="x_thread", anchor_signal_id=None, text="this is the part \u2014 the only part \u2014 that matters.")
    )
    check(
        "lint_x_burst_post flags em-dash",
        any("em-dash" in v for v in res.violations),
    )

    # Lint rule: AI pivot still flags.
    res = voice_lint.lint_x_burst_post(
        PostHook(format="x_thread", anchor_signal_id=None, text="it's not the proxy, it's the queue.")
    )
    check(
        "lint_x_burst_post flags it's-not-X-it's-Y AI pivot",
        any("pivot" in v.lower() for v in res.violations),
    )

    # signal_store helpers
    from routr_signal.lib import signal_store as _ss
    check(
        "signal_store exports recent_classified_for_drafting",
        callable(getattr(_ss, "recent_classified_for_drafting", None)),
    )
    check(
        "signal_store exports signal_ids_posted_today",
        callable(getattr(_ss, "signal_ids_posted_today", None)),
    )
    check(
        "signal_store exports 14d post memory helpers",
        callable(getattr(_ss, "signal_ids_posted_since", None))
        and callable(getattr(_ss, "recent_post_texts", None)),
    )
    # smoke-call recent_classified_for_drafting on an empty/small DB; must return a list
    rows = _ss.recent_classified_for_drafting(window_hours=48, min_score=0.0, limit=5)
    check("recent_classified_for_drafting returns a list", isinstance(rows, list))
    excluded = _ss.signal_ids_posted_today(kind="x_burst", platform="x")
    check("signal_ids_posted_today returns a set", isinstance(excluded, set))
    excluded_recent = _ss.signal_ids_posted_since(kind="x_burst", platform="x", days=14)
    check("signal_ids_posted_since returns a set", isinstance(excluded_recent, set))
    recent_texts = _ss.recent_post_texts(kind="x_burst", platform="x", days=14, limit=3)
    check("recent_post_texts returns a list", isinstance(recent_texts, list))

    # Task module + CLI
    from routr_signal.tasks import x_burst
    check("tasks.x_burst exports run + cli", callable(getattr(x_burst, "run", None)) and callable(getattr(x_burst, "cli", None)))
    check("tasks.x_burst.DEFAULT_COUNT == 2", getattr(x_burst, "DEFAULT_COUNT", 0) == 2)
    check("tasks.x_burst has recent-post memory window", getattr(x_burst, "RECENT_POST_MEMORY_DAYS", 0) == 14)
    check("x_burst dry runs use isolated post kind", "x_burst_dry_run" in x_burst._record_dry_run.__code__.co_consts)
    check(
        "x_burst similarity check detects repeated drafts",
        x_burst._most_similar_recent_post(
            "agent review failed because rollback boundaries were unclear",
            [{"id": "p1", "text": "rollback boundaries were unclear in the long agent review"}],
        )
        is not None,
    )

    # pyproject console script
    import pathlib as _pl
    pyproj = (_pl.Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    check(
        "pyproject.toml declares routr-burst console script",
        "routr-burst" in pyproj and "routr_signal.tasks.x_burst:cli" in pyproj,
    )

    # pipeline.yml schedule + job
    pipeline_yml = (_pl.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "pipeline.yml").read_text(encoding="utf-8")
    check(
        "pipeline.yml has 02:30 UTC cron (8 AM IST)",
        '"30 2 * * *"' in pipeline_yml,
    )
    check(
        "pipeline.yml has 07:00 UTC cron (12:30 PM IST)",
        '"0 7 * * *"' in pipeline_yml,
    )
    check("pipeline.yml has x_burst job", "x_burst:" in pipeline_yml or "  x_burst:" in pipeline_yml)
    check("pipeline.yml documents x_burst manual-review only", "Nothing in this lane auto-ships to X" in pipeline_yml)
    check(
        "pipeline.yml x_burst job uses `needs: [daily]` for sequential 02:30 run",
        "needs: [daily]" in pipeline_yml,
    )
    check(
        "pipeline.yml workflow_dispatch task choices include 'burst'",
        "burst" in pipeline_yml and "synthesis" in pipeline_yml,
    )


def check_x_watch_surface() -> None:
    section("[15] x_watch fast-reply task surface")

    from routr_signal.classify import x_reply_scorer
    from routr_signal.lib.config import prompt as _prompt, x_fast_watch as _x_fast_watch
    from routr_signal.tasks import x_watch

    cfg = _x_fast_watch()
    accounts = cfg.get("accounts", []) if isinstance(cfg, dict) else []
    check("config/x_fast_watch.yaml loads", isinstance(cfg, dict) and bool(cfg))
    check("x_fast_watch config includes at least 60 accounts", isinstance(accounts, list) and len(accounts) >= 60)
    handles = {str(a.get("handle", "")).lower() for a in accounts if isinstance(a, dict)}
    check("x_fast_watch includes VC lane accounts", {"snowmaker", "deedydas", "garrytan"}.issubset(handles))
    check("x_fast_watch includes AI-builder lane accounts", {"hwchase17", "jxnlco", "timzaman"}.issubset(handles))

    try:
        scorer_prompt = _prompt("x_reply_scorer")
    except FileNotFoundError:
        scorer_prompt = ""
    check("config/prompts/x_reply_scorer.md exists", bool(scorer_prompt))
    check(
        "x_reply_scorer prompt is about fast replies within 10-60 minutes",
        "10-60 minutes" in scorer_prompt and "suggested_reply" in scorer_prompt,
    )

    response = {
        "opportunities": [
            {
                "id": "xwatch-1",
                "score": 0.91,
                "reason": "Strong technical opening.",
                "reply_angle": "Add the eval distinction.",
                "suggested_reply": "the missing distinction is model failure vs orchestration failure. most agent evals blend them together.",
            },
            {"id": "xwatch-2", "score": "bad", "suggested_reply": ""},
        ]
    }
    parsed = x_reply_scorer._parse_response(response)
    check("x_reply_scorer._parse_response returns two entries", len(parsed) == 2)
    check("x_reply_scorer clamps invalid score to 0", parsed[1].score == 0.0)

    built = x_watch._build_twitter_config(cfg)
    searches = built.get("searches", [])
    check("x_watch builds grouped X `from:` searches", isinstance(searches, list) and searches and "from:" in searches[0])
    check("x_watch grouped searches exclude replies by default", all("-filter:replies" in s for s in searches))
    check("x_watch prioritizes mid-tier builder accounts first", "from:swyx" in searches[0] and "from:simonw" in " ".join(searches[:2]))
    check("x_watch config uses 10-60m production recency", cfg.get("fetch", {}).get("min_age_minutes") == 10 and cfg.get("fetch", {}).get("fresh_window_minutes") == 60)
    sample_reply = x_watch._render_dm(
        RawItem(
            id="xwatch-test",
            source="x",
            title="t",
            body="tweet body",
            url="https://twitter.com/example/status/1",
            author="@swyx",
            created_at=datetime.now(timezone.utc),
        ),
        x_reply_scorer.ReplyOpportunity(
            signal_id="xwatch-test",
            score=0.8,
            reason="reason",
            reply_angle="angle",
            suggested_reply="copy only this",
        ),
        account_meta={"swyx": {"tier": 3, "tags": ["ai_engineering"]}},
    )
    check("x_watch DM starts with copy-only suggested reply block", sample_reply.startswith("COPY SUGGESTED REPLY ONLY:\n```\ncopy only this\n```"))
    check("x_watch DM labels tweet URL as Source", "Source: https://twitter.com/example/status/1" in sample_reply)
    check("x_watch dry-run kind is isolated from real alert dedupe", "x_reply_alert_dry_run" in x_watch.run.__code__.co_consts)
    import inspect as _inspect
    x_watch_run_src = _inspect.getsource(x_watch.run)
    check(
        "x_watch dry-run does not persist fetched tweets as seen",
        "persist_seen=False" in x_watch_run_src,
    )
    check("x_watch never dedupes raw fetches as seen", "dedupes on sent alerts" in x_watch_run_src)

    import pathlib as _pl
    pyproj = (_pl.Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    check(
        "pyproject.toml declares routr-x-watch console script",
        "routr-x-watch" in pyproj and "routr_signal.tasks.x_watch:cli" in pyproj,
    )

    pipeline_yml = (_pl.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "pipeline.yml").read_text(encoding="utf-8")
    check("pipeline.yml has fast X watch cron", '"5,20,35,50 * * * *"' in pipeline_yml)
    check("pipeline.yml has x_watch job", "x_watch:" in pipeline_yml)
    check("pipeline.yml wires dry_run into x_watch", "ROUTR_X_WATCH_DRY_RUN" in pipeline_yml)
    check("pipeline.yml exposes x_watch dry-run tuning inputs", "x_watch_window_minutes" in pipeline_yml and "ROUTR_X_WATCH_MIN_AGE_MINUTES" in pipeline_yml and "ROUTR_X_WATCH_MIN_SCORE" in pipeline_yml)
    check("pipeline.yml workflow_dispatch task choices include x_watch", "x_watch" in pipeline_yml)


def check_discord_dump_private_guardrails() -> None:
    section("[15] Discord dump private analysis guardrails")

    repo_root = Path(__file__).resolve().parent.parent
    gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")
    check(".gitignore blocks private Discord dump artifacts", "data/private/" in gitignore)

    try:
        from routr_signal.discord_dump.privacy import redact_sensitive_text
    except Exception as e:  # noqa: BLE001
        check("discord_dump privacy redactor imports", False, str(e))
        return

    sample = (
        "email me at founder@example.com or call +1 415 555 1212. "
        "invite https://discord.gg/private-room token Bearer sk-secret "
        "url https://example.com/path?utm_source=x&token=secret&keep=1 "
        "community " + "latent" + " " + "space" + " and https://" + "latent" + "." + "space/feed"
    )
    redacted = redact_sensitive_text(sample)
    check("privacy redactor removes email addresses", "founder@example.com" not in redacted)
    check("privacy redactor removes phone-like values", "415 555 1212" not in redacted)
    check("privacy redactor removes Discord invites", "discord.gg/private-room" not in redacted)
    check("privacy redactor removes bearer/token-looking secrets", "sk-secret" not in redacted)
    check("privacy redactor strips sensitive URL query values", "token=secret" not in redacted)
    check("privacy redactor removes source-specific community names", "latent" not in redacted.lower())


def check_discord_dump_loader() -> None:
    section("[15b] Discord dump loader and normalizer")

    try:
        from routr_signal.discord_dump.loader import normalize_record
        from routr_signal.discord_dump.types import NormalizedLeadRecord, NormalizedMessage
    except Exception as e:  # noqa: BLE001
        check("discord_dump loader imports", False, str(e))
        return

    discord_row = {
        "id": "1506705673617145968",
        "channel_id": "1209672547642249216",
        "content": "Shipping a useful agent eval writeup https://example.com/evals?utm_source=x",
        "timestamp": "2026-05-20T17:10:25.828000+00:00",
        "author": {"id": "718221565371744377", "username": "builder", "global_name": "Builder"},
        "embeds": [{"url": "https://example.com/evals", "title": "Agent evals"}],
        "thread": {"id": "thread-1", "name": "Agent evals"},
        "attachments": [{"url": "https://cdn.discordapp.com/file.png", "content_type": "image/png"}],
    }
    normalized = normalize_record(discord_row, source_file="sample/page-1.json")
    check("Discord message normalizes to NormalizedMessage", isinstance(normalized, NormalizedMessage))
    if isinstance(normalized, NormalizedMessage):
        check("Discord message preserves message id", normalized.message_id == "1506705673617145968")
        check("Discord message preserves real handle", normalized.author_username == "builder")
        check("Discord message captures thread id", normalized.thread_id == "thread-1")
        check("Discord message captures embed metadata", normalized.embeds[0].get("title") == "Agent evals")
        check("Discord message records source file", normalized.source_file == "sample/page-1.json")

    lead_row = {
        "repo_full_name": "owner/project",
        "owner": "owner",
        "html_url": "https://github.com/owner/project",
        "lead_score": 0.82,
    }
    lead = normalize_record(lead_row, source_file="sample/leads.json")
    check("non-Discord lead normalizes to NormalizedLeadRecord", isinstance(lead, NormalizedLeadRecord))
    if isinstance(lead, NormalizedLeadRecord):
        check("lead record keeps source file", lead.source_file == "sample/leads.json")
        check("lead record keeps original keys", "repo_full_name" in lead.raw)


def check_discord_dump_links() -> None:
    section("[15c] Discord dump URL canonicalization and policy")

    try:
        from routr_signal.discord_dump.links import (
            canonicalize_url,
            classify_domain,
            extract_links_from_message,
            is_crawl_eligible,
        )
        from routr_signal.discord_dump.types import NormalizedMessage
    except Exception as e:  # noqa: BLE001
        check("discord_dump link helpers import", False, str(e))
        return

    check(
        "X status URLs canonicalize across hosts",
        canonicalize_url("https://twitter.com/swyx/status/123?s=20") == "https://x.com/i/status/123",
    )
    check(
        "YouTube watch URLs canonicalize to video id",
        canonicalize_url("https://www.youtube.com/watch?v=abc123&utm_source=x") == "https://youtube.com/watch?v=abc123",
    )
    check(
        "arXiv PDFs canonicalize to abs page",
        canonicalize_url("https://arxiv.org/pdf/2401.12345v2.pdf") == "https://arxiv.org/abs/2401.12345",
    )
    check(
        "GitHub URLs strip tracking and trailing slash",
        canonicalize_url("https://github.com/Owner/Repo/?utm_source=x") == "https://github.com/Owner/Repo",
    )
    check("Discord private channel URLs are not crawl eligible", not is_crawl_eligible("https://discord.com/channels/1/2/3"))
    check("Discord CDN assets are not crawl eligible", not is_crawl_eligible("https://cdn.discordapp.com/file.png"))
    check("image assets are not crawl eligible", not is_crawl_eligible("https://example.com/og-image.jpg"))
    check("blog pages are crawl eligible", is_crawl_eligible("https://example.com/post"))
    check("X status domain class is x", classify_domain("https://x.com/i/status/123") == "x")
    check("YouTube domain class is youtube", classify_domain("https://youtu.be/abc123") == "youtube")

    msg = NormalizedMessage(
        message_id="m1",
        channel_id="c1",
        content="Read https://example.com/post?utm_source=x and https://discord.gg/private",
        timestamp=None,
        source_file="sample.json",
        embeds=[{"url": "https://youtu.be/abc123", "title": "Demo"}],
        attachments=[{"url": "https://cdn.discordapp.com/file.png"}],
    )
    links = extract_links_from_message(msg)
    canon = {link.canonical_url for link in links}
    check("extract_links_from_message includes content URL", "https://example.com/post" in canon)
    check("extract_links_from_message includes embed URL", "https://youtube.com/watch?v=abc123" in canon)
    check("extract_links_from_message excludes private/media URLs", all("discord" not in u for u in canon))


def check_discord_dump_crawl_queue() -> None:
    section("[15d] Discord dump bounded crawl queue")

    try:
        from routr_signal.discord_dump.crawl_queue import CrawlLimits, build_crawl_queue
        from routr_signal.discord_dump.types import ExtractedLink
    except Exception as e:  # noqa: BLE001
        check("discord_dump crawl queue imports", False, str(e))
        return

    def link(url: str, message_id: str, domain_class: str = "web") -> ExtractedLink:
        return ExtractedLink(
            raw_url=url,
            canonical_url=url,
            source_message_id=message_id,
            source_field="content",
            domain_class=domain_class,  # type: ignore[arg-type]
            crawl_eligible=True,
        )

    links = [
        link("https://example.com/a", "m1"),
        link("https://example.com/b", "m1"),
        link("https://other.com/c", "m1"),
        link("https://third.com/d", "m1"),
        link("https://github.com/o/r1", "m2", "github"),
        link("https://github.com/o/r2", "m3", "github"),
        link("https://github.com/o/r3", "m4", "github"),
        link("https://x.com/i/status/1", "m5", "x"),
        link("https://x.com/i/status/2", "m6", "x"),
        link("https://youtube.com/watch?v=a", "m7", "youtube"),
        link("https://twitter.com/u/status/3", "m8", "x"),
        link("https://fxtwitter.com/u/status/4", "m9", "x"),
    ]
    limits = CrawlLimits(
        max_urls=5,
        max_urls_per_message=2,
        max_urls_per_domain_per_message=1,
        default_domain_cap=1,
        domain_caps={"github.com": 2, "x.com": 1, "youtube.com": 1},
        class_caps={"x": 1},
    )
    queue = build_crawl_queue(links, limits=limits)
    queued = [item.link.canonical_url for item in queue]
    check("crawl queue respects global cap", len(queue) == 5)
    check("crawl queue caps URLs per message", sum(1 for item in queue if item.link.source_message_id == "m1") == 2)
    check("crawl queue caps same domain per message", not ({"https://example.com/a", "https://example.com/b"} <= set(queued)))
    check("crawl queue applies domain-specific GitHub cap", sum(1 for item in queue if item.domain == "github.com") == 2)
    check("crawl queue applies X class cap across URL variants", sum(1 for item in queue if item.link.domain_class == "x") == 1)
    check("crawl queue is deterministic", queued == [item.link.canonical_url for item in build_crawl_queue(links, limits=limits)])


def check_discord_dump_enrichment_fallbacks() -> None:
    section("[15e] Discord dump enrichment safe fallbacks")

    try:
        from routr_signal.discord_dump.crawl_queue import CrawlQueueItem
        from routr_signal.discord_dump.enrich import (
            crawl_result_from_embed,
            enrich_live,
            enrich_dry_run,
            summarize_enrichment_health,
            x_blocked_fallback,
            youtube_transcript_unavailable,
        )
        from routr_signal.discord_dump.types import CrawlResult, ExtractedLink
    except Exception as e:  # noqa: BLE001
        check("discord_dump enrichment helpers import", False, str(e))
        return

    x_link = ExtractedLink(
        raw_url="https://twitter.com/u/status/1",
        canonical_url="https://x.com/i/status/1",
        source_message_id="m1",
        source_field="content",
        domain_class="x",
        crawl_eligible=True,
    )
    embed = {
        "url": "https://twitter.com/u/status/1",
        "title": "Useful launch",
        "description": "Contact me at founder@example.com about the launch.",
        "content_type": "text/html",
    }
    from_embed = crawl_result_from_embed(x_link, embed)
    check("embed fallback returns CrawlResult", isinstance(from_embed, CrawlResult))
    check("embed fallback keeps canonical URL", from_embed.canonical_url == "https://x.com/i/status/1")
    check("embed fallback redacts sensitive text", "founder@example.com" not in from_embed.text)
    check("embed fallback records text hash", bool(from_embed.text_hash))

    x_fallback = x_blocked_fallback(x_link, embed=embed, reason="blocked by login")
    check("X blocked fallback marks failed status", x_fallback.status == "failed")
    check("X blocked fallback records failure reason", "blocked by login" in (x_fallback.failure_reason or ""))
    check("X blocked fallback preserves embed text", "Useful launch" in x_fallback.text)

    yt_link = ExtractedLink(
        raw_url="https://youtu.be/abc123",
        canonical_url="https://youtube.com/watch?v=abc123",
        source_message_id="m2",
        source_field="embed.url",
        domain_class="youtube",
        crawl_eligible=True,
    )
    yt_fallback = youtube_transcript_unavailable(
        yt_link,
        embed={"title": "Demo video", "description": "Transcript is not available."},
    )
    check("YouTube transcript fallback is non-fatal", yt_fallback.status == "metadata_only")
    check("YouTube transcript fallback records reason", "transcript unavailable" in (yt_fallback.failure_reason or ""))

    dry = enrich_dry_run([CrawlQueueItem(link=x_link, domain="x.com", priority=35)])
    check("dry-run enrichment returns one result per queue item", len(dry) == 1)
    check("dry-run enrichment makes no live fetch", dry[0].status == "skipped" and "dry run" in (dry[0].failure_reason or ""))

    web_link = ExtractedLink(
        raw_url="https://example.com/post",
        canonical_url="https://example.com/post",
        source_message_id="m3",
        source_field="content",
        domain_class="web",
        crawl_eligible=True,
    )
    attempted: list[str] = []

    def fake_fetch(url: str) -> tuple[str, str, bool]:
        attempted.append(url)
        return "text/html", "<html><title>Fetched</title><body>Actual page text</body></html>", False

    live = enrich_live([CrawlQueueItem(link=web_link, domain="example.com", priority=40)], fetcher=fake_fetch)
    check("live enrichment attempts fetch before fallback", attempted == ["https://example.com/post"])
    check("live enrichment returns fetched result", live[0].status == "fetched" and "Actual page text" in live[0].text)

    fallback_health = summarize_enrichment_health([x_fallback, yt_fallback, dry[0]], max_fallback_rate=0.5)
    check("enrichment health flags excessive fallback", not fallback_health["healthy"])
    check("enrichment health reports fallback rate", fallback_health["fallback_rate"] == 1.0)


def check_discord_dump_cli_surface() -> None:
    section("[15f] Discord dump analyzer CLI and artifacts")

    try:
        from routr_signal.tasks import discord_dump_analyze
    except Exception as e:  # noqa: BLE001
        check("discord dump analyzer task imports", False, str(e))
        return

    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        input_dir = root / "input"
        output_root = root / "out"
        input_dir.mkdir()
        (input_dir / "messages.json").write_text(
            json.dumps(
                [
                    {
                        "id": "m1",
                        "channel_id": "c1",
                        "content": "Useful writeup https://example.com/post from founder@example.com",
                        "timestamp": "2026-05-20T17:10:25+00:00",
                        "author": {"id": "u1", "username": "builder"},
                        "embeds": [{"url": "https://youtu.be/abc123", "title": "Demo"}],
                    },
                    {"repo_full_name": "owner/project", "html_url": "https://github.com/owner/project"},
                    {"unknown": "shape"},
                ]
            ),
            encoding="utf-8",
        )
        result = discord_dump_analyze.run_analysis(
            input_path=input_dir,
            output_root=output_root,
            run_id="test-run",
            max_crawl_urls=10,
            dry_run=True,
        )
        run_dir = output_root / "test-run"
        check("discord dump analyzer returns run directory", result.output_dir == run_dir)
        expected = {
            "run_manifest.json",
            "messages.normalized.jsonl",
            "leads.normalized.jsonl",
            "unsupported.records.jsonl",
            "links.index.jsonl",
            "crawl_queue.jsonl",
            "crawl_results.jsonl",
            "messages.csv",
            "links.csv",
            "people.csv",
            "summary.md",
            "operator_brief.md",
        }
        check("discord dump analyzer writes expected artifacts", expected <= {p.name for p in run_dir.iterdir()})
        normalized_text = (run_dir / "messages.normalized.jsonl").read_text(encoding="utf-8")
        check("discord dump analyzer artifacts redact email", "founder@example.com" not in normalized_text)
        people_csv = (run_dir / "people.csv").read_text(encoding="utf-8")
        links_csv = (run_dir / "links.csv").read_text(encoding="utf-8")
        brief = (run_dir / "operator_brief.md").read_text(encoding="utf-8")
        check("discord dump analyzer writes people CSV", "builder" in people_csv and "message_count" in people_csv)
        check("discord dump analyzer writes links CSV", "https://example.com/post" in links_csv and "crawl_status" in links_csv)
        check("operator brief points to spreadsheet files", "people.csv" in brief and "messages.csv" in brief and "links.csv" in brief)
        manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        check("discord dump analyzer manifest records counts", manifest["discord_messages"] == 1 and manifest["lead_records"] == 1 and manifest["unsupported_records"] == 1)

        fetched: list[str] = []

        def fake_fetcher(url: str) -> tuple[str, str, bool]:
            fetched.append(url)
            return "text/html", "<html><title>Live</title><body>Fetched body</body></html>", False

        live_result = discord_dump_analyze.run_analysis(
            input_path=input_dir,
            output_root=output_root,
            run_id="test-live-run",
            max_crawl_urls=10,
            dry_run=False,
            fetcher=fake_fetcher,
        )
        live_results = (live_result.output_dir / "crawl_results.jsonl").read_text(encoding="utf-8")
        check("discord dump analyzer live mode uses injected fetcher", fetched == ["https://example.com/post"])
        check("discord dump analyzer live mode writes fetched results", "\"status\": \"fetched\"" in live_results and "Fetched body" in live_results)

    pyproj = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    check("pyproject.toml declares routr-discord-dump-analyze", "routr-discord-dump-analyze" in pyproj)
    check("discord dump analyzer exposes --live flag", "--live" in inspect.getsource(discord_dump_analyze.cli))


def check_x_burst_discord_approval_surface() -> None:
    section("[15g] X-burst Discord approval deployment surface")

    from routr_signal.lib import discord_inbox
    from routr_signal.lib import signal_store
    from routr_signal.tasks import dispatch_approved, x_burst

    check("discord_inbox exports DM config check", hasattr(discord_inbox, "is_dm_configured"))
    check("discord_inbox exports channel-aware DM sender", hasattr(discord_inbox, "send_dm_with_channel"))
    check("x_burst defines auto-dispatch cap", getattr(x_burst, "X_BURST_DISPATCH_CAP", 0) > 0)
    check("x_burst records short posts as pending for approval", "status=\"pending\"" in inspect.getsource(x_burst.run))
    check("x_burst records Discord DM refs on run", "record_run_discord_messages" in inspect.getsource(x_burst.run))
    check("dispatch handles x_burst runs", "kind == \"x_burst\"" in inspect.getsource(dispatch_approved.run))
    check("dispatch has x_burst handler", hasattr(dispatch_approved, "_handle_x_burst_run"))

    old_pending = signal_store.pending_posts_for_run
    old_buffer_configured = dispatch_approved.buffer_client.is_configured
    old_create_post = dispatch_approved.buffer_client.create_post
    old_get_message = dispatch_approved.discord_inbox.get_message
    old_add_reaction = dispatch_approved.discord_inbox.add_bot_reaction
    old_update = signal_store.update_post_status
    old_env_required = dispatch_approved.env_required
    calls: dict[str, Any] = {"created": 0, "updated": [], "reacted": []}

    class _Created:
        id = "buffer-1"

    try:
        signal_store.pending_posts_for_run = lambda run_id: [  # type: ignore[assignment]
            {"id": "post-1", "kind": "x_burst", "platform": "x", "text": "ship this", "hook_format": "x_thread"}
        ]
        dispatch_approved.buffer_client.is_configured = lambda: True  # type: ignore[assignment]

        def _create_post(**kwargs: Any) -> _Created:
            calls["created"] += 1
            return _Created()

        dispatch_approved.buffer_client.create_post = _create_post  # type: ignore[assignment]
        dispatch_approved.discord_inbox.get_message = lambda channel_id, message_id: {  # type: ignore[assignment]
            "reactions": [{"me": False, "emoji": {"name": dispatch_approved.HOOK_APPROVAL_EMOJI}}]
        }
        dispatch_approved.discord_inbox.add_bot_reaction = lambda channel_id, message_id, emoji: calls["reacted"].append((channel_id, message_id, emoji)) or True  # type: ignore[assignment]
        signal_store.update_post_status = lambda post_id, **kwargs: calls["updated"].append((post_id, kwargs))  # type: ignore[assignment]
        dispatch_approved.env_required = lambda key: "buffer-channel"  # type: ignore[assignment]

        processed, failed, skipped = dispatch_approved._handle_x_burst_run(
            {"id": "run-x"}, ["dm-channel:dm-message"]
        )
        check("x_burst dispatch processes approved DM", (processed, failed, skipped) == (1, 0, 0))
        check("x_burst dispatch creates one Buffer post", calls["created"] == 1)
        check("x_burst dispatch marks post posted", calls["updated"] and calls["updated"][0][1]["status"] == "posted")
        check("x_burst dispatch marks Discord message processed", calls["reacted"] == [("dm-channel", "dm-message", dispatch_approved.BOT_PROCESSED_MARKER)])
    finally:
        signal_store.pending_posts_for_run = old_pending  # type: ignore[assignment]
        dispatch_approved.buffer_client.is_configured = old_buffer_configured  # type: ignore[assignment]
        dispatch_approved.buffer_client.create_post = old_create_post  # type: ignore[assignment]
        dispatch_approved.discord_inbox.get_message = old_get_message  # type: ignore[assignment]
        dispatch_approved.discord_inbox.add_bot_reaction = old_add_reaction  # type: ignore[assignment]
        signal_store.update_post_status = old_update  # type: ignore[assignment]
        dispatch_approved.env_required = old_env_required  # type: ignore[assignment]


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
    check_distribution_modules()
    check_posts_table()
    check_new_sources_register()
    check_topic_frequency()
    check_people_tables()
    check_hook_source_link()
    check_x_burst_surface()
    check_x_watch_surface()
    check_discord_dump_private_guardrails()
    check_discord_dump_loader()
    check_discord_dump_links()
    check_discord_dump_crawl_queue()
    check_discord_dump_enrichment_fallbacks()
    check_discord_dump_cli_surface()
    check_x_burst_discord_approval_surface()

    # ----- Live idempotence ------
    try:
        check_idempotence_live()
    except Exception as e:
        check("idempotence check executed without crash", False, str(e))

    # ----- LLM variance (informational) ------
    section("[16] LLM classifier variance across iterations (informational)")
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
