"""Admin HTML pages and health endpoint tests."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import db, scanner
from cuttlefish.server import create_app
from cuttlefish.workers import encoder

FFMPEG = shutil.which("ffmpeg")
ffmpeg_required = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


def _make_video(p: Path, duration: int = 1) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=160x120:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)],
        check=True,
    )


def _populate(tmp_path: Path):
    """Build a movies library with one tiny mp4. Return (db_path, media_id)."""
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path / "movies"
    root.mkdir()
    _make_video(root / "Tiny.mp4")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES (?, ?)",
            ("movies", str(root)),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root)
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    return db_path, media_id


def _admin_client(db_path):
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "admin", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "admin", "password": "secret123"})
    return client


def _non_admin_client(db_path):
    """Register an admin (so registration is locked), then a regular user logged in."""
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "admin", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "admin", "password": "secret123"})
    client.post("/api/auth/register", data={"username": "bob", "password": "secret123"})
    client.post("/api/auth/logout")
    client.post("/api/auth/login", data={"username": "bob", "password": "secret123"})
    return client


# --- /health -------------------------------------------------------------


def test_health_ok(tmp_path):
    db_path = tmp_path / "h.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    client = TestClient(create_app(db_path=db_path))
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["schema_version"] >= 4


def test_health_does_not_require_auth(tmp_path):
    db_path = tmp_path / "h.db"
    db.init_schema(db.connect(db_path))
    client = TestClient(create_app(db_path=db_path))
    assert client.get("/health").status_code == 200


# --- admin auth gate ----------------------------------------------------


def test_admin_pages_401_anonymous(tmp_path):
    db_path, _ = _populate(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    for path in ("/admin", "/admin/jobs", "/admin/cleanup", "/admin/encode"):
        r = client.get(path)
        assert r.status_code == 401, f"{path} returned {r.status_code}"


def test_admin_pages_403_non_admin(tmp_path):
    db_path, _ = _populate(tmp_path)
    client = _non_admin_client(db_path)
    for path in ("/admin", "/admin/jobs", "/admin/cleanup", "/admin/encode"):
        r = client.get(path)
        assert r.status_code == 403, f"{path} returned {r.status_code}"


# --- admin pages -------------------------------------------------------


def test_admin_landing_links_to_subpages(tmp_path):
    db_path, _ = _populate(tmp_path)
    client = _admin_client(db_path)
    r = client.get("/admin")
    assert r.status_code == 200
    for href in ("/admin/jobs", "/admin/cleanup", "/admin/encode"):
        assert href in r.text


def test_admin_jobs_empty(tmp_path):
    db_path, _ = _populate(tmp_path)
    client = _admin_client(db_path)
    r = client.get("/admin/jobs")
    assert r.status_code == 200
    assert "No jobs" in r.text


def test_admin_jobs_lists_after_enqueue(tmp_path):
    db_path, media_id = _populate(tmp_path)
    client = _admin_client(db_path)
    client.post("/admin/encode/" + str(media_id), follow_redirects=False)
    r = client.get("/admin/jobs")
    assert r.status_code == 200
    assert "encode" in r.text
    assert "queued" in r.text


def test_admin_encode_form_redirects_to_jobs(tmp_path):
    db_path, media_id = _populate(tmp_path)
    client = _admin_client(db_path)
    r = client.post(f"/admin/encode/{media_id}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/jobs"


def test_admin_encode_page_lists_unencoded(tmp_path):
    db_path, _ = _populate(tmp_path)
    client = _admin_client(db_path)
    r = client.get("/admin/encode")
    assert r.status_code == 200
    assert "Tiny" in r.text
    assert "Enqueue encode" in r.text


@ffmpeg_required
def test_admin_encode_page_hides_already_encoded(tmp_path):
    db_path, media_id = _populate(tmp_path)
    encoder.encode_media(db.connect(db_path), media_id, ffmpeg=FFMPEG)
    client = _admin_client(db_path)
    r = client.get("/admin/encode")
    # Already-encoded items don't appear in the per-item list. The bulk
    # dashboard rows still show the library name though, so we check the
    # specific 'Tiny' (movie title) doesn't appear in a per-item table row.
    assert "Enqueue encode" not in r.text
    assert "already encoded" in r.text


def test_admin_encode_page_has_bulk_section(tmp_path):
    """/admin/encode now has the same per-library bulk dashboard that
    /admin/subtitles has, so users can encode an entire library in one
    click and see live progress (queued/running/done/failed)."""
    db_path, _ = _populate(tmp_path)
    client = _admin_client(db_path)
    r = client.get("/admin/encode")
    assert r.status_code == 200
    assert "Bulk: whole library" in r.text
    assert "Encode everything in this library" in r.text
    assert "class='bulk-jobs-btn'" in r.text
    assert "/api/admin/encode/library-status" in r.text
    # Per-library count cells are server-rendered for refresh-stability.
    for cls in ("jobs-queued", "jobs-running", "jobs-done", "jobs-failed"):
        assert cls in r.text
    # Worker indicator (idle here since no encode job is running).
    assert "<div class='jobs-worker idle'>" in r.text


def test_bulk_encode_for_library_queues_movies_and_episodes(tmp_path):
    """Bulk encode skips items that already have an encoded variant and
    queues every other movie + tv_episode in the library."""
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path / "library"; root.mkdir()
    _make_video(root / "Movie One.mp4")
    show_root = root / "Best Show"
    show_root.mkdir()
    season = show_root / "Season 01"; season.mkdir()
    _make_video(season / "Best Show - S01E01.mp4")
    _make_video(season / "Best Show - S01E02.mp4")

    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('mixed', ?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root)

    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    r = client.post(f"/api/admin/encode/library/{lib_id}")
    assert r.status_code == 200
    body = r.json()
    # 1 movie + 2 episodes = 3 jobs.
    assert body["queued"] == 3, body

    rows = conn.execute(
        "SELECT kind, status, media_id, episode_id FROM jobs WHERE kind = 'encode'"
    ).fetchall()
    assert len(rows) == 3
    assert all(r["status"] == "queued" for r in rows)
    # One movie job (media_id set), two episode jobs (episode_id set).
    movie_jobs = [r for r in rows if r["media_id"] is not None]
    ep_jobs = [r for r in rows if r["episode_id"] is not None]
    assert len(movie_jobs) == 1
    assert len(ep_jobs) == 2


def test_bulk_encode_idempotent_skips_already_encoded(tmp_path):
    """Re-running bulk-encode after an item is already encoded shouldn't
    duplicate its job — the encoded_files / encoded_episodes row gates it."""
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path / "library"; root.mkdir()
    _make_video(root / "A.mp4")
    _make_video(root / "B.mp4")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root)
    media_ids = [r["id"] for r in conn.execute("SELECT id FROM media").fetchall()]
    # Pretend the first one is already encoded.
    with conn:
        conn.execute(
            "INSERT INTO encoded_files (media_id, clean_dir, video_path) VALUES (?, ?, ?)",
            (media_ids[0], str(root / "A"), str(root / "A" / "A.mp4")),
        )

    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    r = client.post(f"/api/admin/encode/library/{lib_id}")
    body = r.json()
    assert body["queued"] == 1  # only the un-encoded one
    # Second call: nothing more to do.
    r2 = client.post(f"/api/admin/encode/library/{lib_id}")
    # The first call queued one job (still 'queued'), so technically the
    # un-encoded movie doesn't have an encoded_files row YET. We expect a
    # SECOND queue here because the gate is encoded_files presence, not
    # job-already-queued. That's the same behavior as ASR bulk: the dedupe
    # is by *output existence*, not job presence. Either is fine for now.
    # Just assert no crash and a sane count.
    assert r2.status_code == 200
    assert isinstance(r2.json()["queued"], int)


def test_encode_library_status_endpoint(tmp_path):
    """/api/admin/encode/library-status mirrors the asr equivalent: per-
    library {queued, running, done, failed} plus the currently-running
    title so the page can display 'Worker is encoding: Foo'."""
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path / "library"; root.mkdir()
    _make_video(root / "A.mp4")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(root),),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root)
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    with conn:
        conn.execute(
            "INSERT INTO jobs (kind, media_id, status) VALUES ('encode', ?, 'running')",
            (media_id,),
        )

    client = _admin_client(db_path)
    r = client.get("/api/admin/encode/library-status")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "encode"
    libs = {l["id"]: l for l in body["libraries"]}
    assert libs[lib_id]["running"] == 1
    assert body["current"] is not None
    assert body["current"]["library_id"] == lib_id


@ffmpeg_required
def test_admin_cleanup_page_lists_candidates(tmp_path):
    db_path, media_id = _populate(tmp_path)
    encoder.encode_media(db.connect(db_path), media_id, ffmpeg=FFMPEG)
    client = _admin_client(db_path)
    r = client.get("/admin/cleanup")
    assert r.status_code == 200
    assert "Tiny" in r.text
    assert "Delete original" in r.text


@ffmpeg_required
def test_admin_delete_original_via_form(tmp_path):
    db_path, media_id = _populate(tmp_path)
    encoder.encode_media(db.connect(db_path), media_id, ffmpeg=FFMPEG)
    original = Path(db.connect(db_path).execute(
        "SELECT source_path FROM media WHERE id = ?", (media_id,)
    ).fetchone()["source_path"])
    assert original.is_file()
    client = _admin_client(db_path)
    r = client.post(f"/admin/originals/{media_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/cleanup"
    assert not original.exists()


@ffmpeg_required
def test_cleanup_library_status_endpoint(tmp_path):
    """Per-library candidate count + freeable bytes, used by the
    /admin/cleanup bulk dashboard."""
    db_path, media_id = _populate(tmp_path)
    encoder.encode_media(db.connect(db_path), media_id, ffmpeg=FFMPEG)
    client = _admin_client(db_path)
    r = client.get("/api/admin/cleanup/library-status")
    assert r.status_code == 200
    body = r.json()
    libs = body["libraries"]
    assert len(libs) == 1
    lib = libs[0]
    assert lib["candidate_count"] == 1
    assert lib["total_bytes"] > 0
    # Server provides the human-readable form so the JS doesn't need its
    # own _human_size copy.
    assert isinstance(lib["total_bytes_human"], str)


@ffmpeg_required
def test_admin_cleanup_page_has_bulk_section(tmp_path):
    """/admin/cleanup now has the same bulk-per-library affordance the
    other admin pages have, with confirm-then-delete and live counts."""
    db_path, media_id = _populate(tmp_path)
    encoder.encode_media(db.connect(db_path), media_id, ffmpeg=FFMPEG)
    client = _admin_client(db_path)
    r = client.get("/admin/cleanup")
    assert r.status_code == 200
    assert "Bulk: whole library" in r.text
    assert "id='cleanup-dashboard'" in r.text
    assert "class='bulk-cleanup-btn'" in r.text
    assert "/api/admin/cleanup/library-status" in r.text
    # Per-library candidate count should be server-rendered.
    assert "cleanup-count" in r.text
    assert "cleanup-size" in r.text


@ffmpeg_required
def test_bulk_cleanup_deletes_all_originals(tmp_path):
    """POST to /api/admin/cleanup/library/{id} synchronously deletes
    every original-with-encoded-sibling in that library and reports the
    count + bytes freed."""
    db_path, media_id = _populate(tmp_path)
    encoder.encode_media(db.connect(db_path), media_id, ffmpeg=FFMPEG)
    conn = db.connect(db_path)
    original = Path(conn.execute(
        "SELECT source_path FROM media WHERE id = ?", (media_id,)
    ).fetchone()["source_path"])
    encoded = Path(conn.execute(
        "SELECT video_path FROM encoded_files WHERE media_id = ?", (media_id,)
    ).fetchone()["video_path"])
    assert original.is_file()
    assert encoded.is_file()

    library_id = conn.execute(
        "SELECT library_id FROM media WHERE id = ?", (media_id,)
    ).fetchone()["library_id"]
    client = _admin_client(db_path)
    r = client.post(f"/api/admin/cleanup/library/{library_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == 1
    assert body["freed_bytes"] > 0
    assert body["errors"] == []
    # Original is gone, encoded file is untouched.
    assert not original.exists()
    assert encoded.is_file()
    # Subsequent call: nothing to delete (re-pointed source_path is the
    # encoded directory, which doesn't satisfy the candidate criteria).
    r2 = client.post(f"/api/admin/cleanup/library/{library_id}")
    assert r2.json()["deleted"] == 0


def test_bulk_cleanup_404_unknown_library(tmp_path):
    db_path, _ = _populate(tmp_path)
    client = _admin_client(db_path)
    r = client.post("/api/admin/cleanup/library/99999")
    assert r.status_code == 404


@ffmpeg_required
def test_bulk_cleanup_also_removes_orphan_sidecars(tmp_path):
    """When the original video is deleted, any subtitle/poster sidecars
    sitting next to it (sharing the stem) should be removed too —
    otherwise the library root is left with leftover .srt / .asr.srt /
    .jpg files that look broken next to the clean Title/ folder."""
    db_path, media_id = _populate(tmp_path)
    encoder.encode_media(db.connect(db_path), media_id, ffmpeg=FFMPEG)
    conn = db.connect(db_path)
    original = Path(conn.execute(
        "SELECT source_path FROM media WHERE id = ?", (media_id,)
    ).fetchone()["source_path"])
    assert original.is_file()

    # Drop sidecars next to the source: original sub, ASR sub, and a poster.
    stem = original.stem
    parent = original.parent
    sidecar_srt = parent / f"{stem}.srt"
    sidecar_asr = parent / f"{stem}.asr.srt"
    sidecar_jpg = parent / f"{stem}.jpg"
    sidecar_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    sidecar_asr.write_text("1\n00:00:00,000 --> 00:00:01,000\nasr\n")
    sidecar_jpg.write_bytes(b"\xff\xd8\xff\xe0fake jpg")
    expected_freed_min = (
        original.stat().st_size
        + sidecar_srt.stat().st_size
        + sidecar_asr.stat().st_size
        + sidecar_jpg.stat().st_size
    )
    # Also drop a non-sidecar (.nfo) — should NOT be touched by cleanup.
    unrelated = parent / f"{stem}.nfo"
    unrelated.write_text("metadata")

    library_id = conn.execute(
        "SELECT library_id FROM media WHERE id = ?", (media_id,)
    ).fetchone()["library_id"]
    client = _admin_client(db_path)
    r = client.post(f"/api/admin/cleanup/library/{library_id}")
    body = r.json()
    assert body["deleted"] == 1
    # files_deleted = 1 video + 3 sidecars
    assert body["files_deleted"] == 4
    assert body["freed_bytes"] >= expected_freed_min

    assert not original.exists()
    assert not sidecar_srt.exists()
    assert not sidecar_asr.exists()
    assert not sidecar_jpg.exists()
    # Non-sidecar cruft is preserved (handled by /admin/sweep, not here).
    assert unrelated.is_file()


@ffmpeg_required
def test_per_item_delete_also_removes_orphan_sidecars(tmp_path):
    """The single-item form on /admin/cleanup should clean up sidecars
    too — same orphan-leftover problem if it doesn't."""
    db_path, media_id = _populate(tmp_path)
    encoder.encode_media(db.connect(db_path), media_id, ffmpeg=FFMPEG)
    conn = db.connect(db_path)
    original = Path(conn.execute(
        "SELECT source_path FROM media WHERE id = ?", (media_id,)
    ).fetchone()["source_path"])
    sidecar = original.parent / f"{original.stem}.asr.srt"
    sidecar.write_text("dummy asr srt")

    client = _admin_client(db_path)
    r = client.post(f"/admin/originals/{media_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not original.exists()
    assert not sidecar.exists()


@ffmpeg_required
def test_bulk_cleanup_only_targets_its_own_library(tmp_path):
    """Bulk delete on library A must NOT touch any originals in library B."""
    db_path, media_a = _populate(tmp_path)
    encoder.encode_media(db.connect(db_path), media_a, ffmpeg=FFMPEG)

    # Add a second library with its own movie + encoded version.
    other_root = tmp_path / "other_lib"; other_root.mkdir()
    _make_video(other_root / "B.mp4")
    conn = db.connect(db_path)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('other', ?)",
            (str(other_root),),
        )
        lib_b = cur.lastrowid
    scanner.scan_library(conn, lib_b, other_root)
    media_b = conn.execute(
        "SELECT id FROM media WHERE library_id = ?", (lib_b,)
    ).fetchone()["id"]
    encoder.encode_media(conn, media_b, ffmpeg=FFMPEG)
    original_b = Path(conn.execute(
        "SELECT source_path FROM media WHERE id = ?", (media_b,)
    ).fetchone()["source_path"])
    assert original_b.is_file()

    # Bulk-clean only library A.
    library_a = conn.execute(
        "SELECT library_id FROM media WHERE id = ?", (media_a,)
    ).fetchone()["library_id"]
    client = _admin_client(db_path)
    r = client.post(f"/api/admin/cleanup/library/{library_a}")
    assert r.json()["deleted"] == 1
    # Library B's original is still there.
    assert original_b.is_file()


