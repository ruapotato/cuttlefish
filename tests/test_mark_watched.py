"""Tests for delete-progress, mark-watched, and the form variants used on
the /continue-watching page."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cuttlefish import db, scanner
from cuttlefish.server import create_app


def _setup(tmp_path: Path):
    root = tmp_path / "movies"
    root.mkdir()
    (root / "Movie.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    # Manually set duration since the empty file wasn't probed
    with conn:
        conn.execute("UPDATE media SET duration_seconds = 600.0 WHERE id = ?", (media_id,))
    return db_path, media_id


def _logged_in(db_path):
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    return client


def test_delete_progress(tmp_path):
    db_path, media_id = _setup(tmp_path)
    client = _logged_in(db_path)
    client.put(f"/api/progress/{media_id}", json={"position_seconds": 90.0})
    assert client.get(f"/api/progress/{media_id}").json()["position_seconds"] == 90.0
    assert client.delete(f"/api/progress/{media_id}").status_code == 200
    assert client.get(f"/api/progress/{media_id}").json()["position_seconds"] == 0.0


def test_mark_watched_sets_to_duration(tmp_path):
    db_path, media_id = _setup(tmp_path)
    client = _logged_in(db_path)
    r = client.post(f"/api/progress/{media_id}/watched")
    assert r.status_code == 200
    assert r.json()["position_seconds"] == 600.0


def test_mark_watched_unknown_media_404(tmp_path):
    db_path, _ = _setup(tmp_path)
    client = _logged_in(db_path)
    assert client.post("/api/progress/9999/watched").status_code == 404


def test_delete_progress_requires_login(tmp_path):
    db_path, media_id = _setup(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    assert client.delete(f"/api/progress/{media_id}").status_code == 401
    assert client.post(f"/api/progress/{media_id}/watched").status_code == 401


def test_delete_episode_progress(tmp_path):
    root = tmp_path / "tv"
    s1 = root / "Show" / "Season 01"
    s1.mkdir(parents=True)
    (s1 / "Show - S01E01.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('tv', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    ep_id = conn.execute("SELECT id FROM tv_episodes").fetchone()["id"]
    client = _logged_in(db_path)
    client.put(f"/api/progress/episode/{ep_id}", json={"position_seconds": 30.0})
    assert client.delete(f"/api/progress/episode/{ep_id}").status_code == 200
    assert client.get(f"/api/progress/episode/{ep_id}").json()["position_seconds"] == 0.0


def test_form_mark_watched_redirects(tmp_path):
    db_path, media_id = _setup(tmp_path)
    client = _logged_in(db_path)
    r = client.post(f"/progress/{media_id}/watched", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/continue-watching"


def test_form_reset_redirects(tmp_path):
    db_path, media_id = _setup(tmp_path)
    client = _logged_in(db_path)
    client.put(f"/api/progress/{media_id}", json={"position_seconds": 90.0})
    r = client.post(f"/progress/{media_id}/reset", follow_redirects=False)
    assert r.status_code == 303
    assert client.get(f"/api/progress/{media_id}").json()["position_seconds"] == 0.0


def test_continue_watching_page_shows_action_buttons(tmp_path):
    db_path, media_id = _setup(tmp_path)
    client = _logged_in(db_path)
    client.put(f"/api/progress/{media_id}", json={"position_seconds": 90.0, "duration_seconds": 600.0})
    r = client.get("/continue-watching")
    assert r.status_code == 200
    assert "Mark watched" in r.text
    assert "Reset" in r.text
