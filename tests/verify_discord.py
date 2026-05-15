"""Tiny handshake post to confirm the configured Discord webhook is alive.

Builds a minimal Digest with a single note and no signals/hooks/leads, so the
target channel receives one short "webhook check" message rather than a real
digest. Run after rotating DISCORD_WEBHOOK_URL.
"""

from __future__ import annotations

import io
import sys
from datetime import datetime, timezone
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from routr_signal.lib.types import Digest
from routr_signal.output import discord as discord_out


def main() -> int:
    digest = Digest(
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        pain_signals=[],
        active_accounts=[],
        hooks=[],
        source_counts={},
        notes=["webhook check after channel rotation. no signals in this message."],
    )
    ok = discord_out.publish(digest)
    print(f"discord publish ok: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
