"""Tests for the login-required gate + landing page."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import auth, db
from cuttlefish.server import create_app


@pytest.fixture
def db_with_admin(tmp_path: Path):
    """A DB that already has at least one user, so the auth gate is active."""
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    auth.create_user(conn, "alice", "secret123", is_admin=True)
    return db_path


def test_anonymous_html_redirects_to_login(db_with_admin):
    client = TestClient(create_app(db_path=db_with_admin))
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


def test_anonymous_html_redirect_preserves_next(db_with_admin):
    client = TestClient(create_app(db_path=db_with_admin))
    r = client.get("/watch/42", follow_redirects=False)
    assert r.status_code == 303
    # The redirect carries ?next=/watch/42 so post-login we land back there.
    assert "next=/watch/42" in r.headers["location"]


def test_anonymous_api_returns_401(db_with_admin):
    client = TestClient(create_app(db_path=db_with_admin))
    assert client.get("/api/media").status_code == 401
    assert client.get("/api/libraries").status_code == 401


def test_anonymous_stream_returns_401(db_with_admin):
    client = TestClient(create_app(db_path=db_with_admin))
    assert client.get("/stream/1").status_code == 401
    assert client.get("/poster/1").status_code == 401
    assert client.get("/subtitle/1").status_code == 401


def test_health_endpoint_stays_public(db_with_admin):
    client = TestClient(create_app(db_path=db_with_admin))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_auth_endpoints_stay_public(db_with_admin):
    """/api/auth/login and /api/auth/register need to be reachable without
    a session, otherwise nobody could ever log in."""
    client = TestClient(create_app(db_path=db_with_admin))
    r = client.post("/api/auth/login",
                    data={"username": "alice", "password": "secret123"})
    assert r.status_code == 200
    # /api/auth/register is admin-only after the first user, but should
    # return its own 403 (not 401 from the middleware).
    client2 = TestClient(create_app(db_path=db_with_admin))
    r = client2.post("/api/auth/register",
                     data={"username": "bob", "password": "secret123"})
    assert r.status_code == 403  # admin-required, NOT 401 from middleware


def test_login_page_shows_landing_warning(db_with_admin):
    client = TestClient(create_app(db_path=db_with_admin))
    r = client.get("/login")
    assert r.status_code == 200
    assert "Authorized access only" in r.text
    assert "private media server" in r.text.lower()


def test_login_then_browse(db_with_admin):
    client = TestClient(create_app(db_path=db_with_admin))
    # Hit / unauthenticated → redirect
    assert client.get("/", follow_redirects=False).status_code == 303
    # Log in
    client.post("/api/auth/login",
                data={"username": "alice", "password": "secret123"})
    # Now / works
    r = client.get("/")
    assert r.status_code == 200


def test_login_redirects_to_next_after_success(db_with_admin):
    client = TestClient(create_app(db_path=db_with_admin))
    r = client.post(
        "/login",
        data={"username": "alice", "password": "secret123", "next": "/watch/7"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/watch/7"


def test_login_rejects_next_external_url(db_with_admin):
    """Defense in depth: a malicious ?next=//evil.com shouldn't redirect
    off-site after login. Only same-origin paths are honored."""
    client = TestClient(create_app(db_path=db_with_admin))
    r = client.post(
        "/login",
        data={"username": "alice", "password": "secret123", "next": "//evil.com/"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_no_users_no_gate(tmp_path):
    """Empty DB → middleware lets everything through. Keeps test fixtures
    that don't bootstrap an admin working, and there's nothing to protect
    on a fresh install anyway."""
    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    client = TestClient(create_app(db_path=db_path))
    assert client.get("/").status_code == 200
    assert client.get("/api/libraries").status_code == 200
