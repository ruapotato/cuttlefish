"""SQLite layer: schema, connection, default DB path."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS libraries (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT    NOT NULL UNIQUE,
        kind       TEXT    NOT NULL CHECK(kind IN ('movies','tv','audiobooks')),
        root_path  TEXT    NOT NULL UNIQUE,
        created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS media (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        library_id    INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
        kind          TEXT    NOT NULL CHECK(kind IN ('movie','tv_show','audiobook')),
        source_path   TEXT    NOT NULL,
        title_guess   TEXT    NOT NULL,
        first_seen_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_seen_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(library_id, source_path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tv_episodes (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        show_id       INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
        season        INTEGER NOT NULL,
        episode       INTEGER NOT NULL,
        source_path   TEXT    NOT NULL,
        title_guess   TEXT,
        first_seen_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_seen_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(show_id, source_path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audiobook_tracks (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id       INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
        order_index   INTEGER NOT NULL,
        source_path   TEXT    NOT NULL,
        first_seen_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_seen_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(book_id, source_path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT    NOT NULL UNIQUE,
        password_hash TEXT    NOT NULL,
        is_admin      INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token       TEXT    PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at  TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS media_progress (
        user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        media_id         INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
        position_seconds REAL    NOT NULL,
        duration_seconds REAL,
        updated_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, media_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS episode_progress (
        user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        episode_id       INTEGER NOT NULL REFERENCES tv_episodes(id) ON DELETE CASCADE,
        position_seconds REAL    NOT NULL,
        duration_seconds REAL,
        updated_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, episode_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS encoded_files (
        media_id      INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
        clean_dir     TEXT    NOT NULL,
        video_path    TEXT    NOT NULL,
        subtitle_path TEXT,
        poster_path   TEXT,
        size_bytes    INTEGER,
        encoded_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind        TEXT    NOT NULL CHECK(kind IN ('encode','subtitle','asr','metadata')),
        media_id    INTEGER REFERENCES media(id) ON DELETE CASCADE,
        episode_id  INTEGER REFERENCES tv_episodes(id) ON DELETE CASCADE,
        status      TEXT    NOT NULL CHECK(status IN ('queued','running','done','failed')) DEFAULT 'queued',
        payload     TEXT,
        result      TEXT,
        error       TEXT,
        created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        started_at  TEXT,
        finished_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_pending ON jobs(kind, status, id)",
)


def default_db_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "cuttlefish" / "cuttlefish.db"


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    with conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
