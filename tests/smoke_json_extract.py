"""Smoke test the LLM JSON extractor against common malformed shapes."""

from __future__ import annotations

import io
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from routr_signal.classify.client import _extract_json  # noqa: E402


FENCE = "```"


CASES: list[tuple[str, str, dict | None]] = [
    (
        "balanced-fence",
        f'{FENCE}json\n{{"items":[{{"id":"x-1"}}]}}\n{FENCE}',
        {"items": [{"id": "x-1"}]},
    ),
    (
        "fence-no-json-tag",
        f'{FENCE}\n{{"items":[]}}\n{FENCE}',
        {"items": []},
    ),
    (
        "unclosed-fence",
        f'{FENCE}json\n{{"items":[{{"id":"x-1"}}]}}',
        {"items": [{"id": "x-1"}]},
    ),
    (
        "trailing-fence-only",
        '{"items":[]}\n' + FENCE,
        {"items": []},
    ),
    (
        "preamble-and-balanced-fence",
        f'Here is the output you asked for:\n\n{FENCE}json\n{{"items":[{{"id":"x-9"}}]}}\n{FENCE}',
        {"items": [{"id": "x-9"}]},
    ),
    (
        "plain-json",
        '{"items":[{"id":"x-1"}]}',
        {"items": [{"id": "x-1"}]},
    ),
    (
        "embedded-quotes",
        '{"items":[{"id":"x-1","summary":"They said \\"hi\\" once"}]}',
        {"items": [{"id": "x-1", "summary": 'They said "hi" once'}]},
    ),
]


def main() -> int:
    fails = 0
    for name, text, expected in CASES:
        try:
            got = _extract_json(text)
            ok = (expected is None) or (got == expected)
            mark = "PASS" if ok else "FAIL"
            print(f"{mark} {name:30s} -> {got!r}")
            if not ok:
                print(f"       expected: {expected!r}")
                fails += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {name:30s} -> {type(e).__name__}: {e}")
            fails += 1

    print()
    if fails:
        print(f"FAIL: {fails} of {len(CASES)} cases failed")
        return 1
    print(f"PASS: all {len(CASES)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
