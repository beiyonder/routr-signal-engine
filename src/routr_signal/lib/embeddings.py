"""Embedding client using Gemini's gemini-embedding-001 model.

Why Gemini for embeddings even when the classifier provider is Anthropic:
    Anthropic does not ship an embeddings API. Gemini's gemini-embedding-001 is
    the current state of the art for English + multilingual retrieval, is on
    Google's generous free tier, and our cosine layer wants a single embedding
    space for both anchors and incoming items.

We default to 768-dim output (truncated from the model's native 3072) because:
    - it's plenty for cosine ranking over ~15 anchors;
    - 4x smaller storage means we can cache anchor embeddings inline;
    - the model loses very little quality when truncated to 768.

Embedding cost on AI Studio's free tier is effectively zero at our daily volume.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

from .env import env_required
from .logging import debug, warn


DEFAULT_MODEL = "gemini-embedding-001"
DEFAULT_DIM = 768


_client: Any = None


def _get_client() -> Any:
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=env_required("GEMINI_API_KEY"))
    return _client


def embed(
    text: str,
    *,
    task_type: str = "RETRIEVAL_DOCUMENT",
    model: str = DEFAULT_MODEL,
    dim: int = DEFAULT_DIM,
) -> list[float] | None:
    """Return a single embedding vector for `text`, or None on failure.

    `task_type` of `RETRIEVAL_DOCUMENT` for anchor / corpus texts,
    `RETRIEVAL_QUERY` for incoming items being scored against the corpus.
    """

    if not text or not text.strip():
        return None

    client = _get_client()
    from google.genai import errors as genai_errors
    from google.genai import types

    try:
        resp = client.models.embed_content(
            model=model,
            contents=text,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=dim,
            ),
        )
    except genai_errors.APIError as e:
        warn(f"embeddings: API error: {e}")
        return None
    except Exception as e:  # noqa: BLE001
        warn(f"embeddings: unexpected error: {e}")
        return None

    # google-genai returns either `embedding` (single) or `embeddings[0]`.
    # Handle both shapes.
    if hasattr(resp, "embedding") and resp.embedding is not None:
        vec = resp.embedding.values
    elif hasattr(resp, "embeddings") and resp.embeddings:
        vec = resp.embeddings[0].values
    else:
        warn("embeddings: no embedding in response")
        return None

    if not vec:
        return None

    # Normalize to unit length so dot product == cosine similarity.
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return None
    return [x / norm for x in vec]


def embed_many(
    texts: list[str],
    *,
    task_type: str = "RETRIEVAL_DOCUMENT",
    model: str = DEFAULT_MODEL,
    dim: int = DEFAULT_DIM,
) -> list[list[float] | None]:
    """Embed a list of texts one at a time (Gemini embeds one input per request)."""

    out: list[list[float] | None] = []
    for t in texts:
        vec = embed(t, task_type=task_type, model=model, dim=dim)
        out.append(vec)
    return out


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity between two unit-normalized vectors. Returns 0.0 if either is None."""

    if a is None or b is None:
        return 0.0
    if len(a) != len(b):
        return 0.0
    # Both are unit-normalized in embed(), so dot product is the cosine.
    return sum(x * y for x, y in zip(a, b, strict=False))
