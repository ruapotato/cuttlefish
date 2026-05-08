"""Continue Watching section on the home page (/).

The section appears above the kind sections when the user has anything
in progress. For TV: one tile per show, showing the in-progress episode
or — if the latest watched one ended — the NEXT unwatched episode (so
finishing an episode auto-suggests the next one).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import db, scanner
from cuttlefish.server import create_app


def _setup_two_season_show(tmp_path: Path):
    """Library with one show, 2 seasons × 2 episodes each. Return
    (db_path, conn, show_id, eps_in_order)."""
    root = tmp_path / "tv"
    show = root / "My Show"
    s1 = show / "Season 01"
    s2 = show / "Season 02"
    s1.mkdir(parents=True)
    s2.mkdir(parents=True)
    (s1 / "My Show - S01E01 - Pilot.mp4").write_bytes(b"")
    (s1 / "My Show - S01E02 - Two.mp4").write_bytes(b"")
    (s2 / "My Show - S02E01 - Return.mp4").write_bytes(b"")
    (s2 / "My Show - S02E02 - Finale.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('tv', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    show_id = conn.execute(
        "SELECT id FROM media WHERE kind='tv_show'"
    ).fetchone()["id"]
    eps = conn.execute(
        "SELECT id, season, episode FROM tv_episodes ORDER BY season, episode"
    ).fetchall()
    return db_path, conn, show_id, eps


def _login(client, username="alice"):
    client.post("/api/auth/register",
                data={"username": username, "password": "secret123"})
    client.post("/api/auth/login",
                data={"username": username, "password": "secret123"})


def _user_id(client):
    return client.get("/api/me").json()["id"]


def test_no_continue_watching_section_when_no_progress(tmp_path):
    """Logged-in user with nothing in progress: section is absent (no
    point taking up the top of the page with empty state)."""
    db_path, _, _, _ = _setup_two_season_show(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    r = client.get("/")
    assert r.status_code == 200
    assert "Continue watching" not in r.text


def test_continue_watching_shows_in_progress_episode(tmp_path):
    """Episode at 30% should appear in continue-watching as itself."""
    db_path, conn, _, eps = _setup_two_season_show(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    user_id = _user_id(client)
    with conn:
        conn.execute(
            "INSERT INTO episode_progress "
            "(user_id, episode_id, position_seconds, duration_seconds) "
            "VALUES (?, ?, 180, 600)",
            (user_id, eps[0]["id"]),  # S01E01 at 30%
        )
    r = client.get("/")
    assert "Continue watching" in r.text
    # Tile for S01E01, with show poster URL and a watch link to that episode
    assert f"/watch/episode/{eps[0]['id']}" in r.text
    assert "My Show S01E01" in r.text
    # Not the "Up next" badge — it's the in-progress one
    assert "class='cw-badge cw-next'" not in r.text
    assert "Up next" not in r.text


def test_continue_watching_jumps_to_next_after_finishing(tmp_path):
    """User finished S01E01 (>= 95% of duration). The card should now
    be S01E02 with an 'Up next' badge — not S01E01 again."""
    db_path, conn, _, eps = _setup_two_season_show(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    user_id = _user_id(client)
    with conn:
        conn.execute(
            "INSERT INTO episode_progress "
            "(user_id, episode_id, position_seconds, duration_seconds) "
            "VALUES (?, ?, 600, 600)",  # finished
            (user_id, eps[0]["id"]),
        )
    r = client.get("/")
    assert "Continue watching" in r.text
    assert f"/watch/episode/{eps[1]['id']}" in r.text  # S01E02
    assert f"/watch/episode/{eps[0]['id']}" not in r.text  # not S01E01
    assert "My Show S01E02" in r.text
    assert "Up next" in r.text
    assert "class='cw-badge cw-next'" in r.text


def test_continue_watching_jumps_across_seasons(tmp_path):
    """Finishing the season finale (S01E02) should suggest S02E01."""
    db_path, conn, _, eps = _setup_two_season_show(tmp_path)
    s1e2 = eps[1]
    s2e1 = eps[2]
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    user_id = _user_id(client)
    with conn:
        conn.execute(
            "INSERT INTO episode_progress "
            "(user_id, episode_id, position_seconds, duration_seconds) "
            "VALUES (?, ?, 600, 600)",
            (user_id, s1e2["id"]),
        )
    r = client.get("/")
    assert f"/watch/episode/{s2e1['id']}" in r.text
    assert "My Show S02E01" in r.text


def test_continue_watching_drops_show_when_series_finished(tmp_path):
    """Finishing the series finale (S02E02) → show drops out of
    continue-watching entirely."""
    db_path, conn, _, eps = _setup_two_season_show(tmp_path)
    finale = eps[3]
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    user_id = _user_id(client)
    with conn:
        conn.execute(
            "INSERT INTO episode_progress "
            "(user_id, episode_id, position_seconds, duration_seconds) "
            "VALUES (?, ?, 600, 600)",
            (user_id, finale["id"]),
        )
    r = client.get("/")
    assert "Continue watching" not in r.text


def test_continue_watching_dedupes_per_show(tmp_path):
    """Multiple episodes with progress for one show → only ONE card
    (the most recent one's resume target)."""
    db_path, conn, _, eps = _setup_two_season_show(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    user_id = _user_id(client)
    # Older entry for S01E01, newer (and in-progress) for S01E02.
    with conn:
        conn.execute(
            "INSERT INTO episode_progress "
            "(user_id, episode_id, position_seconds, duration_seconds, updated_at) "
            "VALUES (?, ?, 600, 600, '2024-01-01 00:00:00')",
            (user_id, eps[0]["id"]),
        )
        conn.execute(
            "INSERT INTO episode_progress "
            "(user_id, episode_id, position_seconds, duration_seconds, updated_at) "
            "VALUES (?, ?, 200, 600, '2024-06-01 00:00:00')",
            (user_id, eps[1]["id"]),
        )
    r = client.get("/")
    # Should see exactly one episode link from this show in the section.
    # (The links also appear elsewhere on the show page, but this is /.)
    assert r.text.count(f"/watch/episode/{eps[1]['id']}") == 1
    # S01E01 shouldn't appear at all (it was finished, but S01E02 supersedes).
    assert f"/watch/episode/{eps[0]['id']}" not in r.text


def test_continue_watching_movie_in_progress(tmp_path):
    """In-progress movie should appear as a card too."""
    root = tmp_path / "movies"; root.mkdir()
    (root / "Big Movie.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root)
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    user_id = _user_id(client)
    with conn:
        conn.execute(
            "INSERT INTO media_progress "
            "(user_id, media_id, position_seconds, duration_seconds) "
            "VALUES (?, ?, 300, 1200)",
            (user_id, media_id),
        )
    r = client.get("/")
    assert "Continue watching" in r.text
    assert "Big Movie" in r.text
    assert f"/watch/{media_id}" in r.text


def test_continue_watching_drops_finished_movies(tmp_path):
    """Movie watched to >= 95% shouldn't keep nagging in continue-watching."""
    root = tmp_path / "movies"; root.mkdir()
    (root / "Done Movie.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root)
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    user_id = _user_id(client)
    with conn:
        conn.execute(
            "INSERT INTO media_progress "
            "(user_id, media_id, position_seconds, duration_seconds) "
            "VALUES (?, ?, 1200, 1200)",
            (user_id, media_id),
        )
    r = client.get("/")
    assert "Continue watching" not in r.text


def test_anonymous_user_sees_no_continue_watching(tmp_path):
    """Logged-out user shouldn't see a Continue Watching section even if
    other users have progress."""
    db_path, conn, _, eps = _setup_two_season_show(tmp_path)
    # Register an admin so their progress exists, but don't log in here.
    client_admin = TestClient(create_app(db_path=db_path))
    _login(client_admin, username="admin")
    user_id = _user_id(client_admin)
    with conn:
        conn.execute(
            "INSERT INTO episode_progress "
            "(user_id, episode_id, position_seconds, duration_seconds) "
            "VALUES (?, ?, 100, 600)",
            (user_id, eps[0]["id"]),
        )
    # Anonymous client.
    client = TestClient(create_app(db_path=db_path))
    r = client.get("/", follow_redirects=False)
    # /  is gated by login-required middleware, so anon redirects to login.
    # The point of this test: we don't crash, and we don't leak someone
    # else's progress on an anonymous landing.
    assert r.status_code in (200, 303, 401)
    if r.status_code == 200:
        assert "Continue watching" not in r.text
