"""Thin httpx wrapper with retry, jitter, and a per-host token-bucket rate limiter."""

from __future__ import annotations

import random
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx


@dataclass(slots=True)
class HostBudget:
    """How often we may hit a given host."""

    min_seconds_between_requests: float
    last_request_at: float = 0.0


_budgets: dict[str, HostBudget] = defaultdict(lambda: HostBudget(min_seconds_between_requests=0.0))


def set_budget(host: str, min_seconds_between_requests: float) -> None:
    _budgets[host] = HostBudget(min_seconds_between_requests=min_seconds_between_requests)


def _wait_for_host(url: str) -> None:
    host = urlparse(url).netloc
    if not host:
        return
    budget = _budgets[host]
    if budget.min_seconds_between_requests <= 0:
        return

    now = time.monotonic()
    elapsed = now - budget.last_request_at
    if elapsed < budget.min_seconds_between_requests:
        time.sleep(budget.min_seconds_between_requests - elapsed)
    budget.last_request_at = time.monotonic()


def get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
    backoff_seconds: float = 5.0,
    accept_status: tuple[int, ...] = (200,),
) -> httpx.Response:
    """GET with retry-on-5xx-or-429 and per-host throttling.

    Raises the final httpx.HTTPError if all retries fail. Returns the Response on success
    even if the status is in `accept_status` (which defaults to just 200).
    """

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        _wait_for_host(url)
        try:
            resp = httpx.get(
                url,
                headers=headers or {},
                params=params,
                timeout=timeout,
                follow_redirects=True,
            )
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < max_retries:
                _sleep_backoff(attempt, backoff_seconds)
                continue
            raise

        if resp.status_code in accept_status:
            return resp

        # Retry only on 429 + 5xx; everything else is a hard fail to surface upstream.
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt < max_retries:
                # Honor Retry-After if present.
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else None
                _sleep_backoff(attempt, backoff_seconds, override=wait)
                continue

        # Non-retryable failure.
        resp.raise_for_status()
        return resp  # unreachable, but appeases type checker

    assert last_exc is not None  # pragma: no cover
    raise last_exc


def _sleep_backoff(attempt: int, base: float, *, override: float | None = None) -> None:
    if override is not None:
        time.sleep(override)
        return
    # Exponential backoff with jitter.
    wait = base * (2**attempt) + random.uniform(0, base)
    time.sleep(wait)
