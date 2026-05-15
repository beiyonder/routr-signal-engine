"""Lazy environment-variable accessor. Loads a .env file if present (local dev)."""

from __future__ import annotations

import os
from pathlib import Path

from .paths import project_root


_dotenv_loaded = False


def _load_dotenv() -> None:
    """Tiny .env loader. We don't depend on python-dotenv to keep the dep tree minimal."""

    global _dotenv_loaded
    if _dotenv_loaded:
        return
    env_path: Path = project_root() / ".env"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Don't overwrite already-set env (e.g., from GitHub Actions secrets).
            os.environ.setdefault(key, value)
    _dotenv_loaded = True


def env(name: str, default: str | None = None) -> str | None:
    _load_dotenv()
    return os.environ.get(name, default)


def env_required(name: str) -> str:
    val = env(name)
    if not val:
        raise RuntimeError(f"Required environment variable {name} is not set.")
    return val


def env_flag(name: str, default: bool = False) -> bool:
    val = env(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: list[str] | None = None) -> list[str]:
    val = env(name)
    if not val:
        return default or []
    return [s.strip() for s in val.split(",") if s.strip()]
