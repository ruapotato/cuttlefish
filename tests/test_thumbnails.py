"""Thumbnail generation + merged home page tests."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import db, scanner, thumbnails
from cuttlefish.server import create_app

FFMPEG = shutil.which("ffmpeg")
ffmpeg_required = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


def _make_video(p: Path, duration: int = 6) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=160x120:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)],
        check=True,
    )


# --- core thumbnail generation -------------------------------------------


def test_cache_dir_respects_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert thumbnails.cache_dir() == tmp_path / "cache" / "cuttlefish" / "thumbs"


def test_media_and_episode_thumb_paths_are_distinct(tmp_path):
    a = thumbnails.media_thumb_path(7)
    b = thumbnails.episode_thumb_path(7)
    assert a != b
    assert "media-7" in a.name
    assert "episode-7" in b.name


def test_generate_thumbnail_returns_none_for_missing_file(tmp_path):
    out = tmp_path / "out.jpg"
    assert thumbnails.generate_thumbnail(tmp_path / "nope.mp4", out) is None
    assert not out.exists()


def test_generate_thumbnail_returns_none_for_empty_file(tmp_path):
    src = tmp_path / "empty.mp4"
    src.write_bytes(b"")
    out = tmp_path / "out.jpg"
    # The empty-file short-circuit lives in get_duration / generate's flow.
    # If ffmpeg is invoked it'll error and we'll get None either way.
    assert thumbnails.generate_thumbnail(src, out) is None


@ffmpeg_required
def test_generate_thumbnail_produces_image(tmp_path):
    src = tmp_path / "v.mp4"
    _make_video(src, duration=6)  # short, so the 10% rule kicks in
    out = tmp_path / "out.jpg"
    result = thumbnails.generate_thumbnail(src, out, ffmpeg=FFMPEG)
    assert result == out
    assert out.is_file()
    assert out.stat().st_size > 100  # actually has bytes


@ffmpeg_required
def test_get_or_generate_returns_cached_on_second_call(tmp_path):
    src = tmp_path / "v.mp4"
    _make_video(src, duration=6)
    out = tmp_path / "out.jpg"
    a = thumbnails.get_or_generate(src, out, ffmpeg=FFMPEG)
    assert a == out
    mtime = out.stat().st_mtime
    # Second call should return the same path without regenerating.
    b = thumbnails.get_or_generate(src, out, ffmpeg=FFMPEG)
    assert b == out
    assert out.stat().st_mtime == mtime  # not re-written


# --- /poster fallback to generation -------------------------------------


@ffmpeg_required
def test_poster_endpoint_generates_thumbnail_on_demand(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    src = root / "Movie.mp4"
    _make_video(src, duration=6)
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    # Scanner already generated a thumbnail. Confirm /poster serves it.
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/poster/{media_id}")
    assert r.status_code == 200
    # Image bytes (JPEG starts with 0xFF 0xD8)
    assert r.content[:2] == b"\xff\xd8"


@ffmpeg_required
def test_poster_endpoint_for_show_uses_first_episode_frame(tmp_path):
    root = tmp_path / "tv"
    s1 = root / "Show" / "Season 01"
    _make_video(s1 / "S01E01.mp4", duration=6)
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('tv', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    show_id = conn.execute("SELECT id FROM media WHERE kind='tv_show'").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/poster/{show_id}")
    assert r.status_code == 200
    assert r.content[:2] == b"\xff\xd8"


def test_poster_endpoint_404_for_audiobook_without_cover(tmp_path):
    root = tmp_path / "books"
    book = root / "Book"
    book.mkdir(parents=True)
    (book / "01.mp3").write_bytes(b"")  # no cover
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('b', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    assert client.get(f"/poster/{media_id}").status_code == 404


def test_poster_endpoint_prefers_real_poster_over_generated(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "Movie.mp4").write_bytes(b"")  # empty so generation fails
    real_bytes = b"\xff\xd8\xff\xe0real-jpeg"
    (root / "Movie.jpg").write_bytes(real_bytes)
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
    r = client.get(f"/poster/{media_id}")
    assert r.status_code == 200
    assert r.content == real_bytes


# --- merged home page ----------------------------------------------------


def test_home_empty_state(tmp_path):
    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    client = TestClient(create_app(db_path=db_path))
    r = client.get("/")
    assert r.status_code == 200
    assert "No libraries yet" in r.text


def test_home_shows_movies_and_tv_and_audiobooks_in_one_page(tmp_path):
    """A single library with mixed contents → home shows all sections merged."""
    root = tmp_path / "media"
    root.mkdir()
    (root / "A Movie.mp4").write_bytes(b"")
    (root / "Show" / "Season 01" / "S01E01.mp4").parent.mkdir(parents=True)
    (root / "Show" / "Season 01" / "S01E01.mp4").write_bytes(b"")
    (root / "Book").mkdir()
    (root / "Book" / "01.mp3").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('media', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    client = TestClient(create_app(db_path=db_path))
    r = client.get("/")
    assert r.status_code == 200
    assert "Movies" in r.text and "(1)" in r.text
    assert "TV Shows" in r.text
    assert "Audiobooks" in r.text
    # Items themselves
    assert "A Movie" in r.text
    assert "Show" in r.text
    assert "Book" in r.text


def test_home_merges_across_multiple_libraries(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    (a / "Movie A.mp4").write_bytes(b"")
    (b / "Movie B.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur_a = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('a', ?)",
            (str(a),),
        )
        cur_b = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('b', ?)",
            (str(b),),
        )
    scanner.scan_library(conn, cur_a.lastrowid, a)
    scanner.scan_library(conn, cur_b.lastrowid, b)
    client = TestClient(create_app(db_path=db_path))
    r = client.get("/")
    # Both movies show up in the same Movies section
    assert "Movie A" in r.text and "Movie B" in r.text
    assert "(2)" in r.text  # count in section header


def test_home_hides_empty_sections(tmp_path):
    """If there are no audiobooks, the Audiobooks section is hidden."""
    root = tmp_path / "media"
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
    client = TestClient(create_app(db_path=db_path))
    r = client.get("/")
    # Movie present
    assert "Movies" in r.text
    # No TV / audiobook sections
    assert "TV Shows" not in r.text
    assert "Audiobooks" not in r.text


def test_home_no_libraries_just_no_media_message(tmp_path):
    """Library exists but no media scanned yet."""
    root = tmp_path / "media"; root.mkdir()
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(root),),
        )
    client = TestClient(create_app(db_path=db_path))
    r = client.get("/")
    assert r.status_code == 200
    assert "No media yet" in r.text or "No media" in r.text
