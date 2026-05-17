"""Buffer GraphQL client.

Buffer exposes its scheduling layer at `https://api.buffer.com/graphql`.

Auth: Bearer token (`BUFFER_ACCESS_TOKEN`). Free tier issues 1 personal token
per account; sufficient for our use.

What we use:
  - `query channels(input: {organizationId})` — list connected channels.
  - `mutation createPost(input: {channelId, text, schedulingType, mode, assets})`
    — publish or queue a post to a channel.

What Buffer does NOT expose via GraphQL (today):
  - Per-post engagement metrics (likes, retweets, impressions).
  - Account-level analytics dashboards.

Both of those live in Buffer's analytics product. We omit the feedback loop
for now and revisit when (a) we have a paid tier with engagement export, or
(b) we add direct X API metrics pulls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from .env import env_required
from .logging import debug, info, warn


GRAPHQL_ENDPOINT = "https://api.buffer.com/graphql"
DEFAULT_TIMEOUT = 25.0


# Buffer enum values discovered via schema introspection on 2026-05-17.
# See `.serena/memories/40-distribution-stack.md` for details.
VALID_SCHEDULING_TYPES = ("notification", "automatic")
VALID_SHARE_MODES = ("addToQueue", "shareNow", "shareNext", "customScheduled", "recommendedTime")


class BufferError(RuntimeError):
    """Raised when Buffer returns errors or an unparseable response."""


@dataclass(slots=True)
class CreatedPost:
    id: str
    status: str | None
    text: str | None
    raw: dict[str, Any]


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {env_required('BUFFER_ACCESS_TOKEN')}",
        "Content-Type": "application/json",
    }


def _post_query(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        resp = httpx.post(
            GRAPHQL_ENDPOINT,
            headers=_headers(),
            content=body,
            timeout=DEFAULT_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise BufferError(f"buffer: network error: {e}") from e

    if resp.status_code != 200:
        snippet = resp.text[:500] if resp.text else ""
        raise BufferError(f"buffer: HTTP {resp.status_code}: {snippet!r}")

    try:
        data = resp.json()
    except ValueError as e:
        raise BufferError(f"buffer: non-JSON response: {resp.text[:300]!r}") from e

    if data.get("errors"):
        raise BufferError(f"buffer: GraphQL errors: {data['errors']}")
    return data.get("data") or {}


# ---------------------------------------------------------------------------
# Account / channels (sanity + lookup)
# ---------------------------------------------------------------------------


def account() -> dict[str, Any]:
    """Return basic info on the authenticated account. Useful for sanity checks."""

    data = _post_query("query { account { id email } }")
    return data.get("account") or {}


def list_channels(organization_id: str) -> list[dict[str, Any]]:
    """List all connected channels under an organization."""

    q = "query Q($input: ChannelsInput!) { channels(input: $input) { id service name } }"
    data = _post_query(q, {"input": {"organizationId": organization_id}})
    chans = data.get("channels") or []
    return chans if isinstance(chans, list) else []


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


CREATE_POST_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess {
      post { id status text createdAt sentAt dueAt channelId channelService }
    }
    ... on NotFoundError       { message }
    ... on UnauthorizedError   { message }
    ... on UnexpectedError     { message }
    ... on RestProxyError      { message link code }
    ... on LimitReachedError   { message }
    ... on InvalidInputError   { message }
  }
}
"""


def create_post(
    *,
    channel_id: str,
    text: str,
    mode: str = "shareNow",
    scheduling_type: str = "automatic",
    save_to_draft: bool = False,
    metadata: dict[str, Any] | None = None,
) -> CreatedPost:
    """Publish a post via Buffer to the given channel.

    `mode` is one of VALID_SHARE_MODES:
      - shareNow:         publish immediately
      - addToQueue:       enqueue per the channel's posting schedule
      - shareNext:        skip the queue, publish at the next scheduled slot
      - customScheduled:  requires `dueAt` (not exposed by this helper)
      - recommendedTime:  let Buffer pick the slot

    `scheduling_type`: 'automatic' (publish via Buffer-Buffer auth) or
    'notification' (send a reminder; user posts manually). Default automatic.

    Buffer's response is a UNION `PostActionPayload`; we resolve via the
    `__typename` discriminator and raise `BufferError` on any non-success
    variant with the upstream message embedded.
    """

    if mode not in VALID_SHARE_MODES:
        raise ValueError(f"buffer: mode {mode!r} not in {VALID_SHARE_MODES}")
    if scheduling_type not in VALID_SCHEDULING_TYPES:
        raise ValueError(
            f"buffer: scheduling_type {scheduling_type!r} not in {VALID_SCHEDULING_TYPES}"
        )

    inp: dict[str, Any] = {
        "channelId": channel_id,
        "text": text,
        "mode": mode,
        "schedulingType": scheduling_type,
        "assets": [],
    }
    if save_to_draft:
        inp["saveToDraft"] = True
    if metadata:
        inp["metadata"] = metadata

    info(f"buffer: createPost channel={channel_id} mode={mode} len={len(text)}")
    data = _post_query(CREATE_POST_MUTATION, {"input": inp})
    cp = data.get("createPost")
    if not isinstance(cp, dict):
        raise BufferError(f"buffer: createPost returned no payload: {data!r}")

    typename = cp.get("__typename") or ""
    if typename == "PostActionSuccess":
        post_payload = cp.get("post") or {}
        if not isinstance(post_payload, dict):
            raise BufferError(f"buffer: PostActionSuccess missing post body: {cp!r}")
        post_id = post_payload.get("id")
        if not isinstance(post_id, str):
            raise BufferError(f"buffer: PostActionSuccess missing post.id: {post_payload!r}")
        debug(f"buffer: createPost ok id={post_id} status={post_payload.get('status')}")
        return CreatedPost(
            id=post_id,
            status=post_payload.get("status"),
            text=post_payload.get("text"),
            raw=post_payload,
        )

    # Any non-success variant carries a `message` (and sometimes a code/link).
    msg = cp.get("message") or "(no message)"
    code = cp.get("code")
    link = cp.get("link")
    raise BufferError(
        f"buffer: createPost {typename or 'unknown'}: {msg}"
        + (f" (code={code})" if code is not None else "")
        + (f" link={link}" if link else "")
    )


def is_configured() -> bool:
    """Return True iff the env vars needed for Buffer are present."""

    try:
        env_required("BUFFER_ACCESS_TOKEN")
        env_required("BUFFER_ORG_ID")
        env_required("BUFFER_X_CHANNEL_ID")
        return True
    except Exception:
        return False
