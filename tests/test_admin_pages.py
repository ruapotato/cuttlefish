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
            "INSERT INTO libraries (name, kind, root_path) VALUES (?, ?, ?)",
            ("movies", "movies", str(root)),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root, "movies")
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
    assert "Tiny" not in r.text
    assert "already encoded" in r.text


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
    assert "<a href='/admin'>Admin</a>" not in r.text
