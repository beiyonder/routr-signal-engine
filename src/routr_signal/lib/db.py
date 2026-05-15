"""SQLite connection + schema management for the local intel store.

This is the local-side mirror of what will eventually live in Cloudflare D1.
The schema here is intentionally a strict subset of the D1 schema so the
Phase-4 sync job can be a straight column-for-column INSERT OR REPLACE.

Tables:
    signals -- every fetched item, progressively enriched (cosine -> LLM ->
               action_label). Dedupe = id PRIMARY KEY.
    runs    -- one row per pipeline invocation; counts, costs, notes.
    people  -- (Phase 2) authors aggregated across signals.
    drafts  -- (Phase 2) every drafted post hook.

Connection management:
    A single SQLite file at data/intel.db (gitignored). One connection per
    process; WAL mode for concurrent reads if a future dashboard reads
    while CI writes. foreign_keys ON.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .paths import data_dir


_CONN: sqlite3.Connection | None = None
_LOCK = threading.Lock()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signals (
    id                      TEXT PRIMARY KEY,
    source                  TEXT NOT NULL,
    author_handle           TEXT,
    title                   TEXT NOT NULL,
    body                    TEXT NOT NULL DEFAULT '',
    url                     TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    fetched_at              TEXT NOT NULL,
    raw_extra               TEXT NOT NULL DEFAULT '{}',
    cosine_score            REAL,
    cosine_top_topic        TEXT,
    cosine_at               TEXT,
    llm_score               REAL,
    llm_relevant            INTEGER,
    llm_topics              TEXT,
    llm_pain_summary        TEXT,
    llm_engagement_angle    TEXT,
    llm_do_not_engage       TEXT,
    llm_lead_handle         TEXT,
    llm_lead_platform       TEXT,
    classified_at           TEXT,
    combined_score          REAL,
    rank_in_run             INTEGER,
    run_id                  TEXT,
    action_label            TEXT NOT NULL DEFAULT 'untriaged',
    action_notes            TEXT,
    engaged_at              TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_source        ON signals(source);
CREATE INDEX IF NOT EXISTS idx_signals_author        ON signals(author_handle);
CREATE INDEX IF NOT EXISTS idx_signals_run           ON signals(run_id);
CREATE INDEX IF NOT EXISTS idx_signals_combined     ON signals(combined_score);
CREATE INDEX IF NOT EXISTS idx_signals_action        ON signals(action_label);
CREATE INDEX IF NOT EXISTS idx_signals_created_at    ON signals(created_at);

CREATE TABLE IF NOT EXISTS runs (
    id                  TEXT PRIMARY KEY,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT,
    source_counts       TEXT NOT NULL DEFAULT '{}',
    cosine_kept         TEXT NOT NULL DEFAULT '{}',
    classifier_relevant TEXT NOT NULL DEFAULT '{}',
    notes               TEXT NOT NULL DEFAULT '[]',
    cost_estimate_usd   REAL,
    digest_md           TEXT,
    hooks_json          TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
"""


def db_path() -> Path:
    return data_dir() / "intel.db"


def get_db() -> sqlite3.Connection:
    """Return the process-wide connection, initializing on first use."""

    global _CONN
    with _LOCK:
        if _CONN is not None:
            return _CONN
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        _CONN = conn
        return conn


def close_db() -> None:
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None
