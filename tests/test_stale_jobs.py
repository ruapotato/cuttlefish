"""Test that stale 'running' jobs get reset to 'queued' on server start.

Mirrors what cmd_serve does so the queued state is recoverable when a
worker dies mid-job (server crash / Ctrl+C / OOM kill / etc.).
"""
from __future__ import annotations

from pathlib import Path

from cuttlefish import db


def _reset_stale(conn):
    """The exact statement cmd_serve runs at startup. Kept inline here so
    the test exercises the SQL behavior, not the CLI plumbing."""
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'queued', started_at = NULL "
            "WHERE status = 'running'"
        )
    return cur.rowcount or 0


def test_running_job_gets_reset_to_queued(tmp_path: Path):
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    with conn:
        # Need a media row referenced by the job
        conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(tmp_path),),
        )
        conn.execute(
            "INSERT INTO media (library_id, kind, source_path, title_guess) "
            "VALUES (1, 'movie', ?, 'Movie')",
            (str(tmp_path / "m.mp4"),),
        )
        conn.execute(
            "INSERT INTO jobs (kind, media_id, status, started_at) "
            "VALUES ('asr', 1, 'running', '2025-01-01 00:00:00')"
        )
    n = _reset_stale(conn)
    assert n == 1
    row = conn.execute("SELECT status, started_at FROM jobs WHERE id = 1").fetchone()
    assert row["status"] == "queued"
    assert row["started_at"] is None


def test_done_jobs_are_left_alone(tmp_path: Path):
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    with conn:
        conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(tmp_path),),
        )
        conn.execute(
            "INSERT INTO media (library_id, kind, source_path, title_guess) "
            "VALUES (1, 'movie', ?, 'Movie')",
            (str(tmp_path / "m.mp4"),),
        )
        conn.execute(
            "INSERT INTO jobs (kind, media_id, status, started_at, finished_at) "
            "VALUES ('asr', 1, 'done', '2025-01-01 00:00:00', '2025-01-01 00:01:00')"
        )
        conn.execute(
            "INSERT INTO jobs (kind, media_id, status, error) "
            "VALUES ('asr', 1, 'failed', 'something went wrong')"
        )
        conn.execute(
            "INSERT INTO jobs (kind, media_id, status) VALUES ('asr', 1, 'queued')"
        )
    n = _reset_stale(conn)
    assert n == 0  # Nothing was 'running'
    statuses = sorted(r["status"] for r in conn.execute("SELECT status FROM jobs").fetchall())
    assert statuses == ["done", "failed", "queued"]


def test_multiple_stale_jobs_all_reset(tmp_path: Path):
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    with conn:
        conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(tmp_path),),
        )
        conn.execute(
            "INSERT INTO media (library_id, kind, source_path, title_guess) "
            "VALUES (1, 'movie', ?, 'Movie')",
            (str(tmp_path / "m.mp4"),),
        )
        for _ in range(3):
            conn.execute(
                "INSERT INTO jobs (kind, media_id, status, started_at) "
                "VALUES ('asr', 1, 'running', '2025-01-01 00:00:00')"
            )
    assert _reset_stale(conn) == 3
    rows = conn.execute("SELECT status, started_at FROM jobs").fetchall()
    assert all(r["status"] == "queued" for r in rows)
    assert all(r["started_at"] is None for r in rows)
