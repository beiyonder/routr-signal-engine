"""Beehiiv API v2 client.

Docs: https://developers.beehiiv.com/api-reference/

Auth: Bearer token. We use the v2 endpoint base
`https://api.beehiiv.com/v2/publications/<publication_id>/...`.

The `BEEHIIV_PUBLICATION_ID` we store is in the v2 format (`pub_<uuid>`).

We use a single endpoint today:
  - POST /v2/publications/<pub_id>/posts — create a post as a draft.
    The newsletter doesn't send automatically; the user reviews on Beehiiv
    and clicks Send when ready.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from .env import env_required
from .logging import debug, info, warn


API_BASE = "https://api.beehiiv.com/v2"
DEFAULT_TIMEOUT = 30.0


class BeehiivError(RuntimeError):
    pass


@dataclass(slots=True)
class CreatedBeehiivPost:
    id: str
    status: str | None
    web_url: str | None
    raw: dict[str, Any]


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {env_required('BEEHIIV_API_KEY')}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def create_draft_post(
    *,
    title: str,
    subtitle: str | None = None,
    body_markdown: str,
    body_html: str | None = None,
    publication_id: str | None = None,
) -> CreatedBeehiivPost:
    """Create a draft post in Beehiiv.

    Beehiiv's create-post endpoint accepts either Markdown or HTML for the
    body. We default to Markdown (cleaner round-trip from our synthesis
    output). HTML is supported if the caller already has rendered content.

    The created post lands in the publication's drafts; it does NOT send
    automatically. The user reviews, edits, and triggers send in Beehiiv's UI.

    Endpoint: POST /v2/publications/<pub_id>/posts
    """

    pub_id = publication_id or env_required("BEEHIIV_PUBLICATION_ID")
    url = f"{API_BASE}/publications/{pub_id}/posts"

    payload: dict[str, Any] = {
        "title": title,
        "status": "draft",  # never auto-send; the user clicks send in the UI
    }
    if subtitle:
        payload["subtitle"] = subtitle
    if body_html:
        payload["body_html"] = body_html
    else:
        payload["body_content"] = body_markdown
        payload["body_content_format"] = "markdown"

    info(f"beehiiv: create_draft_post pub={pub_id} title={title!r} body_len={len(body_markdown)}")

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        resp = httpx.post(url, headers=_headers(), content=body, timeout=DEFAULT_TIMEOUT)
    except httpx.HTTPError as e:
        raise BeehiivError(f"beehiiv: network error: {e}") from e

    if resp.status_code not in (200, 201):
        snippet = resp.text[:500] if resp.text else ""
        raise BeehiivError(f"beehiiv: HTTP {resp.status_code} on create post: {snippet!r}")

    try:
        data = resp.json()
    except ValueError as e:
        raise BeehiivError(f"beehiiv: non-JSON response: {resp.text[:300]!r}") from e

    # Beehiiv v2 envelopes responses with `data: { ... }`.
    post_obj = data.get("data") if isinstance(data, dict) else None
    if not isinstance(post_obj, dict):
        # Some endpoints return the post object directly; tolerate both.
        post_obj = data if isinstance(data, dict) else {}

    post_id = post_obj.get("id")
    if not isinstance(post_id, str):
        raise BeehiivError(f"beehiiv: created post missing id: {post_obj!r}")

    debug(f"beehiiv: created draft post id={post_id} status={post_obj.get('status')}")
    return CreatedBeehiivPost(
        id=post_id,
        status=post_obj.get("status"),
        web_url=post_obj.get("web_url") or post_obj.get("url"),
        raw=post_obj,
    )


def is_configured() -> bool:
    try:
        env_required("BEEHIIV_API_KEY")
        env_required("BEEHIIV_PUBLICATION_ID")
        return True
    except Exception:
        return False
