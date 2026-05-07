"""Jellyfin/Emby-style TV UX: click show → next episode + episode strip."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import db, scanner
from cuttlefish.server import create_app


def _setup_show_with_two_seasons(tmp_path: Path):
    """Build a TV library with one show, 2 seasons × 2 episodes each."""
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
    return db_path, conn


def _login(client, username="alice"):
    client.post("/api/auth/register",
                data={"username": username, "password": "secret123"})
    client.post("/api/auth/login",
                data={"username": username, "password": "secret123"})


# --- /show/{id} now redirects to next episode ---------------------------


def test_show_redirects_anonymous_to_first_episode(tmp_path):
    db_path, conn = _setup_show_with_two_seasons(tmp_path)
    show_id = conn.execute("SELECT id FROM media WHERE kind='tv_show'").fetchone()["id"]
    first_ep_id = conn.execute(
        "SELECT id FROM tv_episodes ORDER BY season, episode LIMIT 1"
    ).fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/show/{show_id}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/watch/episode/{first_ep_id}"


def test_show_redirects_logged_in_user_to_next_unwatched(tmp_path):
    """User has watched S01E01 fully — next click on the show should land
    them on S01E02."""
    db_path, conn = _setup_show_with_two_seasons(tmp_path)
    show_id = conn.execute("SELECT id FROM media WHERE kind='tv_show'").fetchone()["id"]
    eps = conn.execute(
        "SELECT id, season, episode FROM tv_episodes ORDER BY season, episode"
    ).fetchall()
    s1e1, s1e2 = eps[0]["id"], eps[1]["id"]

    # Stamp durations + a finished S01E01 progress directly in the DB
    with conn:
        for e in eps:
            conn.execute(
                "UPDATE tv_episodes SET duration_seconds = 600 WHERE id = ?",
                (e["id"],),
            )

    client = TestClient(create_app(db_path=db_path))
    _login(client)
    me = client.get("/api/me").json()
    user_id = me["id"]
    with conn:
        conn.execute(
            "INSERT INTO episode_progress (user_id, episode_id, position_seconds, duration_seconds) "
            "VALUES (?, ?, ?, ?)",
            (user_id, s1e1, 590, 600),  # ~98% — counts as watched
        )

    r = client.get(f"/show/{show_id}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/watch/episode/{s1e2}"


def test_show_redirects_to_first_when_all_watched(tmp_path):
    db_path, conn = _setup_show_with_two_seasons(tmp_path)
    show_id = conn.execute("SELECT id FROM media WHERE kind='tv_show'").fetchone()["id"]
    eps = conn.execute(
        "SELECT id FROM tv_episodes ORDER BY season, episode"
    ).fetchall()

    with conn:
        for e in eps:
            conn.execute(
                "UPDATE tv_episodes SET duration_seconds = 600 WHERE id = ?",
                (e["id"],),
            )

    client = TestClient(create_app(db_path=db_path))
    _login(client)
    user_id = client.get("/api/me").json()["id"]
    # Mark every episode as watched
    with conn:
        for e in eps:
            conn.execute(
                "INSERT INTO episode_progress (user_id, episode_id, position_seconds, duration_seconds) "
                "VALUES (?, ?, 600, 600)",
                (user_id, e["id"]),
            )
    r = client.get(f"/show/{show_id}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/watch/episode/{eps[0]['id']}"


def test_show_with_no_episodes_falls_back_to_old_page(tmp_path):
    """A scanned show that somehow has no tv_episodes rows should not 500;
    fall back to a small placeholder page."""
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('tv', ?)",
            (str(tmp_path),),
        )
        conn.execute(
            "INSERT INTO media (library_id, kind, source_path, title_guess) "
            "VALUES (?, 'tv_show', ?, 'Empty Show')",
            (cur.lastrowid, str(tmp_path / "Empty Show")),
        )
    show_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/show/{show_id}", follow_redirects=False)
    assert r.status_code == 200
    assert "No episodes" in r.text


# --- Episode page: strip, season tabs, current highlight ---------------


def test_episode_page_renders_strip_with_all_episodes(tmp_path):
    db_path, conn = _setup_show_with_two_seasons(tmp_path)
    eps = conn.execute(
        "SELECT id, season FROM tv_episodes ORDER BY season, episode"
    ).fetchall()
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/watch/episode/{eps[0]['id']}")
    assert r.status_code == 200
    # Strip exists
    assert "episode-strip" in r.text
    # Season tabs present (there are 2 seasons)
    assert "Season 1" in r.text and "Season 2" in r.text
    # All four episodes have cards (links to /watch/episode/{id})
    for e in eps:
        assert f"/watch/episode/{e['id']}" in r.text
    # Per-episode poster URLs
    for e in eps:
        assert f"/poster/episode/{e['id']}" in r.text


def test_specials_folder_stored_under_season_zero(tmp_path):
    """A folder literally named 'Specials' inside a show goes under
    season=0 (Plex/Jellyfin convention), not numbered alongside real
    seasons."""
    root = tmp_path / "tv"
    show = root / "Some Show"
    s1 = show / "Season 01"
    specials = show / "Specials"
    s1.mkdir(parents=True)
    specials.mkdir(parents=True)
    (s1 / "Some Show - S01E01.mp4").write_bytes(b"")
    (specials / "Some Show - Special.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('tv', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    seasons = sorted(
        r["season"] for r in conn.execute("SELECT season FROM tv_episodes").fetchall()
    )
    assert seasons == [0, 1]


@pytest.mark.parametrize("name", [
    "Extras", "extras", "Bonus", "Bonus Features", "Behind the Scenes",
    "Deleted Scenes", "Featurettes", "Trailers",
])
def test_extras_aliases_all_map_to_season_zero(tmp_path, name):
    root = tmp_path / "tv"
    show = root / "Show"
    extra_dir = show / name
    s1 = show / "Season 01"
    s1.mkdir(parents=True)
    extra_dir.mkdir(parents=True)
    (s1 / "Show - S01E01.mp4").write_bytes(b"")
    (extra_dir / "thing.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('tv', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    seasons = sorted(
        r["season"] for r in conn.execute("SELECT season FROM tv_episodes").fetchall()
    )
    assert 0 in seasons


def test_show_redirect_skips_extras_when_regular_episodes_exist(tmp_path):
    """Clicking a show shouldn't auto-play a Special when there are real
    Season 1 episodes still unwatched."""
    root = tmp_path / "tv"
    show = root / "Show"
    s1 = show / "Season 01"
    extras = show / "Specials"
    s1.mkdir(parents=True)
    extras.mkdir(parents=True)
    (s1 / "Show - S01E01.mp4").write_bytes(b"")
    (extras / "Show - Special.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('tv', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    show_id = conn.execute("SELECT id FROM media WHERE kind='tv_show'").fetchone()["id"]
    s1e1_id = conn.execute(
        "SELECT id FROM tv_episodes WHERE season = 1 LIMIT 1"
    ).fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/show/{show_id}", follow_redirects=False)
    assert r.headers["location"] == f"/watch/episode/{s1e1_id}"


def test_show_redirect_falls_back_to_extras_if_only_extras(tmp_path):
    """A show that contains ONLY Specials (no regular seasons) should
    still play the first extra rather than 404'ing."""
    root = tmp_path / "tv"
    show = root / "Show"
    extras = show / "Specials"
    extras.mkdir(parents=True)
    (extras / "Show - Special.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('tv', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    show_id = conn.execute("SELECT id FROM media WHERE kind='tv_show'").fetchone()["id"]
    extras_id = conn.execute("SELECT id FROM tv_episodes").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/show/{show_id}", follow_redirects=False)
    assert r.headers["location"] == f"/watch/episode/{extras_id}"


def test_episode_strip_renders_extras_tab_last(tmp_path):
    """A show with Season 1 + Specials gets two tabs in order: 'Season 1'
    then 'Extras'."""
    root = tmp_path / "tv"
    show = root / "Show"
    s1 = show / "Season 01"
    extras = show / "Specials"
    s1.mkdir(parents=True)
    extras.mkdir(parents=True)
    (s1 / "Show - S01E01.mp4").write_bytes(b"")
    (extras / "Show - X.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('tv', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    s1e1 = conn.execute(
        "SELECT id FROM tv_episodes WHERE season = 1 LIMIT 1"
    ).fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/watch/episode/{s1e1}")
    body = r.text
    # Both labels render in the tabs row.
    assert "Season 1" in body
    assert "Extras" in body
    # And 'Extras' tab comes AFTER 'Season 1' in the source order.
    assert body.index("Season 1") < body.index("Extras")


def test_episode_page_hides_other_seasons_via_hidden_attr_and_css(tmp_path):
    """The non-current season block carries the HTML `hidden` attribute
    AND a matching CSS rule that overrides our explicit display:grid —
    otherwise both seasons would render simultaneously."""
    db_path, conn = _setup_show_with_two_seasons(tmp_path)
    s2_first = conn.execute(
        "SELECT id FROM tv_episodes WHERE season=2 ORDER BY episode LIMIT 1"
    ).fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/watch/episode/{s2_first}")
    assert r.status_code == 200
    # We're on a season-2 episode, so the season-2 block should NOT be hidden
    # and the season-1 block should be.
    assert "<div class='ep-cards' data-season='1' hidden>" in r.text
    assert "<div class='ep-cards' data-season='2'>" in r.text
    # CSS override that makes [hidden] actually hide despite display:grid
    assert ".ep-cards[hidden]" in r.text
    assert "display: none" in r.text


def test_episode_strip_marks_current_episode(tmp_path):
    db_path, conn = _setup_show_with_two_seasons(tmp_path)
    eps = conn.execute(
        "SELECT id FROM tv_episodes ORDER BY season, episode"
    ).fetchall()
    s1e2 = eps[1]["id"]
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/watch/episode/{s1e2}")
    # The current episode's <a> should have the 'current' class.
    # Find the snippet around /watch/episode/{s1e2}.
    import re
    m = re.search(rf"<a class='([^']*)'\s+href='/watch/episode/{s1e2}'", r.text)
    assert m, "current episode link not found"
    assert "current" in m.group(1)


def test_episode_strip_shows_progress_indicator(tmp_path):
    db_path, conn = _setup_show_with_two_seasons(tmp_path)
    eps = conn.execute(
        "SELECT id FROM tv_episodes ORDER BY season, episode"
    ).fetchall()
    with conn:
        for e in eps:
            conn.execute(
                "UPDATE tv_episodes SET duration_seconds = 600 WHERE id = ?",
                (e["id"],),
            )
    client = TestClient(create_app(db_path=db_path))
    _login(client)
    user_id = client.get("/api/me").json()["id"]
    # S01E01 watched, S01E02 in progress at 30%, S02E01 untouched
    with conn:
        conn.execute(
            "INSERT INTO episode_progress (user_id, episode_id, position_seconds, duration_seconds) "
            "VALUES (?, ?, 600, 600)", (user_id, eps[0]["id"]),
        )
        conn.execute(
            "INSERT INTO episode_progress (user_id, episode_id, position_seconds, duration_seconds) "
            "VALUES (?, ?, 180, 600)", (user_id, eps[1]["id"]),
        )
    r = client.get(f"/watch/episode/{eps[2]['id']}")
    # Watched indicator
    assert "ep-watched" in r.text
    # In-progress bar
    assert "ep-progress" in r.text


def test_episode_page_single_season_hides_tabs(tmp_path):
    """If there's only one season, the season-tabs row shouldn't render."""
    root = tmp_path / "tv"
    show = root / "Show"
    s1 = show / "Season 01"
    s1.mkdir(parents=True)
    (s1 / "Show - S01E01.mp4").write_bytes(b"")
    (s1 / "Show - S01E02.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('tv', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    ep_id = conn.execute("SELECT id FROM tv_episodes LIMIT 1").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/watch/episode/{ep_id}")
    assert r.status_code == 200
    # The "season-tabs" string appears in the inline CSS regardless. Check
    # for the actual rendered <div class='season-tabs'> element.
    assert "<div class='season-tabs'>" not in r.text
    # But the strip itself (with both episodes) should still render.
    assert "episode-strip" in r.text