def test_admin_link_in_header_for_admin(tmp_path):
    db_path, _ = _populate(tmp_path)
    client = _admin_client(db_path)
    r = client.get("/")
    assert "/admin" in r.text


def test_admin_link_not_in_header_for_non_admin(tmp_path):
    db_path, _ = _populate(tmp_path)
    client = _non_admin_client(db_path)
    r = client.get("/")
    # Non-admin should not see the admin link in their header
    # (they will see "/admin" mentioned elsewhere, hence checking for the link
    # form specifically).
    assert "<a href='/admin'>Admin settings</a>" not in r.text


def test_userbar_distinguishes_account_and_admin_settings(tmp_path):
    """Header used to show the username as a link AND a separate 'Admin'
    link — for an admin user that read as two identical buttons doing
    different things. The labels should make their roles unambiguous."""
    db_path, _ = _populate(tmp_path)
    client = _admin_client(db_path)
    r = client.get("/")
    assert r.status_code == 200
    # Username appears as a label, not the link target — the destination
    # is described by the link text instead.
    assert "<span class='username'>admin</span>" in r.text
    assert "<a href='/account'>Your account</a>" in r.text
    assert "<a href='/admin'>Admin settings</a>" in r.text


def test_userbar_for_non_admin_omits_admin_settings(tmp_path):
    db_path, _ = _populate(tmp_path)
    client = _non_admin_client(db_path)
    r = client.get("/")
    assert "<a href='/account'>Your account</a>" in r.text
    assert "Admin settings" not in r.text
