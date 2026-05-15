"""GitHub issues source — scans configured competitor repos for fresh open issues.

Uses the GITHUB_TOKEN provided in Actions (1000 req/hour/repo). Locally, set GITHUB_TOKEN
to a PAT with `public_repo` scope, or omit it (60 req/hour per IP).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from dateutil import parser as dateparser

from ..lib import http
from ..lib.config import github_repos
from ..lib.dedupe import SeenStore
from ..lib.env import env
from ..lib.filters import prefilter
from ..lib.logging import debug, info, warn
from ..lib.types import RawItem


SOURCE = "github"
API_BASE = "https://api.github.com"


def _headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "routr-signal-engine/0.1",
    }
    token = env("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _fetch_issues(
    owner: str,
    repo: str,
    *,
    labels: list[str],
    since_iso: str,
    per_page: int,
) -> list[dict[str, Any]]:
    url = f"{API_BASE}/repos/{owner}/{repo}/issues"
    params: dict[str, Any] = {
        "state": "open",
        "sort": "created",
        "direction": "desc",
        "since": since_iso,
        "per_page": per_page,
    }
    if labels:
        params["labels"] = ",".join(labels)

    try:
        resp = http.get(url, headers=_headers(), params=params, timeout=20.0)
        data = resp.json()
        if not isinstance(data, list):
            warn(f"github_issues: {owner}/{repo} returned non-list payload: {data!r}")
            return []
        return data
    except Exception as e:  # noqa: BLE001
        warn(f"github_issues: {owner}/{repo} fetch failed: {e}")
        return []


def _issue_to_item(issue: dict[str, Any], owner: str, repo: str) -> RawItem | None:
    # GitHub's /issues endpoint includes PRs; skip those.
    if "pull_request" in issue:
        return None

    number = issue.get("number")
    if not number:
        return None

    item_id = f"{SOURCE}-{owner}-{repo}-{number}"

    title = issue.get("title") or ""
    body = issue.get("body") or ""
    url = issue.get("html_url") or f"https://github.com/{owner}/{repo}/issues/{number}"
    author = (issue.get("user") or {}).get("login")

    created_raw = issue.get("created_at")
    try:
        created_at = dateparser.isoparse(created_raw) if created_raw else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        created_at = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    labels: list[str] = [l.get("name", "") for l in (issue.get("labels") or []) if isinstance(l, dict)]

    return RawItem(
        id=item_id,
        source=SOURCE,
        title=title,
        body=body,
        url=url,
        author=author,
        created_at=created_at,
        extra={"repo": f"{owner}/{repo}", "labels": labels, "number": number},
    )


def fetch() -> list[RawItem]:
    cfg = github_repos()
    lookback = int(cfg.get("lookback_hours", 30))
    since_dt = datetime.now(timezone.utc) - timedelta(hours=lookback)
    since_iso = since_dt.isoformat().replace("+00:00", "Z")

    seen = SeenStore(SOURCE)
    collected: list[RawItem] = []

    for r in cfg.get("repos", []):
        owner = r.get("owner")
        name = r.get("name")
        if not owner or not name:
            continue
        labels = list(r.get("labels") or [])
        max_items = int(r.get("max_items_per_run", 25))

        issues = _fetch_issues(owner, name, labels=labels, since_iso=since_iso, per_page=max_items)
        debug(f"github_issues: {owner}/{name} returned {len(issues)} issues since {since_iso}")

        added = 0
        for issue in issues[:max_items]:
            item = _issue_to_item(issue, owner, name)
            if item is None:
                continue
            if seen.has(item.id):
                continue
            seen.add_item(item)
            collected.append(item)
            added += 1
        debug(f"github_issues: {owner}/{name} → {added} new items")

    filtered = prefilter(collected)
    info(f"github_issues: {len(collected)} new items, {len(filtered)} pass keyword prefilter")

    seen.save()
    return filtered


if __name__ == "__main__":
    import json

    items = fetch()
    print(json.dumps([it.to_dict() for it in items], indent=2))
