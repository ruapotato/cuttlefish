"""SQLite layer: schema, connection, default DB path."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 7

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
    """
    CREATE TABLE IF NOT EXISTS audiobook_progress (
        user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        book_id          INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
        current_track_id INTEGER REFERENCES audiobook_tracks(id) ON DELETE SET NULL,
        position_seconds REAL    NOT NULL DEFAULT 0,
        updated_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, book_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS encoded_episodes (
        episode_id    INTEGER PRIMARY KEY REFERENCES tv_episodes(id) ON DELETE CASCADE,
        clean_dir     TEXT    NOT NULL,
        video_path    TEXT    NOT NULL,
        subtitle_path TEXT,
        poster_path   TEXT,
        size_bytes    INTEGER,
        encoded_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
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


# Idempotent additive ALTER TABLE migrations for DBs created on an older
# SCHEMA_VERSION. SQLite's ALTER TABLE ADD COLUMN raises OperationalError
# when the column already exists, which we swallow.
ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("media", "duration_seconds REAL"),
    ("media", "poster_path TEXT"),
    ("tv_episodes", "duration_seconds REAL"),
    ("tv_episodes", "poster_path TEXT"),
    ("audiobook_tracks", "duration_seconds REAL"),
)


def _migrate_libraries_drop_kind(conn: sqlite3.Connection) -> None:
    """v6→v7: drop the `kind` column from `libraries`. Cuttlefish now decides
    what each subfolder is by looking at it, so the library no longer has a
    type. SQLite ALTER TABLE DROP COLUMN exists in 3.35+, but using the
    recreate pattern here works on every SQLite version we support."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'libraries' AND type = 'table'"
    ).fetchone()
    if row is None or " kind " not in (row["sql"] or ""):
        return  # column already absent
    conn.execute("""
        CREATE TABLE libraries_new (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL UNIQUE,
            root_path  TEXT    NOT NULL UNIQUE,
            created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "INSERT INTO libraries_new (id, name, root_path, created_at) "
        "SELECT id, name, root_path, created_at FROM libraries"
    )
    conn.execute("DROP TABLE libraries")
    conn.execute("ALTER TABLE libraries_new RENAME TO libraries")


def init_schema(conn: sqlite3.Connection) -> None:
    with conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
        for table, col_decl in ADDITIVE_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_decl}")
            except sqlite3.OperationalError:
                pass  # column already exists
        _migrate_libraries_drop_kind(conn)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
