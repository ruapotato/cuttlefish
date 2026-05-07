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
    # 'available' depends on whether nemo is importable in the test env
    # (it usually is once asr moved to base deps, but CI cache state can
    # vary). Assert shape only.
    assert isinstance(body.get("available"), bool)
    # We never started a worker in this test, so this is always False.
    assert body["worker_in_process"] is False
    # No jobs enqueued.
    assert body["queued"] == 0


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
    assert "ASR worker isn't running" in r.text


def test_admin_subtitles_green_state_when_worker_running(tmp_path, monkeypatch):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    client, _, _ = _admin_client_with_movie(tmp_path)
    monkeypatch.setattr(asr, "is_available", lambda: True)
    asr._worker_in_process = True
    try:
        r = client.get("/admin/subtitles")
        assert r.status_code == 200
        assert "ASR worker is running" in r.text
    finally:
        asr._worker_in_process = False  # reset for other tests


def test_asr_library_status_aggregates_per_library(tmp_path):
    """The page polls one endpoint that returns per-library {queued, running,
    done, failed} counts so a refresh during a long bulk run shows the same
    live state the JS would have driven to."""
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    from fastapi.testclient import TestClient
    from cuttlefish.server import create_app

    movies_root = tmp_path / "movies"; movies_root.mkdir()
    _make_video(movies_root / "A.mp4")
    _make_video(movies_root / "B.mp4")
    other_root = tmp_path / "other"; other_root.mkdir()
    _make_video(other_root / "C.mp4")

    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur1 = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('movies', ?)",
            (str(movies_root),),
        )
        lib_movies = cur1.lastrowid
        cur2 = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('other', ?)",
            (str(other_root),),
        )
        lib_other = cur2.lastrowid
    scanner.scan_library(conn, lib_movies, movies_root)
    scanner.scan_library(conn, lib_other, other_root)
    movie_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM media WHERE library_id = ?", (lib_movies,)
    ).fetchall()]
    other_id = conn.execute(
        "SELECT id FROM media WHERE library_id = ?", (lib_other,)
    ).fetchone()["id"]

    with conn:
        # movies: 1 done, 1 queued
        conn.execute(
            "INSERT INTO jobs (kind, media_id, status) VALUES ('asr', ?, 'done')",
            (movie_ids[0],),
        )
        conn.execute(
            "INSERT INTO jobs (kind, media_id) VALUES ('asr', ?)",
            (movie_ids[1],),
        )
        # other: 1 running
        conn.execute(
            "INSERT INTO jobs (kind, media_id, status) VALUES ('asr', ?, 'running')",
            (other_id,),
        )

    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    r = client.get("/api/admin/asr/library-status")
    assert r.status_code == 200
    body = r.json()
    libs = {l["id"]: l for l in body["libraries"]}
    assert libs[lib_movies]["queued"] == 1
    assert libs[lib_movies]["done"] == 1
    assert libs[lib_movies]["running"] == 0
    assert libs[lib_other]["running"] == 1
    assert libs[lib_other]["queued"] == 0


def test_asr_library_status_shows_currently_transcribing_title(tmp_path):
    """When a job is in 'running' state the response identifies what file is
    being transcribed so the page can show 'Worker is transcribing: …'.
    This is the bit that prevents the user from thinking the system is
    broken when the worker is busy on a different library's older job."""
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    from fastapi.testclient import TestClient
    from cuttlefish.server import create_app

    root = tmp_path / "media"; root.mkdir()
    _make_video(root / "Tiny.mp4")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('lib', ?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root)
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    with conn:
        conn.execute(
            "INSERT INTO jobs (kind, media_id, status) VALUES ('asr', ?, 'running')",
            (media_id,),
        )

    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    r = client.get("/api/admin/asr/library-status")
    assert r.status_code == 200
    body = r.json()
    assert body["current"] is not None
    assert body["current"]["library_id"] == lib_id
    assert body["current"]["library_name"] == "lib"
    # The title should be readable — at least non-empty and matching the
    # scanned title for the file we created.
    assert body["current"]["title"]


def test_asr_library_status_idle_when_nothing_running(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    from fastapi.testclient import TestClient
    from cuttlefish.server import create_app
    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    r = client.get("/api/admin/asr/library-status")
    assert r.status_code == 200
    body = r.json()
    assert body["current"] is None


def test_admin_subtitles_page_renders_running_indicator_for_refresh(tmp_path):
    """A page refresh during a bulk run should show the same live worker
    indicator the JS would have rendered. This is what keeps the user from
    thinking the system is hung when the page reloads mid-job."""
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    client, db_path, media_id = _admin_client_with_movie(tmp_path)
    conn = db.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO jobs (kind, media_id, status) VALUES ('asr', ?, 'running')",
            (media_id,),
        )
    r = client.get("/admin/subtitles")
    assert r.status_code == 200
    # The rendered worker div should be in 'running' state (not the JS-only
    # idle text that lives inside the script tag).
    assert "<div id='asr-worker' class='asr-worker running'>" in r.text
    assert "Worker is transcribing:" in r.text


