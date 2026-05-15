"""One-liner logger. Writes to stderr so GitHub Actions captures it cleanly."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from .env import env_flag


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def info(msg: str) -> None:
    print(f"[{_ts()}] INFO  {msg}", file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(f"[{_ts()}] WARN  {msg}", file=sys.stderr, flush=True)


def error(msg: str) -> None:
    print(f"[{_ts()}] ERROR {msg}", file=sys.stderr, flush=True)


def debug(msg: str) -> None:
    if env_flag("ROUTR_SIGNAL_DEBUG"):
        print(f"[{_ts()}] DEBUG {msg}", file=sys.stderr, flush=True)
