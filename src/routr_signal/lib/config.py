"""YAML config loader. One function per file, cached for the process lifetime."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .paths import config_dir, prompts_dir


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path} did not parse to a dict: got {type(data).__name__}")
    return data


@lru_cache(maxsize=1)
def keywords() -> dict[str, Any]:
    return _load_yaml(config_dir() / "keywords.yaml")


@lru_cache(maxsize=1)
def subreddits() -> dict[str, Any]:
    return _load_yaml(config_dir() / "subreddits.yaml")


@lru_cache(maxsize=1)
def github_repos() -> dict[str, Any]:
    return _load_yaml(config_dir() / "github_repos.yaml")


@lru_cache(maxsize=1)
def hn_config() -> dict[str, Any]:
    return _load_yaml(config_dir() / "hn.yaml")


@lru_cache(maxsize=1)
def twitter_watch() -> dict[str, Any]:
    """X/Twitter source config. Returns {} if file is absent (source becomes a no-op)."""

    path = config_dir() / "twitter_watch.yaml"
    if not path.exists():
        return {}
    return _load_yaml(path)


@lru_cache(maxsize=1)
def x_fast_watch() -> dict[str, Any]:
    """Fast X reply-monitor config. Returns {} if absent."""

    path = config_dir() / "x_fast_watch.yaml"
    if not path.exists():
        return {}
    return _load_yaml(path)


@lru_cache(maxsize=1)
def hf_papers() -> dict[str, Any]:
    """HuggingFace Papers source config. Returns {} if file is absent (source becomes a no-op)."""

    path = config_dir() / "hf_papers.yaml"
    if not path.exists():
        return {}
    return _load_yaml(path)


@lru_cache(maxsize=1)
def newsletters() -> dict[str, Any]:
    """Newsletter RSS source config. Returns {} if file is absent (source becomes a no-op)."""

    path = config_dir() / "newsletters.yaml"
    if not path.exists():
        return {}
    return _load_yaml(path)


@lru_cache(maxsize=None)
def prompt(name: str) -> str:
    """Load a system prompt from config/prompts/<name>.md."""

    path = prompts_dir() / f"{name}.md"
    return path.read_text(encoding="utf-8")


def keyword_phrases() -> list[str]:
    """Flat list of substring filters for cheap pre-classification."""

    return [s.lower() for s in keywords().get("must_match_any", [])]


def suppress_phrases() -> list[str]:
    return [s.lower() for s in keywords().get("suppress_if_contains", [])]
