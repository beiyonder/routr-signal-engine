"""Cosine relevance scoring against curated topic anchors.

Workflow:
    1. Load `config/topic_anchors.yaml` (cached).
    2. Embed each anchor once per process (cached on disk in `data/cache/anchors-<hash>.json`).
    3. For each RawItem, embed `title + body`, compute max weighted cosine against
       any anchor. Return the score AND the topic of the best-matching anchor.
    4. Apply `min_cosine_threshold` to drop items below the threshold *before* the
       LLM call. This is the cost / noise cut.

We persist anchor embeddings on disk keyed by (model, dim, anchor-list hash) so
re-runs don't re-embed anchors. Item embeddings are not cached — they are one-off.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import embeddings
from .logging import debug, info, warn
from .paths import config_dir, data_dir
from .types import RawItem


# Threshold against *raw* cosine (unweighted). Gemini's embedding space has a high
# baseline for any English text (~0.55-0.65 between unrelated topics), so the threshold
# has to sit above that floor. 0.68 was tuned against the smoke test fixture:
#   on-topic items score 0.80+, off-topic items score 0.60-0.66.
DEFAULT_MIN_COSINE = 0.68

# Truncate the text we embed (Gemini embedding model has 2048-token limit anyway).
MAX_EMBED_CHARS = 6000


@dataclass(slots=True)
class CosineScore:
    """Two-number score per item.

    `score` is the *raw* max cosine similarity against any anchor in [0, 1].
        Used for thresholding (filter off-topic).
    `weighted_score` is `score * anchor_weight` of the top anchor; used as a
        ranking signal but clamped to [0, 1] for storage.
    `top_topic` is the topic of the best-matching anchor by weighted score
        (so weights bias topic assignment but not pass/fail).
    """

    score: float
    weighted_score: float
    top_topic: str
    top_anchor_idx: int


def _anchor_config_path() -> Path:
    return config_dir() / "topic_anchors.yaml"


def _anchor_cache_dir() -> Path:
    d = data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_anchors() -> list[dict[str, Any]]:
    path = _anchor_config_path()
    if not path.exists():
        warn(f"cosine: anchor config missing at {path}; cosine scoring disabled.")
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    anchors = raw.get("anchors") or []
    if not isinstance(anchors, list):
        warn("cosine: anchors not a list; cosine scoring disabled.")
        return []
    cleaned: list[dict[str, Any]] = []
    for i, a in enumerate(anchors):
        if not isinstance(a, dict):
            continue
        text = (a.get("text") or "").strip()
        if not text:
            continue
        cleaned.append(
            {
                "idx": i,
                "topic": a.get("topic") or "other",
                "text": text,
                "weight": float(a.get("weight", 1.0)),
            }
        )
    return cleaned


def _anchors_signature(anchors: list[dict[str, Any]], model: str, dim: int) -> str:
    payload = json.dumps(
        {"m": model, "d": dim, "a": [(a["topic"], a["text"], a["weight"]) for a in anchors]},
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _load_or_compute_anchor_embeddings(
    anchors: list[dict[str, Any]],
    *,
    model: str = embeddings.DEFAULT_MODEL,
    dim: int = embeddings.DEFAULT_DIM,
) -> list[list[float] | None]:
    sig = _anchors_signature(anchors, model, dim)
    cache_path = _anchor_cache_dir() / f"anchors-{sig}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, list) and len(cached) == len(anchors):
                debug(f"cosine: loaded anchor embeddings from {cache_path}")
                return cached
        except (json.JSONDecodeError, OSError) as e:
            warn(f"cosine: anchor cache unreadable, recomputing: {e}")

    info(f"cosine: embedding {len(anchors)} anchors (model={model}, dim={dim})")
    vecs = embeddings.embed_many(
        [a["text"] for a in anchors],
        task_type="RETRIEVAL_DOCUMENT",
        model=model,
        dim=dim,
    )
    try:
        cache_path.write_text(json.dumps(vecs), encoding="utf-8")
    except OSError as e:
        warn(f"cosine: could not persist anchor cache: {e}")
    return vecs


def score_items(
    items: list[RawItem],
    *,
    min_cosine: float | None = None,
    model: str = embeddings.DEFAULT_MODEL,
    dim: int = embeddings.DEFAULT_DIM,
) -> tuple[list[tuple[RawItem, CosineScore]], list[RawItem]]:
    """Score every item against the anchor set. Return (kept, dropped).

    Each kept item is a (RawItem, CosineScore) pair. `cosine_score` and `cosine_top_topic`
    are also written into `RawItem.extra` for downstream consumers.

    `min_cosine` defaults to DEFAULT_MIN_COSINE. Items strictly below the threshold land in
    the `dropped` list; everything at or above is in `kept`.
    """

    if min_cosine is None:
        min_cosine = DEFAULT_MIN_COSINE

    anchors = load_anchors()
    if not anchors:
        # No anchors configured: pass everything through with score 0, no filtering.
        out: list[tuple[RawItem, CosineScore]] = []
        for it in items:
            cs = CosineScore(score=0.0, weighted_score=0.0, top_topic="other", top_anchor_idx=-1)
            it.extra["cosine_score"] = 0.0
            it.extra["cosine_top_topic"] = "other"
            out.append((it, cs))
        return out, []

    anchor_vecs = _load_or_compute_anchor_embeddings(anchors, model=model, dim=dim)

    kept: list[tuple[RawItem, CosineScore]] = []
    dropped: list[RawItem] = []
    for it in items:
        text = (it.title + "\n" + it.body).strip()[:MAX_EMBED_CHARS]
        if not text:
            dropped.append(it)
            continue
        item_vec = embeddings.embed(text, task_type="RETRIEVAL_QUERY", model=model, dim=dim)
        if item_vec is None:
            # Embedding failed; do not drop — let the LLM see it. We give a neutral score.
            cs = CosineScore(score=0.0, weighted_score=0.0, top_topic="other", top_anchor_idx=-1)
            it.extra["cosine_score"] = 0.0
            it.extra["cosine_top_topic"] = "other"
            kept.append((it, cs))
            continue

        # Track best by RAW cosine (for thresholding) and best by WEIGHTED (for topic).
        best_raw_idx = -1
        best_raw = 0.0
        best_weighted_idx = -1
        best_weighted = 0.0

        for i, av in enumerate(anchor_vecs):
            sim = embeddings.cosine(item_vec, av)
            if sim > best_raw:
                best_raw = sim
                best_raw_idx = i
            weighted = sim * anchors[i]["weight"]
            if weighted > best_weighted:
                best_weighted = weighted
                best_weighted_idx = i

        top_topic = (
            anchors[best_weighted_idx]["topic"] if best_weighted_idx >= 0 else "other"
        )
        weighted_clamped = min(1.0, max(0.0, best_weighted))

        cs = CosineScore(
            score=round(best_raw, 4),
            weighted_score=round(weighted_clamped, 4),
            top_topic=top_topic,
            top_anchor_idx=best_raw_idx,
        )
        # Store the raw score for downstream (combined_score uses this).
        it.extra["cosine_score"] = cs.score
        it.extra["cosine_weighted_score"] = cs.weighted_score
        it.extra["cosine_top_topic"] = top_topic

        if best_raw >= min_cosine:
            kept.append((it, cs))
        else:
            dropped.append(it)

    info(
        f"cosine: scored {len(items)} items; "
        f"kept {len(kept)} (>= {min_cosine}), dropped {len(dropped)}"
    )
    return kept, dropped
