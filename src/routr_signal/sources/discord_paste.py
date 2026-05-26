"""Discord paste-in source.

Discord aggressively bans accounts that automate against their API or DOM. We
side-step the ToS risk entirely: paste interesting messages from any Discord
channel into markdown files under
`data/manual/discord-pastes/` and the pipeline ingests them on the next run.

Expected markdown file shape (one file per channel-per-day works well):

    # ai-community / general / 2026-05-15

    @username  https://discord.com/channels/SERVER/CHANNEL/MSG_ID
        First message body. Indented so we know where it starts.
        Multi-line works fine; keep indentation.

    @another  https://discord.com/channels/...
        Another message.

Rules:
    * Filename is informational; we only parse contents.
    * Each message starts with a line that begins with `@`, contains the
      author handle, and a Discord deep link (used as both URL and dedupe key).
    * Message body is the indented block until the next `@` line or blank line.
    * Channel name is inferred from the H1 header at the top of the file.

We dedupe by the deep link, so re-running across the same paste file is safe.
Files are NEVER deleted by the pipeline. You manage your inbox.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..lib.dedupe import SeenStore
from ..lib.filters import prefilter
from ..lib.logging import debug, info, warn
from ..lib.paths import manual_dir
from ..lib.types import RawItem


SOURCE = "discord"  # NOTE: not currently in the Platform literal. See main.py.

# Author line: "@handle  https://discord.com/channels/.../.../...."
# We accept either tab or 2+ spaces between handle and URL.
_AUTHOR_LINE_RE = re.compile(
    r"^@(?P<handle>[A-Za-z0-9_.\-]+)\s+(?P<url>https?://\S*discord\S*)\s*$"
)

# H1 header at the top of a paste file (best-effort channel naming).
_H1_RE = re.compile(r"^#\s+(?P<title>.+?)\s*$")


def _short_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


def _parse_paste_file(path: Path) -> list[RawItem]:
    """Parse one paste file. Skips authoring instructions and the H1 header line."""

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    # Find the channel name from the first H1, if any.
    channel = "unknown"
    for ln in lines[:5]:
        m = _H1_RE.match(ln)
        if m:
            channel = m.group("title").strip()
            break

    items: list[RawItem] = []
    cur_handle: str | None = None
    cur_url: str | None = None
    cur_body: list[str] = []
    file_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    def _flush() -> None:
        nonlocal cur_handle, cur_url, cur_body
        if cur_handle is None or cur_url is None:
            return
        body = "\n".join(cur_body).strip()
        if not body:
            cur_handle, cur_url, cur_body = None, None, []
            return
        item_id = f"{SOURCE}-{_short_hash(cur_url)}"
        items.append(
            RawItem(
                id=item_id,
                source=SOURCE,  # type: ignore[arg-type]  # see main.py for literal extension note
                title=body[:120].replace("\n", " "),
                body=body,
                url=cur_url,
                author=f"@{cur_handle}",
                created_at=file_mtime,
                extra={
                    "channel": channel,
                    "via": "paste",
                    "file": str(path.relative_to(path.parents[2])),
                },
            )
        )
        cur_handle, cur_url, cur_body = None, None, []

    for raw in lines:
        if not raw.strip():
            # Blank line ends the current message, but keep going to find the next one.
            _flush()
            continue
        m = _AUTHOR_LINE_RE.match(raw.strip())
        if m:
            _flush()
            cur_handle = m.group("handle")
            cur_url = m.group("url")
            cur_body = []
            continue
        if cur_handle is not None:
            # Strip leading 4-space or tab indent if present; treat anything else as body.
            stripped = raw.lstrip()
            cur_body.append(stripped)

    _flush()
    return items


def fetch() -> list[RawItem]:
    paste_dir = manual_dir("discord-pastes")
    files = sorted(paste_dir.glob("*.md"))
    files = [f for f in files if f.name != "README.md"]
    if not files:
        debug("discord_paste: no .md files under data/manual/discord-pastes/; nothing to do")
        return []

    seen = SeenStore(SOURCE)
    collected: list[RawItem] = []
    for f in files:
        try:
            items = _parse_paste_file(f)
        except Exception as e:  # noqa: BLE001
            warn(f"discord_paste: failed to parse {f.name}: {e}")
            continue
        added = 0
        for it in items:
            if seen.has(it.id):
                continue
            seen.add_item(it)
            collected.append(it)
            added += 1
        debug(f"discord_paste: {f.name} -> {added} new items ({len(items)} parsed)")

    filtered = prefilter(collected)
    info(f"discord_paste: {len(collected)} new, {len(filtered)} pass keyword prefilter")
    seen.save()
    return filtered


if __name__ == "__main__":
    out = fetch()
    print(json.dumps([it.to_dict() for it in out], indent=2))
