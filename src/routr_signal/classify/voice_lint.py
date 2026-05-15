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
}

LENGTH_LIMITS: dict[HookFormat, int] = {
    "x_thread": 280,
    "linkedin": 230,
    "reddit": 110,
    "hn_comment": 420,
    "devto_title": 90,
}

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

    # 4. case rule
    if hook.format in LOWERCASE_FORMATS:
        ratio = _uppercase_ratio(text)
        if ratio > 0.25:
            violations.append(
                f"uppercase ratio {ratio:.2f} > 0.25 for lowercase-only format {hook.format}"
            )

    # 5. length
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
