"""Tests for the subtitle generation UI and source-file ASR pipeline."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import db, scanner
from cuttlefish.server import create_app
from cuttlefish.workers import asr, encoder

FFMPEG = shutil.which("ffmpeg")
ffmpeg_required = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


def _make_video(p: Path, duration: int = 1) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=160x120:rate=10",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(p)],
        check=True,
    )


def _admin_client_with_movie(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path / "lib"
    root.mkdir()
    _make_video(root / "Tiny.mp4")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('lib', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    return client, db_path, media_id


# --- API: ASR no longer requires encoded version ----------------------


def test_api_asr_works_without_encoded_version(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    client, db_path, media_id = _admin_client_with_movie(tmp_path)
    r = client.post(f"/api/admin/asr/{media_id}")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    job = db.connect(db_path).execute(
        "SELECT kind, media_id, status FROM jobs WHERE media_id = ?", (media_id,)
    ).fetchone()
    assert (job["kind"], job["status"]) == ("asr", "queued")


def test_api_asr_episode_endpoint(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path / "tv"
    s1 = root / "Show" / "Season 01"
    _make_video(s1 / "Show - S01E01.mp4")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('tv', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    ep_id = conn.execute("SELECT id FROM tv_episodes").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    r = client.post(f"/api/admin/asr/episode/{ep_id}")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    job = db.connect(db_path).execute(
        "SELECT kind, episode_id, status FROM jobs WHERE episode_id = ?", (ep_id,)
    ).fetchone()
    assert (job["kind"], job["status"]) == ("asr", "queued")


def test_api_asr_status_endpoint(tmp_path):
    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    r = client.get("/api/admin/asr-status")
    assert r.status_code == 200
    body = r.json()
    # 'available' is False on the test box (we don't install nemo for tests).
    # 'worker_in_process' is False because we haven't started one. 'queued'
    # is 0 because no jobs were enqueued.
    assert body == {"available": False, "worker_in_process": False, "queued": 0}


def test_admin_subtitles_warns_when_worker_not_running(tmp_path, monkeypatch):
    """If [asr] is installed but no worker is running in this process, the
    page should call that out specifically — that was the missing signal
    the user hit."""
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    client, _, _ = _admin_client_with_movie(tmp_path)
    # Pretend [asr] is installed; mark_worker_started has not been called.
    monkeypatch.setattr(asr, "is_available", lambda: True)
    asr._worker_in_process = False
    r = client.get("/admin/subtitles")
    assert r.status_code == 200
    assert "no worker is running" in r.text
    assert "--with-asr-worker" in r.text


def test_admin_subtitles_green_state_when_worker_running(tmp_path, monkeypatch):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    client, _, _ = _admin_client_with_movie(tmp_path)
    monkeypatch.setattr(asr, "is_available", lambda: True)
    asr._worker_in_process = True
    try:
        r = client.get("/admin/subtitles")
        assert r.status_code == 200
        assert "ASR worker is running in this process" in r.text
    finally:
        asr._worker_in_process = False  # reset for other tests


def test_admin_subtitles_shows_pending_count(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    client, db_path, media_id = _admin_client_with_movie(tmp_path)
    client.post(f"/admin/asr/{media_id}", follow_redirects=False)
    r = client.get("/admin/subtitles")
    assert "1 ASR job(s) waiting" in r.text


# --- ASR resolver: writes SRT in the right place per scenario ---------


def test_resolve_asr_target_for_loose_movie(tmp_path):
    """Movie scanned as a loose video → SRT goes next to source."""
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path / "lib"; root.mkdir()
    _make_video(root / "Tiny.mp4")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('lib', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    job_id = encoder.enqueue_encode(conn, media_id)  # any job row to fetch
    # Simulate an asr job by inserting one
    with conn:
        cur = conn.execute("INSERT INTO jobs (kind, media_id) VALUES ('asr', ?)", (media_id,))
    job = conn.execute("SELECT id, kind, media_id, episode_id FROM jobs WHERE id = ?",
                       (cur.lastrowid,)).fetchone()
    video, srt, kind = asr._resolve_asr_target(conn, job)
    assert video.name == "Tiny.mp4"
    assert srt.name == "Tiny.srt"
    assert srt.parent == root
    assert kind == "media"


def test_resolve_asr_target_for_episode(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path / "tv"
    s1 = root / "Show" / "Season 01"
    _make_video(s1 / "S01E01.mp4")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('tv', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    ep_id = conn.execute("SELECT id FROM tv_episodes").fetchone()["id"]
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (kind, episode_id) VALUES ('asr', ?)", (ep_id,)
        )
    job = conn.execute("SELECT id, kind, media_id, episode_id FROM jobs WHERE id = ?",
                       (cur.lastrowid,)).fetchone()
    video, srt, kind = asr._resolve_asr_target(conn, job)
    assert video.name == "S01E01.mp4"
    assert srt.name == "S01E01.srt"
    assert srt.parent == s1
    assert kind == "episode"


# --- /admin/subtitles HTML page --------------------------------------


def test_admin_subtitles_page_lists_items(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    client, _, _ = _admin_client_with_movie(tmp_path)
    r = client.get("/admin/subtitles")
    assert r.status_code == 200
    assert "Tiny" in r.text
    assert "Generate" in r.text
    # The status banner mentions either install or already-installed state
    assert "ASR" in r.text


def test_admin_subtitles_page_requires_admin(tmp_path):
    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    client = TestClient(create_app(db_path=db_path))
    assert client.get("/admin/subtitles").status_code == 401


def test_admin_subtitles_form_enqueues_job(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    client, db_path, media_id = _admin_client_with_movie(tmp_path)
    r = client.post(f"/admin/asr/{media_id}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/jobs"
    job = db.connect(db_path).execute(
        "SELECT kind, status FROM jobs WHERE media_id = ?", (media_id,)
    ).fetchone()
    assert (job["kind"], job["status"]) == ("asr", "queued")


# --- watch-page admin actions ---------------------------------------


def test_watch_page_shows_generate_button_for_admin_when_no_subtitle(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    client, _, media_id = _admin_client_with_movie(tmp_path)
    r = client.get(f"/watch/{media_id}")
    assert r.status_code == 200
    assert "Generate subtitles via ASR" in r.text
    # Watch page now uses a JS-driven button + polling, not a form redirect.
    assert "id='gen-subs'" in r.text
    assert f"/api/admin/asr/{media_id}" in r.text  # POST URL embedded in the inline JS
    assert "/api/admin/jobs/" in r.text  # polling URL embedded in the inline JS


def test_watch_page_hides_generate_button_when_subtitle_present(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path / "lib"; root.mkdir()
    _make_video(root / "Movie.mp4")
    (root / "Movie.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('lib', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    r = client.get(f"/watch/{media_id}")
    assert r.status_code == 200
    assert "Generate subtitles via ASR" not in r.text
    # And the <track> tag IS present
    assert "<track" in r.text


def test_watch_page_no_generate_button_for_anonymous(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    # Create admin (so /register isn't open) then visit watch as anonymous
    client, db_path, media_id = _admin_client_with_movie(tmp_path)
    client.post("/api/auth/logout")
    r = client.get(f"/watch/{media_id}")
    assert r.status_code == 200
    assert "Generate subtitles via ASR" not in r.text


# --- Single-job API endpoint ----------------------------------------


def test_api_get_single_job(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    client, db_path, media_id = _admin_client_with_movie(tmp_path)
    r = client.post(f"/api/admin/asr/{media_id}")
    job_id = r.json()["job_id"]

    r = client.get(f"/api/admin/jobs/{job_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == job_id
    assert body["kind"] == "asr"
    assert body["media_id"] == media_id
    assert body["status"] == "queued"
    assert body["error"] is None


def test_api_get_single_job_404(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    client, _, _ = _admin_client_with_movie(tmp_path)
    assert client.get("/api/admin/jobs/9999").status_code == 404


def test_api_get_single_job_requires_admin(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    client, _, media_id = _admin_client_with_movie(tmp_path)
    client.post(f"/api/admin/asr/{media_id}")
    client.post("/api/auth/logout")
    assert client.get("/api/admin/jobs/1").status_code == 401
