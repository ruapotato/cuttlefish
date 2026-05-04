"""Tests for cruft detection, the cruft admin endpoints/page, and the
in-process worker thread spawned by serve --with-worker."""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import cruft, db, scanner
from cuttlefish.server import create_app
from cuttlefish.workers import encoder

FFMPEG = shutil.which("ffmpeg")
ffmpeg_required = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


# --- list_cruft ----------------------------------------------------------


def test_list_cruft_finds_non_media_files(tmp_path: Path):
    root = tmp_path / "movies"
    root.mkdir()
    (root / "Real Movie.mp4").write_bytes(b"")
    (root / "downloadedfrom.txt").write_text("spam")
    (root / "movie.nfo").write_text("scene info")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('m','movies',?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    entries = cruft.list_cruft(conn, lib_id)
    names = sorted(e.path.name for e in entries)
    assert names == ["downloadedfrom.txt", "movie.nfo"]
    assert all(e.reason == "non-media" for e in entries)


def test_list_cruft_keeps_paired_sidecars(tmp_path: Path):
    root = tmp_path / "movies"
    root.mkdir()
    (root / "Movie.mp4").write_bytes(b"")
    (root / "Movie.srt").write_text("subs")  # sidecar — keep
    (root / "Movie.jpg").write_bytes(b"img")  # sidecar — keep
    (root / "Orphan.srt").write_text("subs")  # orphan sidecar — cruft
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('m','movies',?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    entries = cruft.list_cruft(conn, lib_id)
    assert len(entries) == 1
    assert entries[0].path.name == "Orphan.srt"
    assert entries[0].reason == "orphan-sidecar"


def test_list_cruft_skips_hidden_paths(tmp_path: Path):
    root = tmp_path / "movies"
    root.mkdir()
    (root / ".DS_Store").write_bytes(b"x")
    (root / ".cache").mkdir()
    (root / ".cache" / "junk.txt").write_text("x")
    (root / "downloadedfrom.txt").write_text("x")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('m','movies',?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    entries = cruft.list_cruft(conn, lib_id)
    names = [e.path.name for e in entries]
    assert names == ["downloadedfrom.txt"]


def test_list_cruft_audiobook_kind_audio_extensions(tmp_path: Path):
    root = tmp_path / "books"
    root.mkdir()
    (root / "Book").mkdir()
    (root / "Book" / "01.mp3").write_bytes(b"")
    (root / "Book" / "cover.jpg").write_bytes(b"")  # no sibling audio with same stem → cruft
    (root / "Book" / "spam.txt").write_text("x")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('b','audiobooks',?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    entries = cruft.list_cruft(conn, lib_id)
    names = sorted(e.path.name for e in entries)
    assert names == ["cover.jpg", "spam.txt"]


def test_is_path_inside_a_library(tmp_path: Path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "x.txt").write_text("")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('m','movies',?)",
            (str(root),),
        )
    assert cruft.is_path_inside_a_library(conn, root / "x.txt") is True
    assert cruft.is_path_inside_a_library(conn, tmp_path / "outside.txt") is False


# --- admin endpoints + page ---------------------------------------------


def _admin_client(tmp_path: Path):
    root = tmp_path / "movies"
    root.mkdir()
    (root / "Real.mp4").write_bytes(b"")
    (root / "downloadedfrom.txt").write_text("spam")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('m','movies',?)",
            (str(root),),
        )
        scanner.scan_library(conn, cur.lastrowid, root, "movies")
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    return client, root, db_path


def test_api_cruft_list(tmp_path):
    client, root, _ = _admin_client(tmp_path)
    r = client.get("/api/admin/cruft")
    assert r.status_code == 200
    data = r.json()
    assert any(d["path"].endswith("downloadedfrom.txt") for d in data)


def test_api_cruft_delete(tmp_path):
    client, root, _ = _admin_client(tmp_path)
    target = root / "downloadedfrom.txt"
    assert target.is_file()
    r = client.post("/api/admin/cruft/delete", json={"path": str(target)})
    assert r.status_code == 200
    assert not target.exists()


def test_api_cruft_delete_refuses_outside_library(tmp_path):
    client, _, _ = _admin_client(tmp_path)
    bad = tmp_path / "elsewhere.txt"
    bad.write_text("x")
    r = client.post("/api/admin/cruft/delete", json={"path": str(bad)})
    assert r.status_code == 403


def test_api_cruft_delete_refuses_media_file(tmp_path):
    client, root, _ = _admin_client(tmp_path)
    real = root / "Real.mp4"
    r = client.post("/api/admin/cruft/delete", json={"path": str(real)})
    assert r.status_code == 409
    assert real.is_file()


def test_admin_cruft_page_shows_cruft(tmp_path):
    client, _, _ = _admin_client(tmp_path)
    r = client.get("/admin/cruft")
    assert r.status_code == 200
    assert "downloadedfrom.txt" in r.text
    assert "Delete" in r.text


def test_admin_cruft_page_form_delete(tmp_path):
    client, root, _ = _admin_client(tmp_path)
    target = root / "downloadedfrom.txt"
    r = client.post(
        "/admin/cruft/delete", data={"path": str(target)}, follow_redirects=False
    )
    assert r.status_code == 303
    assert not target.exists()


# --- episode encoding ----------------------------------------------------


def _make_video(p: Path, duration: int = 1) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=160x120:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)],
        check=True,
    )


