"""Prev / Next episode buttons + auto-advance on the video's `ended`.

The watch-episode page exposes navigation controls so users can skip
forward/back without leaving the player, and a small JS snippet hooks
the video's `ended` event so the next episode loads automatically when
the current one finishes (Netflix-style auto-play).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import db, scanner
from cuttlefish.server import create_app


def _two_seasons(tmp_path: Path):
    """2 seasons × 2 episodes — gives us mid-season + boundary cases."""
    root = tmp_path / "tv"
    show = root / "Show"
    s1 = show / "Season 01"
    s2 = show / "Season 02"
    s1.mkdir(parents=True); s2.mkdir(parents=True)
    (s1 / "Show - S01E01.mp4").write_bytes(b"")
    (s1 / "Show - S01E02.mp4").write_bytes(b"")
    (s2 / "Show - S02E01.mp4").write_bytes(b"")
    (s2 / "Show - S02E02.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('tv', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    eps = conn.execute(
        "SELECT id, season, episode FROM tv_episodes ORDER BY season, episode"
    ).fetchall()
    return db_path, [e["id"] for e in eps]


def _login(client):
    client.post("/api/auth/register",
                data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login",
                data={"username": "a", "password": "secret123"})


def test_mid_season_episode_has_both_prev_and_next(tmp_path):
    """S01E02 is between two same-season siblings — both buttons enabled
    and pointing at the right neighbors."""
    db_path, eps = _two_seasons(tmp_path)
    s1e1, s1e2, s2e1, _ = eps
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    r = client.get(f"/watch/episode/{s1e2}")
    assert r.status_code == 200
    assert f"href='/watch/episode/{s1e1}'" in r.text  # prev
    assert f"href='/watch/episode/{s2e1}'" in r.text  # next
    assert "Previous episode" in r.text
    assert "Next episode" in r.text


def test_first_episode_has_no_prev(tmp_path):
    """S01E01: prev button is rendered but disabled (no link)."""
    db_path, eps = _two_seasons(tmp_path)
    s1e1, s1e2, *_ = eps
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    r = client.get(f"/watch/episode/{s1e1}")
    # The prev button must NOT be a link — no anchor with this episode's
    # own id, and no anchor with any other earlier episode (there is none).
    # The disabled state is rendered as a span, not an <a>.
    assert "<span class='ep-nav-btn disabled'>&larr; Previous episode</span>" in r.text
    # Next is still enabled, pointing at S01E02.
    assert f"href='/watch/episode/{s1e2}'" in r.text


def test_last_episode_has_no_next(tmp_path):
    """S02E02: no next link, and no auto-advance JS on `ended`."""
    db_path, eps = _two_seasons(tmp_path)
    *_, s2e2 = eps
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    r = client.get(f"/watch/episode/{s2e2}")
    assert "<span class='ep-nav-btn disabled'>Next episode &rarr;</span>" in r.text
    # No auto-advance event handler when there's nowhere to go.
    assert "addEventListener('ended'" not in r.text


def test_next_crosses_season_boundary(tmp_path):
    """S01E02 → S02E01."""
    db_path, eps = _two_seasons(tmp_path)
    _, s1e2, s2e1, _ = eps
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    r = client.get(f"/watch/episode/{s1e2}")
    assert f"href='/watch/episode/{s2e1}'" in r.text


def test_prev_crosses_season_boundary(tmp_path):
    """S02E01 → S01E02."""
    db_path, eps = _two_seasons(tmp_path)
    _, s1e2, s2e1, _ = eps
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    r = client.get(f"/watch/episode/{s2e1}")
    assert f"href='/watch/episode/{s1e2}'" in r.text


def test_auto_advance_js_targets_next_episode(tmp_path):
    """The `ended`-event handler must navigate to the next episode URL."""
    db_path, eps = _two_seasons(tmp_path)
    s1e1, s1e2, *_ = eps
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    r = client.get(f"/watch/episode/{s1e1}")
    # The script binds an `ended` listener and contains the next URL.
    assert "addEventListener('ended'" in r.text
    assert f"/watch/episode/{s1e2}" in r.text


def test_only_episode_in_show_has_no_neighbors(tmp_path):
    """Edge case: a one-episode show has both buttons disabled and no
    auto-advance JS."""
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
    only = conn.execute("SELECT id FROM tv_episodes").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    r = client.get(f"/watch/episode/{only}")
    assert "<span class='ep-nav-btn disabled'>&larr; Previous episode</span>" in r.text
    assert "<span class='ep-nav-btn disabled'>Next episode &rarr;</span>" in r.text
    assert "addEventListener('ended'" not in r.text
