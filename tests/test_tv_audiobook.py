"""Tests for TV show + audiobook UI, streaming, and progress."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import db, scanner
from cuttlefish.server import create_app


@pytest.fixture
def tv_db(tmp_path: Path):
    root = tmp_path / "tv"
    root.mkdir()
    show = root / "My Show"
    s1 = show / "Season 01"
    s2 = show / "S2"
    (s1).mkdir(parents=True)
    (s2).mkdir(parents=True)
    (s1 / "My Show - S01E01 - Pilot.mp4").write_bytes(b"P" * 100)
    (s1 / "My Show - S01E02 - Two.mp4").write_bytes(b"T" * 100)
    (s2 / "My Show - S02E01 - Return.mp4").write_bytes(b"R" * 100)
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES (?, ?)",
            ("tv", str(root)),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root)
    show_id = conn.execute("SELECT id FROM media WHERE kind='tv_show'").fetchone()["id"]
    return db_path, show_id


@pytest.fixture
def book_db(tmp_path: Path):
    root = tmp_path / "books"
    root.mkdir()
    book = root / "My Book"
    book.mkdir()
    (book / "01.mp3").write_bytes(b"a" * 50)
    (book / "02.mp3").write_bytes(b"b" * 50)
    (book / "03.mp3").write_bytes(b"c" * 50)
    db_path = tmp_path / "b.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES (?, ?)",
            ("books", str(root)),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root)
    book_id = conn.execute("SELECT id FROM media WHERE kind='audiobook'").fetchone()["id"]
    return db_path, book_id


def _login_admin(client: TestClient) -> None:
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})


# --- TV ------------------------------------------------------------------


def test_show_page_lists_episodes_grouped_by_season(tv_db):
    db_path, show_id = tv_db
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/show/{show_id}")
    assert r.status_code == 200
    assert "Season 1" in r.text and "Season 2" in r.text
    assert "S01E01" in r.text and "S02E01" in r.text


def test_watch_show_redirects_to_show_page(tv_db):
    db_path, show_id = tv_db
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/watch/{show_id}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/show/{show_id}"


def test_episode_watch_page(tv_db):
    db_path, _ = tv_db
    client = TestClient(create_app(db_path=db_path))
    ep_id = db.connect(db_path).execute(
        "SELECT id FROM tv_episodes ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    r = client.get(f"/watch/episode/{ep_id}")
    assert r.status_code == 200
    assert f"/stream/episode/{ep_id}" in r.text
    assert "<video" in r.text


def test_stream_episode(tv_db):
    db_path, _ = tv_db
    client = TestClient(create_app(db_path=db_path))
    ep_id = db.connect(db_path).execute(
        "SELECT id FROM tv_episodes ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    r = client.get(f"/stream/episode/{ep_id}", headers={"Range": "bytes=0-49"})
    assert r.status_code == 206
    assert r.headers["content-length"] == "50"


def test_episode_progress_roundtrip(tv_db):
    db_path, _ = tv_db
    client = TestClient(create_app(db_path=db_path))
    _login_admin(client)
    ep_id = db.connect(db_path).execute(
        "SELECT id FROM tv_episodes ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    assert client.get(f"/api/progress/episode/{ep_id}").json()["position_seconds"] == 0.0
    client.put(f"/api/progress/episode/{ep_id}", json={"position_seconds": 88.0, "duration_seconds": 1200.0})
    r = client.get(f"/api/progress/episode/{ep_id}")
    assert r.json()["position_seconds"] == 88.0
    assert r.json()["duration_seconds"] == 1200.0


def test_episode_progress_unknown_404(tv_db):
    db_path, _ = tv_db
    client = TestClient(create_app(db_path=db_path))
    _login_admin(client)
    r = client.put("/api/progress/episode/9999", json={"position_seconds": 1.0})
    assert r.status_code == 404


# --- Audiobooks ----------------------------------------------------------


def test_book_page_lists_chapters(book_db):
    db_path, book_id = book_db
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/book/{book_id}")
    assert r.status_code == 200
    assert "01.mp3" in r.text and "02.mp3" in r.text and "03.mp3" in r.text
    assert "playlist" in r.text  # JS playlist embedded
    assert "<audio" in r.text


def test_watch_book_redirects(book_db):
    db_path, book_id = book_db
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/watch/{book_id}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/book/{book_id}"


def test_stream_track(book_db):
    db_path, _ = book_db
    client = TestClient(create_app(db_path=db_path))
    track_id = db.connect(db_path).execute(
        "SELECT id FROM audiobook_tracks ORDER BY order_index LIMIT 1"
    ).fetchone()["id"]
    r = client.get(f"/stream/track/{track_id}")
    assert r.status_code == 200
    assert r.headers["accept-ranges"] == "bytes"


def test_book_progress_roundtrip(book_db):
    db_path, book_id = book_db
    client = TestClient(create_app(db_path=db_path))
    _login_admin(client)
    tracks = db.connect(db_path).execute(
        "SELECT id FROM audiobook_tracks WHERE book_id = ? ORDER BY order_index", (book_id,)
    ).fetchall()
    track_id = tracks[1]["id"]  # second chapter
    assert client.get(f"/api/progress/book/{book_id}").json()["current_track_id"] is None
    client.put(
        f"/api/progress/book/{book_id}",
        json={"track_id": track_id, "position_seconds": 130.5},
    )
    r = client.get(f"/api/progress/book/{book_id}")
    assert r.json()["current_track_id"] == track_id
    assert r.json()["position_seconds"] == 130.5


def test_book_progress_rejects_track_from_other_book(book_db, tmp_path):
    db_path, book_id = book_db
    # Add a SECOND book with its own track in the same DB
    conn = db.connect(db_path)
    other_root = tmp_path / "other"
    (other_root / "Other").mkdir(parents=True)
    (other_root / "Other" / "01.mp3").write_bytes(b"x" * 10)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('other', ?)",
            (str(other_root),),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, other_root)
    other_track = conn.execute(
        "SELECT t.id FROM audiobook_tracks t JOIN media m ON m.id = t.book_id "
        "WHERE m.library_id = ?", (lib_id,)
    ).fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    _login_admin(client)
    r = client.put(
        f"/api/progress/book/{book_id}",
        json={"track_id": other_track, "position_seconds": 1.0},
    )
    assert r.status_code == 404


def test_book_progress_unknown_book_404(book_db):
    db_path, _ = book_db
    client = TestClient(create_app(db_path=db_path))
    _login_admin(client)
    r = client.put("/api/progress/book/9999", json={"track_id": 1, "position_seconds": 1.0})
    assert r.status_code == 404


# --- library page links to the right destination -------------------------


def test_library_page_links_to_show_for_tv_kind(tv_db):
    db_path, show_id = tv_db
    client = TestClient(create_app(db_path=db_path))
    lib_id = db.connect(db_path).execute("SELECT id FROM libraries").fetchone()["id"]
    r = client.get(f"/library/{lib_id}")
    assert f"href='/show/{show_id}'" in r.text


def test_library_page_links_to_book_for_audiobook_kind(book_db):
    db_path, book_id = book_db
    client = TestClient(create_app(db_path=db_path))
    lib_id = db.connect(db_path).execute("SELECT id FROM libraries").fetchone()["id"]
    r = client.get(f"/library/{lib_id}")
    assert f"href='/book/{book_id}'" in r.text