@ffmpeg_required
def test_encode_episode_creates_season_layout(tmp_path):
    root = tmp_path / "tv"
    show = root / "My Show"
    s1 = show / "Season 01"
    _make_video(s1 / "My Show - S01E03 - Pilot.mp4")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('tv','tv',?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root, "tv")
    ep_id = conn.execute("SELECT id FROM tv_episodes").fetchone()["id"]

    result = encoder.encode_episode(conn, ep_id, ffmpeg=FFMPEG)
    assert result.video_path.is_file()
    assert "Season 01" in result.clean_dir.parts
    assert "S01E03" in result.video_path.name
    row = conn.execute(
        "SELECT video_path FROM encoded_episodes WHERE episode_id = ?", (ep_id,)
    ).fetchone()
    assert row["video_path"] == str(result.video_path)


@ffmpeg_required
def test_run_worker_handles_episode_jobs(tmp_path):
    root = tmp_path / "tv"
    s1 = root / "Show" / "Season 01"
    _make_video(s1 / "Show - S01E01.mp4")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('tv','tv',?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root, "tv")
    ep_id = conn.execute("SELECT id FROM tv_episodes").fetchone()["id"]
    encoder.enqueue_episode_encode(conn, ep_id)
    n = encoder.run_worker(db_path=db_path, once=True, ffmpeg=FFMPEG)
    assert n == 1
    job = conn.execute(
        "SELECT status FROM jobs WHERE episode_id = ?", (ep_id,)
    ).fetchone()
    assert job["status"] == "done"


@ffmpeg_required
def test_admin_episode_encode_endpoint(tmp_path):
    root = tmp_path / "tv"
    s1 = root / "Show" / "Season 01"
    _make_video(s1 / "Show - S01E01.mp4")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('tv','tv',?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root, "tv")
    ep_id = conn.execute("SELECT id FROM tv_episodes").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    r = client.post(f"/api/admin/encode/episode/{ep_id}")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    job = conn.execute(
        "SELECT kind, status FROM jobs WHERE episode_id = ?", (ep_id,)
    ).fetchone()
    assert job["kind"] == "encode" and job["status"] == "queued"


# --- in-process worker thread (used by serve --with-worker) -------------


@ffmpeg_required
def test_run_worker_in_thread_processes_a_job(tmp_path):
    """Mirrors what serve --with-worker does."""
    root = tmp_path / "movies"
    root.mkdir()
    _make_video(root / "Tiny.mp4")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES ('m','movies',?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root, "movies")
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]

    encoder.enqueue_encode(conn, media_id)

    t = threading.Thread(
        target=encoder.run_worker,
        kwargs={"db_path": db_path, "once": True, "ffmpeg": FFMPEG},
        daemon=True,
    )
    t.start()
    t.join(timeout=30)
    assert not t.is_alive(), "worker thread did not finish in time"

    fresh = db.connect(db_path)
    job = fresh.execute("SELECT status FROM jobs").fetchone()
    assert job["status"] == "done"
    enc = fresh.execute("SELECT video_path FROM encoded_files WHERE media_id = ?", (media_id,)).fetchone()
    assert enc and Path(enc["video_path"]).is_file()
