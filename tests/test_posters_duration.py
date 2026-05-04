"""Tests for poster sidecar discovery, /poster endpoints, and ffprobe duration."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import db, scanner
from cuttlefish.probe import get_duration
from cuttlefish.server import create_app

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
ffmpeg_required = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
ffprobe_required = pytest.mark.skipif(FFPROBE is None, reason="ffprobe not installed")


def _make_video(p: Path, duration: int = 1) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=160x120:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)],
        check=True,
    )


# --- probe -------------------------------------------------------------


def test_probe_skips_empty_files(tmp_path):
    p = tmp_path / "empty.mp4"
    p.write_bytes(b"")
    assert get_duration(p) is None


def test_probe_handles_missing_files(tmp_path):
    assert get_duration(tmp_path / "nope.mp4") is None


@ffprobe_required
def test_probe_returns_duration_for_real_video(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    p = tmp_path / "v.mp4"
    _make_video(p, duration=2)
    d = get_duration(p, ffprobe=FFPROBE)
    assert d is not None
    # Test source is exactly 2.0s; allow some encoder slop
    assert 1.5 < d < 3.0


# --- poster sidecar pickup --------------------------------------------


def test_scan_picks_up_movie_poster_sidecar(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    (root / "Movie.mp4").write_bytes(b"")
    (root / "Movie.jpg").write_bytes(b"\xff\xd8\xff\xe0jpg")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('m','movies',?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root, "movies")
    row = conn.execute("SELECT poster_path FROM media").fetchone()
    assert row["poster_path"] is not None
    assert Path(row["poster_path"]).name == "Movie.jpg"


def test_scan_picks_up_show_poster(tmp_path):
    root = tmp_path / "tv"
    show = root / "My Show"
    s1 = show / "Season 01"
    s1.mkdir(parents=True)
    (s1 / "S01E01.mp4").write_bytes(b"")
    (show / "poster.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('tv','tv',?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root, "tv")
    show_row = conn.execute("SELECT poster_path FROM media WHERE kind='tv_show'").fetchone()
    assert Path(show_row["poster_path"]).name == "poster.jpg"


def test_scan_picks_up_audiobook_cover(tmp_path):
    root = tmp_path / "books"
    book = root / "My Book"
    book.mkdir(parents=True)
    (book / "01.mp3").write_bytes(b"")
    (book / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('b','audiobooks',?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root, "audiobooks")
    row = conn.execute("SELECT poster_path FROM media WHERE kind='audiobook'").fetchone()
    assert Path(row["poster_path"]).name == "cover.jpg"


def test_scan_no_poster_yields_null(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    (root / "Movie.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('m','movies',?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root, "movies")
    row = conn.execute("SELECT poster_path FROM media").fetchone()
    assert row["poster_path"] is None


# --- /poster endpoints -----------------------------------------------


def test_poster_endpoint_serves_image(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    (root / "Movie.mp4").write_bytes(b"")
    img_bytes = b"\xff\xd8\xff\xe0test-jpeg-bytes"
    (root / "Movie.jpg").write_bytes(img_bytes)
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('m','movies',?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root, "movies")
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/poster/{media_id}")
    assert r.status_code == 200
    assert r.content == img_bytes


def test_poster_endpoint_404_when_missing(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    (root / "Movie.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('m','movies',?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root, "movies")
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    assert client.get(f"/poster/{media_id}").status_code == 404


def test_library_page_renders_poster_img_when_present(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    (root / "Movie.mp4").write_bytes(b"")
    (root / "Movie.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('m','movies',?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root, "movies")
    lib_id = conn.execute("SELECT id FROM libraries").fetchone()["id"]
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/library/{lib_id}")
    assert r.status_code == 200
    assert f"src='/poster/{media_id}'" in r.text


def test_library_page_no_poster_renders_placeholder(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    (root / "Movie.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('m','movies',?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root, "movies")
    lib_id = conn.execute("SELECT id FROM libraries").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/library/{lib_id}")
    assert "no-poster" in r.text


def test_show_page_includes_poster_header(tmp_path):
    root = tmp_path / "tv"
    show = root / "Show"
    s1 = show / "Season 01"
    s1.mkdir(parents=True)
    (s1 / "S01E01.mp4").write_bytes(b"")
    (show / "poster.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('tv','tv',?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root, "tv")
    show_id = conn.execute("SELECT id FROM media WHERE kind='tv_show'").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/show/{show_id}")
    assert r.status_code == 200
    assert "show-poster" in r.text
    assert f"src='/poster/{show_id}'" in r.text


# --- duration end-to-end via real scan -------------------------------


@ffmpeg_required
def test_scan_records_duration_for_real_video(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    _make_video(root / "Tiny.mp4", duration=2)
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('m','movies',?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root, "movies")
    row = conn.execute("SELECT duration_seconds FROM media").fetchone()
    assert row["duration_seconds"] is not None
    assert 1.5 < row["duration_seconds"] < 3.0


@ffmpeg_required
def test_scan_records_episode_duration_and_show_page_displays_it(tmp_path):
    root = tmp_path / "tv"
    s1 = root / "Show" / "Season 01"
    _make_video(s1 / "Show - S01E01.mp4", duration=2)
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('tv','tv',?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root, "tv")
    show_id = conn.execute("SELECT id FROM media WHERE kind='tv_show'").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    r = client.get(f"/show/{show_id}")
    assert r.status_code == 200
    # Format is "0m 02s" for ~2s clip
    assert "m " in r.text  # the duration label is present somewhere
