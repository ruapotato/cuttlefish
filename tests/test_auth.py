"""Auth + progress endpoint tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import auth, db, scanner
from cuttlefish.server import create_app


@pytest.fixture
def populated_db(tmp_path: Path):
    root = tmp_path / "movies"
    root.mkdir()
    (root / "alpha.mp4").write_bytes(b"A" * 100)
    (root / "beta.mp4").write_bytes(b"B" * 100)
    db_path = tmp_path / "test.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES (?, ?)",
            ("movies", str(root)),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root)
    return db_path


@pytest.fixture
def client(populated_db):
    return TestClient(create_app(db_path=populated_db))


# --- password hashing -----------------------------------------------------


def test_hash_and_verify_password_roundtrip():
    pw = "correct horse battery staple"
    h = auth.hash_password(pw)
    assert auth.verify_password(pw, h) is True
    assert auth.verify_password("wrong", h) is False


def test_verify_password_rejects_garbage():
    assert auth.verify_password("anything", "not-a-real-hash") is False
    assert auth.verify_password("anything", "scrypt$$$$$") is False


def test_authenticate_returns_none_for_unknown_user(populated_db):
    conn = db.connect(populated_db)
    assert auth.authenticate(conn, "ghost", "pw") is None


# --- registration / login flow -------------------------------------------


def test_first_user_becomes_admin(client):
    r = client.post("/api/auth/register", data={"username": "alice", "password": "secret123"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "is_admin": True}


def test_second_registration_requires_admin(client):
    # First user
    client.post("/api/auth/register", data={"username": "alice", "password": "secret123"})
    # Anonymous second registration should be rejected
    r = client.post("/api/auth/register", data={"username": "bob", "password": "secret123"})
    assert r.status_code == 403


def test_admin_can_register_others(client):
    client.post("/api/auth/register", data={"username": "alice", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "alice", "password": "secret123"})
    r = client.post("/api/auth/register", data={"username": "bob", "password": "secret123"})
    assert r.status_code == 200
    assert r.json()["is_admin"] is False


def test_login_sets_cookie_and_me_works(client):
    client.post("/api/auth/register", data={"username": "alice", "password": "secret123"})
    r = client.post("/api/auth/login", data={"username": "alice", "password": "secret123"})
    assert r.status_code == 200
    assert "cuttlefish_session" in r.cookies or any(
        "cuttlefish_session" in c for c in client.cookies.keys()
    )
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["username"] == "alice"
    assert me.json()["is_admin"] is True


def test_me_unauthenticated_401(client):
    assert client.get("/api/me").status_code == 401


def test_login_bad_password(client):
    client.post("/api/auth/register", data={"username": "alice", "password": "secret123"})
    r = client.post("/api/auth/login", data={"username": "alice", "password": "WRONG"})
    assert r.status_code == 401


def test_logout_clears_session(client):
    client.post("/api/auth/register", data={"username": "alice", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "alice", "password": "secret123"})
    assert client.get("/api/me").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/me").status_code == 401


def test_short_password_rejected(client):
    r = client.post("/api/auth/register", data={"username": "alice", "password": "abc"})
    assert r.status_code == 400


# --- progress -----------------------------------------------------------


def test_progress_requires_login(client):
    assert client.get("/api/progress/1").status_code == 401
    assert client.put("/api/progress/1", json={"position_seconds": 12.0}).status_code == 401


def test_progress_roundtrip(client):
    client.post("/api/auth/register", data={"username": "alice", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "alice", "password": "secret123"})
    # Initially 0
    r = client.get("/api/progress/1")
    assert r.status_code == 200
    assert r.json()["position_seconds"] == 0.0
    # Save
    r = client.put("/api/progress/1", json={"position_seconds": 42.5, "duration_seconds": 600.0})
    assert r.status_code == 200
    # Read back
    r = client.get("/api/progress/1")
    assert r.json()["position_seconds"] == 42.5
    assert r.json()["duration_seconds"] == 600.0


def test_progress_per_user(client):
    client.post("/api/auth/register", data={"username": "alice", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "alice", "password": "secret123"})
    client.put("/api/progress/1", json={"position_seconds": 100.0})
    # Admin alice creates bob
    client.post("/api/auth/register", data={"username": "bob", "password": "secret123"})
    client.post("/api/auth/logout")
    # Bob logs in — should not see alice's progress
    client.post("/api/auth/login", data={"username": "bob", "password": "secret123"})
    r = client.get("/api/progress/1")
    assert r.json()["position_seconds"] == 0.0


def test_progress_updates_overwrite(client):
    client.post("/api/auth/register", data={"username": "alice", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "alice", "password": "secret123"})
    client.put("/api/progress/1", json={"position_seconds": 30.0, "duration_seconds": 600.0})
    client.put("/api/progress/1", json={"position_seconds": 100.0})
    r = client.get("/api/progress/1")
    assert r.json()["position_seconds"] == 100.0
    # Duration should be preserved when omitted
    assert r.json()["duration_seconds"] == 600.0


def test_progress_unknown_media_404(client):
    client.post("/api/auth/register", data={"username": "alice", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "alice", "password": "secret123"})
    r = client.put("/api/progress/9999", json={"position_seconds": 1.0})
    assert r.status_code == 404


# --- HTML pages ---------------------------------------------------------


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "<form" in r.text and "password" in r.text


def test_register_page_first_user_offers_admin(client):
    r = client.get("/register")
    assert r.status_code == 200
    assert "first user" in r.text.lower()


def test_register_page_non_first_admin_only(client):
    client.post("/api/auth/register", data={"username": "alice", "password": "secret123"})
    client.post("/api/auth/logout")
    # Anonymous /register now renders an admin-only message instead of a form
    r = client.get("/register")
    assert r.status_code == 200
    # The form action POSTs to /register; if there's no form, the literal
    # action attribute should not appear in the response.
    assert "action='/register'" not in r.text


def test_html_login_post_sets_cookie_and_redirects(client):
    client.post("/api/auth/register", data={"username": "alice", "password": "secret123"})
    r = client.post(
        "/login",
        data={"username": "alice", "password": "secret123"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
