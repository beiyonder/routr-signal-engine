"""Smoke test: live GitHub issues source."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from routr_signal.sources import github_issues


def main() -> int:
    items = github_issues.fetch()
    print(f"github_issues fetched {len(items)} items")
    for it in items[:8]:
        repo = it.extra.get("repo", "?")
        number = it.extra.get("number", "?")
        labels = it.extra.get("labels", [])
        print(f"  [{repo}#{number}] @{it.author} labels={labels}")
        print(f"    title: {it.title[:120]}")
        print(f"    url:   {it.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
