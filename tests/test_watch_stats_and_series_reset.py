"""Per-user watch-time tally + reset-progress for an entire series.

The tally is computed by capturing the delta between successive
position_seconds values during PUT /api/progress[/episode]. Big jumps
(seeks, scrubs) are clamped at 60s so a midnight scrub through a movie
doesn't claim 2 hours of watch time.

The series-reset deletes every episode_progress row for the
(current_user, episode_in_show). Other users' progress untouched.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import db, scanner
from cuttlefish.server import create_app


def _setup_show(tmp_path: Path):
    root = tmp_path / "tv"
    show = root / "Show"
    s1 = show / "Season 01"
    s1.mkdir(parents=True)
    (s1 / "Show - S01E01.mp4").write_bytes(b"")
    (s1 / "Show - S01E02.mp4").write_bytes(b"")
    s2 = show / "Season 02"
    s2.mkdir(parents=True)
    (s2 / "Show - S02E01.mp4").write_bytes(b"")
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
        "SELECT id FROM tv_episodes ORDER BY season, episode"
    ).fetchall()
    return db_path, conn, show_id, [e["id"] for e in eps]


def _login(client, username="alice"):
    client.post("/api/auth/register",
                data={"username": username, "password": "secret123"})
    client.post("/api/auth/login",
                data={"username": username, "password": "secret123"})


def _login_second_user(admin_client, db_path, username):
    """Register a second non-admin user via the admin's session (post-
    first-user registration is admin-only) and return a fresh logged-in
    client for that user."""
    admin_client.post("/api/auth/register",
                      data={"username": username, "password": "secret123"})
    new_client = TestClient(create_app(db_path=db_path))
    new_client.post("/api/auth/login",
                    data={"username": username, "password": "secret123"})
    return new_client


# --- watch-stats tally ---------------------------------------------------


def test_watch_stats_records_play_delta(tmp_path):
    db_path, _, _, eps = _setup_show(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    # 0 -> 30s: tally += 30
    client.put(f"/api/progress/episode/{eps[0]}",
               json={"position_seconds": 30.0, "duration_seconds": 600.0})
    # 30 -> 50s: tally += 20
    client.put(f"/api/progress/episode/{eps[0]}",
               json={"position_seconds": 50.0, "duration_seconds": 600.0})
    r = client.get("/api/me/watch-stats?days=1")
    assert r.status_code == 200
    body = r.json()
    assert body["today_seconds"] == 50
    assert body["days"][-1]["seconds"] == 50
    assert body["total_seconds"] == 50


def test_watch_stats_clamps_large_jumps(tmp_path):
    """Seeking from 0 to 1800s shouldn't claim 30 minutes of watch time."""
    db_path, _, _, eps = _setup_show(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    client.put(f"/api/progress/episode/{eps[0]}",
               json={"position_seconds": 1800.0, "duration_seconds": 3600.0})
    r = client.get("/api/me/watch-stats?days=1")
    body = r.json()
    # Capped at 60s per delta.
    assert body["today_seconds"] == 60


def test_watch_stats_ignores_scrub_back(tmp_path):
    """Scrubbing back (smaller new position) shouldn't decrement the tally."""
    db_path, _, _, eps = _setup_show(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    client.put(f"/api/progress/episode/{eps[0]}",
               json={"position_seconds": 40.0, "duration_seconds": 600.0})
    client.put(f"/api/progress/episode/{eps[0]}",
               json={"position_seconds": 10.0, "duration_seconds": 600.0})
    r = client.get("/api/me/watch-stats?days=1")
    assert r.json()["today_seconds"] == 40


def test_watch_stats_per_user(tmp_path):
    """Alice's tally and Bob's tally are independent."""
    db_path, _, _, eps = _setup_show(tmp_path)
    alice = TestClient(create_app(db_path=db_path))
    _login(alice, "alice")
    bob = _login_second_user(alice, db_path, "bob")
    alice.put(f"/api/progress/episode/{eps[0]}",
              json={"position_seconds": 30.0, "duration_seconds": 600.0})
    bob.put(f"/api/progress/episode/{eps[0]}",
            json={"position_seconds": 15.0, "duration_seconds": 600.0})
    assert alice.get("/api/me/watch-stats?days=1").json()["today_seconds"] == 30
    assert bob.get("/api/me/watch-stats?days=1").json()["today_seconds"] == 15


def test_watch_stats_returns_contiguous_days(tmp_path):
    """7-day range returns 7 entries even when most have zero playback."""
    db_path, _, _, eps = _setup_show(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    client.put(f"/api/progress/episode/{eps[0]}",
               json={"position_seconds": 25.0, "duration_seconds": 600.0})
    body = client.get("/api/me/watch-stats?days=7").json()
    assert len(body["days"]) == 7
    # Today (last entry) holds the tally.
    assert body["days"][-1]["seconds"] == 25
    # Earlier days are zero.
    assert all(d["seconds"] == 0 for d in body["days"][:-1])


def test_watch_stats_works_for_movies_too(tmp_path):
    """Movie playback (PUT /api/progress/{id}) tallies same as episodes."""
    root = tmp_path / "movies"; root.mkdir()
    (root / "Big.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root)
    mid = conn.execute("SELECT id FROM media").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    client.put(f"/api/progress/{mid}",
               json={"position_seconds": 45.0, "duration_seconds": 1800.0})
    assert client.get("/api/me/watch-stats?days=1").json()["today_seconds"] == 45


def test_watch_stats_requires_login(tmp_path):
    db_path, *_ = _setup_show(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    assert client.get("/api/me/watch-stats?days=7").status_code == 401


def test_account_page_renders_watch_chart(tmp_path):
    """The /account page shows today's tally + 7-day and 30-day bar charts."""
    db_path, _, _, eps = _setup_show(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    client.put(f"/api/progress/episode/{eps[0]}",
               json={"position_seconds": 60.0, "duration_seconds": 600.0})
    r = client.get("/account")
    assert r.status_code == 200
    assert "Watch time" in r.text
    assert "watched today" in r.text
    assert "Last 7 days" in r.text
    assert "Last 30 days" in r.text
    # The single 60s of playback shows as "1 min" today.
    assert "1 min" in r.text


# --- series reset --------------------------------------------------------


def test_series_reset_clears_only_this_show_for_this_user(tmp_path):
    db_path, conn, show_id, eps = _setup_show(tmp_path)
    # Add a second show so we can verify it's untouched.
    other = tmp_path / "tv2"
    other.mkdir()
    other_show = other / "Other"
    other_s1 = other_show / "Season 01"
    other_s1.mkdir(parents=True)
    (other_s1 / "Other - S01E01.mp4").write_bytes(b"")
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('tv2', ?)",
            (str(other),),
        )
    scanner.scan_library(conn, cur.lastrowid, other)
    other_ep = conn.execute(
        "SELECT e.id FROM tv_episodes e "
        "JOIN media m ON m.id = e.show_id WHERE m.title_guess = 'Other'"
    ).fetchone()["id"]

    alice = TestClient(create_app(db_path=db_path))
    _login(alice, "alice")
    bob = _login_second_user(alice, db_path, "bob")
    # Both users watch some of show #1 episode 1.
    for c in (alice, bob):
        c.put(f"/api/progress/episode/{eps[0]}",
              json={"position_seconds": 60.0, "duration_seconds": 600.0})
    # Alice also watches show #1 episode 2 and the OTHER show.
    alice.put(f"/api/progress/episode/{eps[1]}",
              json={"position_seconds": 30.0, "duration_seconds": 600.0})
    alice.put(f"/api/progress/episode/{other_ep}",
              json={"position_seconds": 90.0, "duration_seconds": 600.0})

    r = alice.post(f"/api/progress/show/{show_id}/reset")
    assert r.status_code == 200
    assert r.json()["deleted"] == 2  # eps[0] and eps[1] for alice

    # Alice's progress on this show is gone.
    assert alice.get(f"/api/progress/episode/{eps[0]}").json()["position_seconds"] == 0.0
    assert alice.get(f"/api/progress/episode/{eps[1]}").json()["position_seconds"] == 0.0
    # Alice's other-show progress is intact.
    assert alice.get(f"/api/progress/episode/{other_ep}").json()["position_seconds"] == 90.0
    # Bob's progress on this show is intact.
    assert bob.get(f"/api/progress/episode/{eps[0]}").json()["position_seconds"] == 60.0


def test_series_reset_form_redirects_to_show(tmp_path):
    db_path, _, show_id, eps = _setup_show(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    client.put(f"/api/progress/episode/{eps[0]}",
               json={"position_seconds": 60.0, "duration_seconds": 600.0})
    r = client.post(f"/progress/show/{show_id}/reset", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/show/{show_id}"


def test_series_reset_404_on_non_show(tmp_path):
    """Refuse to reset something that isn't actually a TV show."""
    root = tmp_path / "movies"; root.mkdir()
    (root / "Big.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root)
    movie_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    r = client.post(f"/api/progress/show/{movie_id}/reset")
    assert r.status_code == 404


def test_reset_series_button_renders_on_episode_page(tmp_path):
    db_path, _, show_id, eps = _setup_show(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    r = client.get(f"/watch/episode/{eps[0]}")
    assert r.status_code == 200
    assert f"action='/progress/show/{show_id}/reset'" in r.text
    assert "Reset series progress" in r.text


def test_series_reset_requires_login(tmp_path):
    db_path, _, show_id, _ = _setup_show(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    assert client.post(f"/api/progress/show/{show_id}/reset").status_code == 401
