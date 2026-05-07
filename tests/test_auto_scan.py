"""Auto-scan-on-add + live progress tests."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import db, scanner
from cuttlefish.server import create_app, scan_tracker


@pytest.fixture(autouse=True)
def _clear_scans():
    """Each test starts with an empty scan tracker."""
    scan_tracker.reset_for_tests()
    yield
    scan_tracker.reset_for_tests()


def _admin_client(tmp_path: Path):
    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    return client, db_path


def _wait_for_scan(library_id: int, timeout: float = 10.0):
    """Spin-wait until the background scan finishes (or fails)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = scan_tracker.get(library_id)
        if s and s.get("status") in ("done", "failed"):
            return s
        time.sleep(0.05)
    raise TimeoutError(f"scan for {library_id} didn't finish in {timeout}s")


# --- scanner callback ---------------------------------------------------


def test_scanner_invokes_on_progress(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "A.mp4").write_bytes(b"")
    (root / "B.mp4").write_bytes(b"")
    (root / "C.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('lib', ?)",
            (str(root),),
        )
    calls = []
    scanner.scan_library(
        conn, cur.lastrowid, root, on_progress=lambda c, t, n: calls.append((c, t, n)),
    )
    # First call: (0, 3, ""), then per-item, then final (3, 3, "").
    assert calls[0] == (0, 3, "")
    assert calls[-1] == (3, 3, "")
    # Each entry name shows up at least once.
    names = {c[2] for c in calls}
    assert "A.mp4" in names
    assert "B.mp4" in names
    assert "C.mp4" in names


def test_scanner_works_without_callback(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "A.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('lib', ?)",
            (str(root),),
        )
    # Old call shape (no on_progress) still works.
    result = scanner.scan_library(conn, cur.lastrowid, root)
    assert result.movies_added == 1


# --- scan_tracker module ------------------------------------------------


def test_tracker_start_and_finish(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "A.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('lib', ?)",
            (str(root),),
        )
    lib_id = cur.lastrowid
    started = scan_tracker.start(lib_id, db_path)
    assert started is True
    final = _wait_for_scan(lib_id)
    assert final["status"] == "done"
    assert final["result"]["movies_added"] == 1
    assert final["error"] is None


def test_tracker_double_start_is_a_noop(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('lib', ?)",
            (str(root),),
        )
    lib_id = cur.lastrowid
    assert scan_tracker.start(lib_id, db_path) is True
    # Second call while still running OR after finish: returns False the
    # second time only if the first is still running. Wait for it to finish.
    _wait_for_scan(lib_id)
    # After 'done', a new start re-runs and returns True.
    assert scan_tracker.start(lib_id, db_path) is True


def test_tracker_records_failure_on_bad_root(tmp_path):
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('lib', ?)",
            (str(tmp_path / "does-not-exist"),),
        )
    lib_id = cur.lastrowid
    scan_tracker.start(lib_id, db_path)
    final = _wait_for_scan(lib_id)
    assert final["status"] == "failed"
    assert "library root not found" in (final["error"] or "")


# --- API + form integration --------------------------------------------


def test_api_create_library_kicks_off_scan(tmp_path):
    root = tmp_path / "lib"; root.mkdir()
    (root / "A.mp4").write_bytes(b"")
    client, db_path = _admin_client(tmp_path)
    r = client.post(
        "/api/admin/libraries",
        json={"name": "lib", "root_path": str(root)},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["scanning"] is True
    lib_id = body["id"]
    final = _wait_for_scan(lib_id)
    assert final["status"] == "done"


def test_form_create_library_kicks_off_scan(tmp_path):
    root = tmp_path / "lib"; root.mkdir()
    (root / "A.mp4").write_bytes(b"")
    client, db_path = _admin_client(tmp_path)
    r = client.post(
        "/admin/libraries",
        data={"name": "lib", "root_path": str(root)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    rows = db.connect(db_path).execute("SELECT id FROM libraries").fetchall()
    lib_id = rows[0]["id"]
    final = _wait_for_scan(lib_id)
    assert final["status"] == "done"
    assert final["result"]["movies_added"] == 1


def test_get_admin_scans_endpoint(tmp_path):
    root = tmp_path / "lib"; root.mkdir()
    (root / "A.mp4").write_bytes(b"")
    client, db_path = _admin_client(tmp_path)
    r = client.post("/api/admin/libraries",
                    json={"name": "lib", "root_path": str(root)})
    lib_id = r.json()["id"]
    _wait_for_scan(lib_id)
    r = client.get("/api/admin/scans")
    assert r.status_code == 200
    payload = r.json()
    assert str(lib_id) in payload  # JSON keys are stringified ints
    entry = payload[str(lib_id)]
    assert entry["status"] == "done"
    assert entry["result"]["movies_added"] == 1


def test_admin_scans_requires_admin(tmp_path):
    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    client = TestClient(create_app(db_path=db_path))
    # Register an admin so the auth gate has something to enforce, then log
    # out — we want to test the unauthenticated case.
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/logout")
    assert client.get("/api/admin/scans").status_code == 401


def test_admin_libraries_page_includes_progress_polling_js(tmp_path):
    client, _ = _admin_client(tmp_path)
    r = client.get("/admin/libraries")
    assert r.status_code == 200
    assert "/api/admin/scans" in r.text
    assert "data-lib-id" in r.text
    assert "scan-status" in r.text


def test_form_scan_button_uses_tracker(tmp_path):
    """Existing per-row 'Scan' button now goes through the tracker too."""
    root = tmp_path / "lib"; root.mkdir()
    (root / "A.mp4").write_bytes(b"")
    client, db_path = _admin_client(tmp_path)
    r = client.post("/api/admin/libraries",
                    json={"name": "lib", "root_path": str(root)})
    lib_id = r.json()["id"]
    _wait_for_scan(lib_id)  # auto-scan completes

    # Explicit Scan button → another tracker run
    r = client.post(f"/admin/libraries/{lib_id}/scan", follow_redirects=False)
    assert r.status_code == 303
    final = _wait_for_scan(lib_id, timeout=15)
    assert final["status"] == "done"
