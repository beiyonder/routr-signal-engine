"""SQLite connection + schema management for the local intel store.

This is the local-side mirror of what will eventually live in Cloudflare D1.
The schema here is intentionally a strict subset of the D1 schema so the
Phase-4 sync job can be a straight column-for-column INSERT OR REPLACE.

Tables:
    signals -- every fetched item, progressively enriched (cosine -> LLM ->
               action_label). Dedupe = id PRIMARY KEY.
    runs    -- one row per pipeline invocation; counts, costs, notes.
               `kind` distinguishes 'daily' from 'synthesis' runs.
               `discord_message_ids` lists the Discord message IDs the digest
               landed in, so the dispatch worker can poll them for reactions.
    posts   -- outgoing cross-channel publishes (x_thread -> X via Buffer,
               synthesis -> Beehiiv newsletter draft, etc). Pre-created as
               'pending' at publish time; promoted to 'posted'/'failed' by
               the dispatch worker after the user reacts in Discord.
    people  -- (Phase 2) authors aggregated across signals. Not yet built.
    drafts  -- (Phase 2) every drafted post hook. Not yet built.

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
    kind                TEXT NOT NULL DEFAULT 'daily',
    source_counts       TEXT NOT NULL DEFAULT '{}',
    cosine_kept         TEXT NOT NULL DEFAULT '{}',
    classifier_relevant TEXT NOT NULL DEFAULT '{}',
    notes               TEXT NOT NULL DEFAULT '[]',
    cost_estimate_usd   REAL,
    digest_md           TEXT,
    hooks_json          TEXT,
    discord_message_ids TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_kind ON runs(kind);

-- Outgoing posts: one row per cross-channel publish (x_thread -> X, synthesis -> beehiiv, etc).
-- 'pending' rows are created when a digest is published (one per auto-postable hook).
-- The dispatch worker promotes 'pending' -> 'posted'/'failed' once it sees an approval reaction.
CREATE TABLE IF NOT EXISTS posts (
    id                  TEXT PRIMARY KEY,         -- our internal uuid
    kind                TEXT NOT NULL,            -- hook | synthesis | newsletter
    signal_id           TEXT,                     -- FK to signals.id (nullable)
    run_id              TEXT,                     -- FK to runs.id (nullable)
    hook_format         TEXT,                     -- x_thread | linkedin | reddit | hn_comment | devto_title
    platform            TEXT NOT NULL,            -- x | linkedin | beehiiv | manual
    text                TEXT NOT NULL,
    discord_message_id  TEXT,                     -- which message held this draft
    discord_reaction    TEXT,                     -- which emoji approved it
    buffer_post_id      TEXT,                     -- Buffer's id after createPost
    beehiiv_post_id     TEXT,                     -- Beehiiv's id after create
    external_url        TEXT,                     -- final public URL when known
    status              TEXT NOT NULL,            -- pending | approved | posted | failed | skipped
    error               TEXT,
    created_at          TEXT NOT NULL,
    approved_at         TEXT,
    posted_at           TEXT,
    metadata            TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_posts_status   ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_signal   ON posts(signal_id);
CREATE INDEX IF NOT EXISTS idx_posts_run      ON posts(run_id);
CREATE INDEX IF NOT EXISTS idx_posts_platform ON posts(platform);
CREATE INDEX IF NOT EXISTS idx_posts_created  ON posts(created_at);
"""


def _ensure_runs_columns(conn: sqlite3.Connection) -> None:
    """Migration: add columns that may be missing from an older runs table.

    SQLite's CREATE TABLE IF NOT EXISTS does NOT add columns to an existing
    table, so when this code runs against an intel.db created before the
    `kind` or `discord_message_ids` columns existed, we need explicit
    ALTER TABLEs.

    On a fresh database (no `runs` table yet), this is a no-op -- the
    subsequent SCHEMA_SQL executescript will create the table with the
    new columns from the start.
    """

    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
    ).fetchone() is not None
    if not table_exists:
        return

    cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "kind" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'daily'")
    if "discord_message_ids" not in cols:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN discord_message_ids TEXT NOT NULL DEFAULT '[]'"
        )


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
        # IMPORTANT ORDER:
        # Run column-level migrations on the EXISTING `runs` table first so any
        # CREATE INDEX statements in SCHEMA_SQL that reference the new columns
        # (e.g., idx_runs_kind) succeed on first run against an old database.
        # PRAGMA table_info() returns [] for a non-existent table, so this is
        # a no-op for fresh databases (the table is created by executescript
        # right after, already including the new columns).
        _ensure_runs_columns(conn)
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
