"""Post-hook voice lint.

Catches drafter violations of the voice rules from `config/prompts/post_drafter.md`.
Runs as a soft check: violations are logged + appended to digest notes, but the
hook is still returned (we'd rather see a slightly off-rules hook than nothing).

Rules enforced:
    - no em-dash (--) or en-dash characters
    - no emoji
    - no banned marketing words / phrases
    - per-format case rule:
        x_thread, reddit  -> lowercase-dominant (the *prose* should be lowercase;
                             proper noun mixed-case is tolerated but flagged if
                             > 25% of letters are uppercase)
        linkedin, hn_comment, devto_title -> sentence case allowed
    - per-format length cap
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ..lib.types import HookFormat, PostHook


BANNED_PHRASES = {
    "leverage",
    "leverages",
    "leveraging",
    "unlock",
    "unlocks",
    "unlocking",
    "empower",
    "empowering",
    "empowers",
    "synergy",
    "synergies",
    "revolutionize",
    "revolutionary",
    "game-changer",
    "game changer",
    "supercharge",
    "supercharges",
    "next-level",
    "next level",
    "best-in-class",
    "elevate",
    "elevates",
    "harness",
    "harnesses",
    "robust",
    "seamless",
    "seamlessly",
    "scalable",
    "thought leadership",
    "thought leader",
    "ecosystem",
    "delve",
    "delves",
    "delving",
    "tapestry",
    "navigate the landscape",
    "in today's fast paced",
    "in today's rapidly evolving",
    "at the end of the day",
    "needless to say",
    "it goes without saying",
    "moving forward",
    "the bottom line",
}

# Length caps were originally tuned for "opener" drafts (LinkedIn was 230
# because we treated it as opener-only). After the 2026-05-18 prompt rewrite
# every hook is a complete standalone post, so the LinkedIn cap is now the
# full short-post target (350-650). x_thread stays at 280 (X's hard limit).
LENGTH_LIMITS: dict[HookFormat, int] = {
    "x_thread": 280,
    "linkedin": 700,    # post can run a bit over the 650 target without lint-flag spam
    "reddit": 110,
    "hn_comment": 420,
    "devto_title": 90,
}

# Rhetorical pivots that have become AI tells. Detected via regex so we
# catch variants like "isn't the X, it's Y" / "isn't X itself, but Y" / etc.
AI_RHETORICAL_PATTERNS: tuple[tuple[str, str], ...] = (
    # "it's not X, it's Y" / "this isn't a X, it's a Y"
    (
        "it's-not-X-it's-Y pivot",
        r"\b(?:it'?s|that'?s|this\s+is)\s+not\s+(?:a\s+|the\s+|just\s+|merely\s+)?\w+[^.]{0,40}?,?\s+(?:it'?s|that'?s|this\s+is)\s+(?:a\s+|the\s+)?",
    ),
    # "isn't X, but Y" / "isn't the X itself, but Y"
    (
        "isn't-X-but-Y pivot",
        r"\bisn'?t\s+(?:a\s+|the\s+|just\s+)?\w+[^.]{0,40}?,?\s+but\s+",
    ),
    # "the X isn't the Y, it's the Z" full sandwich form
    (
        "X-isn't-Y-it's-Z sandwich",
        r"\b(?:the\s+|a\s+)\w+\s+isn'?t\s+(?:the|a)\s+\w+[^.]{0,40}?,?\s+(?:it'?s|that'?s)\s+(?:the|a)\s+",
    ),
)

# Cliffhanger endings that read as "thread incoming" — banned everywhere
# (including formats that aren't openers anymore). Matched against the
# trailing tail of the text after stripping whitespace.
CLIFFHANGER_TAILS: tuple[str, ...] = (
    "here is why:",
    "here's why:",
    "here is how:",
    "here's how:",
    "here is the catch:",
    "here's the catch:",
    "more below.",
    "more in the replies.",
    "let me explain.",
    "thread incoming.",
    "1/n",
    "🧵",
)

LOWERCASE_FORMATS: set[HookFormat] = {"x_thread", "reddit"}

EMDASH_CHARS = {"\u2014", "\u2013"}  # em-dash, en-dash


@dataclass(slots=True)
class LintResult:
    hook: PostHook
    violations: list[str]


def _contains_emoji(text: str) -> bool:
    for ch in text:
        if unicodedata.category(ch).startswith("So"):
            return True
        # Pictographs span several Unicode blocks; the `So` category catches most.
        cp = ord(ch)
        # Common emoji ranges
        if (
            0x1F300 <= cp <= 0x1FAFF
            or 0x2600 <= cp <= 0x27BF
            or 0x1F000 <= cp <= 0x1F02F
        ):
            return True
    return False


def _uppercase_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters)


def _banned_hits(text: str) -> list[str]:
    lower = text.lower()
    hits: list[str] = []
    for phrase in BANNED_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", lower):
            hits.append(phrase)
    return hits


def lint_hook(hook: PostHook) -> LintResult:
    violations: list[str] = []
    text = hook.text or ""

    # 1. dashes
    if any(ch in text for ch in EMDASH_CHARS):
        violations.append("contains em-dash or en-dash")

    # 2. emoji
    if _contains_emoji(text):
        violations.append("contains emoji")

    # 3. banned phrases
    hits = _banned_hits(text)
    if hits:
        violations.append(f"banned phrases: {', '.join(hits)}")

    # 4. AI rhetorical pivots ("it's not X, it's Y", "isn't X but Y", etc.)
    lower = text.lower()
    for label, pattern in AI_RHETORICAL_PATTERNS:
        if re.search(pattern, lower):
            violations.append(f"AI rhetorical pivot: {label}")
            break  # one is enough; multiple from the same family is redundant

    # 5. cliffhanger endings (no posts end with "here's why:" or 🧵 etc.)
    tail = text.strip().lower()
    for tail_phrase in CLIFFHANGER_TAILS:
        if tail.endswith(tail_phrase):
            violations.append(f"cliffhanger tail: {tail_phrase!r}")
            break

    # 5b. for x_thread specifically: don't end with a colon (cliffhanger)
    if hook.format == "x_thread" and text.strip().endswith(":"):
        violations.append("x_thread ends with colon (cliffhanger)")

    # 6. case rule
    if hook.format in LOWERCASE_FORMATS:
        ratio = _uppercase_ratio(text)
        if ratio > 0.25:
            violations.append(
                f"uppercase ratio {ratio:.2f} > 0.25 for lowercase-only format {hook.format}"
            )

    # 7. length
    cap = LENGTH_LIMITS.get(hook.format)
    if cap and len(text) > cap:
        violations.append(f"length {len(text)} > cap {cap} for {hook.format}")

    return LintResult(hook=hook, violations=violations)


def lint_all(hooks: list[PostHook]) -> list[LintResult]:
    return [lint_hook(h) for h in hooks]


def summarize_violations(results: list[LintResult]) -> str | None:
    """Return a single short string summarizing violations, or None if clean."""

    issues = [
        f"{r.hook.format}: {', '.join(r.violations)}"
        for r in results
        if r.violations
    ]
    if not issues:
        return None
    return "drafter voice-lint flagged: " + "; ".join(issues)
