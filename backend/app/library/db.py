"""SQLite connection + schema for the content library.

A thin wrapper around stdlib ``sqlite3``: one connection per operation (cheap, avoids
cross-thread sharing under the threaded server), WAL for concurrent reads, foreign
keys ON so a series delete cascades to its episodes/sources/exports. The repository
methods live in ``store.py``; this module owns only connection + schema.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

_SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    cover_url   TEXT,
    tags        TEXT NOT NULL DEFAULT '[]',   -- json array
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series_source (
    id              TEXT PRIMARY KEY,
    series_id       TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    platform        TEXT NOT NULL,
    url             TEXT NOT NULL,
    label           TEXT,
    last_scanned_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_series ON series_source(series_id);

CREATE TABLE IF NOT EXISTS episode (
    id                TEXT PRIMARY KEY,
    series_id         TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    platform          TEXT NOT NULL,
    platform_video_id TEXT NOT NULL,
    source_url        TEXT NOT NULL,
    title             TEXT NOT NULL,
    thumbnail_url     TEXT,
    duration          REAL,
    uploader          TEXT,
    discovered_at     TEXT NOT NULL,
    dub_job_id        TEXT,
    dub_state         TEXT NOT NULL DEFAULT 'none',
    UNIQUE(series_id, platform, platform_video_id)   -- dedup: 1 video -> 1 series
);
CREATE INDEX IF NOT EXISTS idx_episode_series ON episode(series_id);
CREATE INDEX IF NOT EXISTS idx_episode_job ON episode(dub_job_id);

CREATE TABLE IF NOT EXISTS export (
    id              TEXT PRIMARY KEY,
    series_id       TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    hashtags        TEXT NOT NULL DEFAULT '[]',   -- json array
    episode_job_ids TEXT NOT NULL DEFAULT '[]',   -- json array, ordered
    output_path     TEXT,
    duration        REAL,
    state           TEXT NOT NULL DEFAULT 'draft',
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_export_series ON export(series_id);
"""


# Additive column migrations applied on top of the base schema (SQLite can't do
# "ADD COLUMN IF NOT EXISTS", so we check first). Append-only, never rewrite.
_MIGRATIONS = [
    ("title_vi", "ALTER TABLE episode ADD COLUMN title_vi TEXT"),  # Vietnamese title (bilingual UI)
]


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


class LibraryDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._init_lock = Lock()
        self._ready = False

    def _ensure_schema(self) -> None:
        if self._ready:
            return
        with self._init_lock:
            if self._ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path)
            try:
                conn.executescript(_SCHEMA)
                for column, ddl in _MIGRATIONS:
                    if not _has_column(conn, "episode", column):
                        conn.execute(ddl)
                conn.commit()
            finally:
                conn.close()
            self._ready = True

    @contextmanager
    def connect(self):
        """A short-lived connection with row access, FK cascade, and WAL."""
        self._ensure_schema()
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
