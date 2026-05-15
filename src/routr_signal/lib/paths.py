"""Centralized path resolution. Every file written by the pipeline goes through here."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def project_root() -> Path:
    """The repo root — the directory containing pyproject.toml."""

    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError(
        "Could not locate project root; expected to find pyproject.toml in an ancestor of "
        f"{here}"
    )


def config_dir() -> Path:
    return project_root() / "config"


def prompts_dir() -> Path:
    return config_dir() / "prompts"


def data_dir() -> Path:
    d = project_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def seen_dir() -> Path:
    d = data_dir() / "seen"
    d.mkdir(parents=True, exist_ok=True)
    return d


def leads_dir() -> Path:
    d = data_dir() / "leads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def digests_dir() -> Path:
    d = data_dir() / "digests"
    d.mkdir(parents=True, exist_ok=True)
    return d


def raw_dir(source: str | None = None) -> Path:
    d = data_dir() / "raw"
    if source:
        d = d / source
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    d = data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def manual_dir(subdir: str | None = None) -> Path:
    """Local-only manual input dir (gitignored). Used by Discord paste-in source."""

    d = data_dir() / "manual"
    if subdir:
        d = d / subdir
    d.mkdir(parents=True, exist_ok=True)
    return d


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