def test_admin_subtitles_page_renders_idle_indicator_when_no_jobs(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    client, _, _ = _admin_client_with_movie(tmp_path)
    r = client.get("/admin/subtitles")
    assert r.status_code == 200
    assert "<div id='asr-worker' class='asr-worker idle'>" in r.text


def test_bulk_asr_for_library_queues_everything_without_existing_asr(tmp_path):
    """Queue ASR for every movie + every episode in a library that doesn't
    already have a <stem>.asr.srt sitting next to it."""
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    from fastapi.testclient import TestClient
    from cuttlefish.server import create_app

    root = tmp_path / "media"
    root.mkdir()
    # 1 loose movie
    _make_video(root / "Movie A.mp4")
    # 1 movie that already has an .asr.srt → should be SKIPPED
    _make_video(root / "Movie B.mp4")
    (root / "Movie B.asr.srt").write_text("WEBVTT\n")
    # 1 TV show with 2 episodes (one already has ASR)
    s1 = root / "Show" / "Season 01"
    _make_video(s1 / "Show - S01E01.mp4")
    _make_video(s1 / "Show - S01E02.mp4")
    (s1 / "Show - S01E02.asr.srt").write_text("WEBVTT\n")

    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root)

    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})

    r = client.post(f"/api/admin/asr/library/{lib_id}")
    assert r.status_code == 200
    body = r.json()
    # Movie A (no ASR) + S01E01 (no ASR) = 2; Movie B + S01E02 already have ASR
    assert body["queued"] == 2
    # Confirm the actual jobs table.
    queued = conn.execute(
        "SELECT COUNT(*) AS c FROM jobs WHERE kind = 'asr' AND status = 'queued'"
    ).fetchone()["c"]
    assert queued == 2


def test_bulk_asr_form_redirects_with_count(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    from fastapi.testclient import TestClient
    from cuttlefish.server import create_app

    root = tmp_path / "media"; root.mkdir()
    _make_video(root / "Movie.mp4")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root)
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    r = client.post(f"/admin/asr/library/{lib_id}", follow_redirects=False)
    assert r.status_code == 303
    assert "/admin/subtitles" in r.headers["location"]
    assert "queued=1" in r.headers["location"]


def test_bulk_asr_unknown_library_404(tmp_path):
    """Bulk enqueue against a library that doesn't exist returns 404."""
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    from fastapi.testclient import TestClient
    from cuttlefish.server import create_app

    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    r = client.post("/api/admin/asr/library/9999")
    assert r.status_code == 404


def test_admin_subtitles_page_has_bulk_section(tmp_path):
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    client, _, _ = _admin_client_with_movie(tmp_path)
    r = client.get("/admin/subtitles")
    assert r.status_code == 200
    assert "Bulk: whole library" in r.text
    assert "Generate ASR for everything in this library" in r.text
    # JS-driven button + status span (not a form-redirect)
    assert "class='bulk-asr-btn'" in r.text
    assert "class='bulk-asr-status'" in r.text
    assert "data-lib-id=" in r.text
    # The page is its own dashboard: it polls a single per-library status
    # endpoint and re-renders the cells. Refresh = same view.
    assert "/api/admin/asr/library-status" in r.text
    # Per-library count cells are server-rendered so a refresh shows the
    # live state immediately without waiting for the first JS poll.
    assert "asr-queued" in r.text
    assert "asr-running" in r.text
    assert "asr-done" in r.text
    assert "asr-failed" in r.text
    # Worker-activity indicator is always present (idle or running variant).
    assert "id='asr-worker'" in r.text or 'id="asr-worker"' in r.text


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
    # ASR writes to <stem>.asr.srt so it sits alongside any original
    # sidecar instead of clobbering it.
    assert srt.name == "Tiny.asr.srt"
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
    assert srt.name == "S01E01.asr.srt"
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
    # Watch page uses a JS-driven button + polling. The button advertises
    # its target URL via data-asr-url so the inline JS doesn't need any
    # Python interpolation (which previously emitted invalid braces and
    # broke the click handler entirely).
    assert "id='gen-subs'" in r.text
    assert f"data-asr-url='/api/admin/asr/{media_id}'" in r.text
    assert "/api/admin/jobs/" in r.text  # polling URL embedded in the inline JS
    # Make sure the old broken pattern hasn't crept back in: an f-string
    # with mismatched braces used to render '}}).then' in the rendered JS.
    assert "}}).then" not in r.text


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
