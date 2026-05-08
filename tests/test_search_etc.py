"""Tests for subtitles (SRT→VTT), search, continue-watching."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import db, scanner, subtitles
from cuttlefish.server import create_app


SRT_SAMPLE = """1
00:00:00,000 --> 00:00:01,500
Hello

2
00:00:02,000 --> 00:00:04,000
World, comma in text
"""


# --- subtitles helpers --------------------------------------------------


def test_srt_to_vtt_format():
    out = subtitles.srt_to_vtt(SRT_SAMPLE)
    assert out.startswith("WEBVTT\n")
    # Timestamp commas converted to dots
    assert "00:00:00.000 --> 00:00:01.500" in out
    # Comma INSIDE the cue text must NOT be converted
    assert "World, comma in text" in out


def test_srt_to_vtt_already_vtt_passthrough():
    # If we hand it a vtt-like string with "-->", it leaves cue text alone
    text = "WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\nhi\n"
    # srt_to_vtt naively prepends WEBVTT only when starting from SRT, but
    # is safe on already-converted strings too — _serve_vtt only invokes
    # srt_to_vtt on .srt extensions. So just verify the function isn't lossy:
    out = subtitles.srt_to_vtt(text)
    assert "00:00:00.000 --> 00:00:01.000" in out


def test_find_sidecar_subtitle(tmp_path):
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"")
    (tmp_path / "movie.srt").write_text("subs")
    assert subtitles.find_sidecar_subtitle(video).name == "movie.srt"


def test_find_sidecar_subtitle_lang_variant(tmp_path):
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"")
    (tmp_path / "movie.en.srt").write_text("subs")
    found = subtitles.find_sidecar_subtitle(video)
    assert found is not None and found.name == "movie.en.srt"


def test_find_sidecar_subtitle_none(tmp_path):
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"")
    assert subtitles.find_sidecar_subtitle(video) is None


# --- subtitle endpoints ------------------------------------------------


def _setup_movie_with_sidecar(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    (root / "Movie.mp4").write_bytes(b"")
    (root / "Movie.srt").write_text(SRT_SAMPLE)
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(root),),
        )
        scanner.scan_library(conn, cur.lastrowid, root)
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    return db_path, media_id


def test_subtitle_endpoint_serves_vtt(tmp_path):
    db_path, media_id = _setup_movie_with_sidecar(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/subtitle/{media_id}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/vtt")
    assert r.text.startswith("WEBVTT")
    assert "-->" in r.text


def test_subtitle_endpoint_404_when_no_sidecar(tmp_path):
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
    client = TestClient(create_app(db_path=db_path))
    assert client.get(f"/subtitle/{media_id}").status_code == 404


def test_watch_page_includes_track_when_subtitle_present(tmp_path):
    db_path, media_id = _setup_movie_with_sidecar(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/watch/{media_id}")
    assert r.status_code == 200
    assert "<track" in r.text
    assert f"/subtitle/{media_id}" in r.text


# --- search ------------------------------------------------------------


def _populate_for_search(tmp_path):
    movies = tmp_path / "movies"
    movies.mkdir()
    (movies / "Coffee Run.mp4").write_bytes(b"")
    (movies / "Big Buck Bunny.mp4").write_bytes(b"")
    tv = tmp_path / "tv"
    show = tv / "Coffee Show"
    s1 = show / "Season 01"
    s1.mkdir(parents=True)
    (s1 / "Coffee Show - S01E01 - Espresso.mp4").write_bytes(b"")
    db_path = tmp_path / "s.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        m = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('movies', ?)",
            (str(movies),),
        ); m_lib = m.lastrowid
        t = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('tv', ?)",
            (str(tv),),
        ); t_lib = t.lastrowid
    scanner.scan_library(conn, m_lib, movies)
    scanner.scan_library(conn, t_lib, tv)
    return db_path


def test_api_search_returns_movies_and_episodes(tmp_path):
    db_path = _populate_for_search(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    r = client.get("/api/search?q=Coffee")
    assert r.status_code == 200
    data = r.json()
    titles = [m["title_guess"] for m in data["media"]]
    assert "Coffee Run" in titles
    show_titles = [m["title_guess"] for m in data["media"] if m["kind"] == "tv_show"]
    assert "Coffee Show" in show_titles
    ep_show_titles = [e["show_title"] for e in data["episodes"]]
    assert "Coffee Show" in ep_show_titles


def test_api_search_empty_query(tmp_path):
    db_path = _populate_for_search(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    r = client.get("/api/search?q=")
    assert r.status_code == 200
    assert r.json() == {"media": [], "episodes": []}


def test_search_page_renders(tmp_path):
    db_path = _populate_for_search(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    r = client.get("/search?q=Coffee")
    assert r.status_code == 200
    assert "Coffee Run" in r.text
    assert "Coffee Show" in r.text


def test_search_page_no_query_shows_form(tmp_path):
    db_path = _populate_for_search(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    r = client.get("/search")
    assert r.status_code == 200
    assert "<form" in r.text and "name='q'" in r.text


# --- continue watching --------------------------------------------------


def test_continue_watching_requires_login(tmp_path):
    db_path = _populate_for_search(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    # Only the JSON API stays — the standalone HTML page was retired in
    # favor of the home-page section.
    assert client.get("/api/continue-watching").status_code == 401


def test_continue_watching_returns_progress_items(tmp_path):
    db_path = _populate_for_search(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    # Initially empty
    assert client.get("/api/continue-watching").json() == {
        "media": [], "episodes": [], "audiobooks": [],
    }
    # Save progress on movie id=1 (Big Buck Bunny first alphabetically; but we don't care about which)
    media_id = db.connect(db_path).execute("SELECT id FROM media WHERE kind='movie' LIMIT 1").fetchone()["id"]
    client.put(f"/api/progress/{media_id}", json={"position_seconds": 60.0, "duration_seconds": 600.0})
    data = client.get("/api/continue-watching").json()
    assert len(data["media"]) == 1
    assert data["media"][0]["position_seconds"] == 60.0


def test_continue_watching_includes_episodes(tmp_path):
    db_path = _populate_for_search(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    ep_id = db.connect(db_path).execute("SELECT id FROM tv_episodes LIMIT 1").fetchone()["id"]
    client.put(f"/api/progress/episode/{ep_id}", json={"position_seconds": 30.0})
    data = client.get("/api/continue-watching").json()
    assert any(e["id"] == ep_id for e in data["episodes"])


def test_continue_watching_skips_zero_position(tmp_path):
    """Progress = 0.0 (e.g. user opened then closed) should not show up."""
    db_path = _populate_for_search(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    media_id = db.connect(db_path).execute("SELECT id FROM media WHERE kind='movie' LIMIT 1").fetchone()["id"]
    client.put(f"/api/progress/{media_id}", json={"position_seconds": 0.0})
    data = client.get("/api/continue-watching").json()
    assert data["media"] == []


def test_continue_watching_renders_on_home_with_progress_label(tmp_path):
    """The home-page Continue Watching section shows a progress badge
    (mm:ss / mm:ss) on the in-progress card."""
    db_path = _populate_for_search(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    media_id = db.connect(db_path).execute("SELECT id FROM media WHERE kind='movie' LIMIT 1").fetchone()["id"]
    client.put(f"/api/progress/{media_id}", json={"position_seconds": 90.0, "duration_seconds": 600.0})
    r = client.get("/")
    assert r.status_code == 200
    assert "Continue watching" in r.text
    # The progress label "1:30 / 10:00 (15%)"
    assert "1:30" in r.text and "10:00" in r.text
